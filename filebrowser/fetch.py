#!/usr/bin/env python3

"""
File Browser Documentation Fetcher

Mirrors the File Browser docs (https://filebrowser.org) from the
filebrowser/filebrowser GitHub repository. The site is MkDocs Material with
no markdown alternates and no llms.txt, so we go to the source: the GitHub
Trees API lists www/docs/**/*.md and raw.githubusercontent serves each file
directly. Four pages the mkdocs nav references (changelog, contributing,
code-of-conduct, security) live at the repo root and are copied into the
page tree by the site build; we map them the same way.

mkdocs.yml supplies page titles, ordering, and grouping for the generated
indexes. The only Material-specific syntax in the content is pymdownx
content tabs (=== "Label"), which are unwrapped into bold labels; the
admonitions already use GitHub-style > [!NOTE] callouts.
"""

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = "filebrowser/filebrowser"
BRANCH = "master"
DOCS_PREFIX = "www/docs/"
SITE_BASE = "https://filebrowser.org"

TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
MKDOCS_URL = f"{RAW_BASE}/www/mkdocs.yml"

# Site pages whose source lives at the repo root rather than in www/docs/
# (the site build copies them in under these names).
ROOT_PAGES = {
    "changelog.md": "CHANGELOG.md",
    "code-of-conduct.md": "CODE-OF-CONDUCT.md",
    "contributing.md": "CONTRIBUTING.md",
    "security.md": "SECURITY.md",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SOURCE_CACHE_KEY = "__source__/tree"

MAX_WORKERS = 8


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    headers = {
        "User-Agent": "filebrowser-docs-fetcher/1.0",
        "Accept-Encoding": "gzip",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            data: bytes = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8")
    except HTTPError as e:
        if e.code == 404:
            return None
        print(f"ERROR: {url}: HTTP {e.code}", file=sys.stderr)
        return None
    except (URLError, TimeoutError, OSError) as e:
        print(f"ERROR: {url}: {e}", file=sys.stderr)
        return None


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return cast(dict, json.load(f))
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def write_file(path: str, content: str, *, dry_run: bool, verbose: bool, label: str) -> None:
    rel = os.path.relpath(path, DOCS_DIR)
    if dry_run:
        print(f"  {label} {rel}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if verbose:
        print(f"  {label} {rel}")


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def discover_pages() -> tuple[dict[str, str], str]:
    """Return page paths and a hash of their Git blobs plus mkdocs.yml."""
    print("Fetching repository tree...")
    body = fetch_url(TREE_URL, timeout=90)
    if not body:
        print("ERROR: failed to fetch tree", file=sys.stderr)
        sys.exit(1)
    data = json.loads(body)
    if data.get("truncated"):
        print("WARNING: tree response truncated; some files may be missing", file=sys.stderr)
    pages: dict[str, str] = {}
    blob_shas: dict[str, str] = {}
    for t in data.get("tree", []):
        path = t.get("path", "")
        if t.get("type") == "blob":
            blob_shas[path] = t.get("sha", "")
        if t.get("type") == "blob" and path.startswith(DOCS_PREFIX) and path.endswith(".md"):
            pages[path[len(DOCS_PREFIX) :]] = path
    for rel, repo_path in ROOT_PAGES.items():
        pages[rel] = repo_path
    relevant = set(pages.values()) | {"www/mkdocs.yml"}
    with open(__file__, "rb") as f:
        fetcher_hash = hashlib.sha256(f.read()).hexdigest()
    fingerprint = sha256(
        json.dumps(
            {
                "blobs": sorted((path, blob_shas.get(path, "")) for path in relevant),
                "fetcher": fetcher_hash,
            },
            separators=(",", ":"),
        )
    )
    return pages, fingerprint


def source_outputs_complete(source: dict) -> bool:
    outputs = source.get("outputs", [])
    return bool(outputs) and all(os.path.isfile(os.path.join(DOCS_DIR, rel)) for rel in outputs)


# ---------------------------------------------------------------------------
# mkdocs.yml nav parsing
# ---------------------------------------------------------------------------


def parse_nav(yml_text: str) -> list[dict]:
    """Parse the `nav:` block of mkdocs.yml into a nested entry list.

    Only the constrained subset mkdocs nav actually uses is handled:
    nested lists of `- Title: path.md`, `- path.md`, and `- Title:` groups.
    Entries are {"title": str|None, "page": rel} or {"title": str,
    "children": [...]}.
    """
    lines = yml_text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if line.rstrip() == "nav:":
            start = i
            break
    if start is None:
        return []

    items: list[dict] = []
    stack: list[tuple[int, list[dict]]] = [(-1, items)]
    for line in lines[start + 1 :]:
        if not line.strip():
            continue
        if not line.startswith(" "):  # left the nav block
            break
        m = re.match(r"^( +)- (.*)$", line)
        if not m:
            continue
        indent = len(m.group(1))
        rest = m.group(2).strip()
        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if rest.endswith(":"):
            children: list[dict] = []
            node: dict = {"title": rest[:-1].strip(), "children": children}
            parent.append(node)
            stack.append((indent, children))
        elif ":" in rest:
            title, _, path = rest.partition(":")
            parent.append({"title": title.strip(), "page": path.strip()})
        else:
            parent.append({"title": None, "page": rest})
    return items


def nav_pages(entries: list[dict]) -> list[str]:
    """Flatten nav to an ordered list of page paths."""
    out: list[str] = []
    for e in entries:
        if "page" in e:
            out.append(e["page"])
        else:
            out.extend(nav_pages(e["children"]))
    return out


# ---------------------------------------------------------------------------
# Content conversion
# ---------------------------------------------------------------------------

TAB_RE = re.compile(r'^( *)===\+? "(.+)"\s*$')


def convert_tabs(text: str) -> str:
    """Unwrap pymdownx content tabs into bold labels with dedented bodies."""
    lines = text.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        m = TAB_RE.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent, label = m.groups()
        prefix = indent + "    "
        out.append(f"{indent}**{label}**")
        out.append("")
        i += 1
        while i < len(lines) and not lines[i].strip():
            i += 1
        while i < len(lines):
            line = lines[i]
            if not line.strip():
                out.append("")
                i += 1
                continue
            if line.startswith(prefix):
                out.append(indent + line[len(prefix) :])
                i += 1
            else:
                break
    return "\n".join(out)


LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)\)")
ASSET_EXT_RE = re.compile(r"\.(png|jpe?g|gif|webp|svg|ico|pdf|ya?ml|json|txt|sh|ps1)$", re.I)


def rewrite_links(text: str, site_dir: str, repo_dir: str, page_set: set[str]) -> str:
    """Fix link targets that do not resolve in the mirrored docs tree.

    The on-disk layout matches the source layout, so links between fetched
    pages are left alone. Static assets become absolute site URLs and
    anything else (e.g. LICENSE) becomes a GitHub blob link.
    """

    def repl(m: re.Match) -> str:
        bang, label, target = m.groups()
        if target.startswith(("http://", "https://", "mailto:", "#")):
            return str(m.group(0))
        path, _, frag = target.partition("#")
        frag = f"#{frag}" if frag else ""
        resolved = posixpath.normpath(posixpath.join(site_dir, path))
        if resolved in page_set:
            return str(m.group(0))
        if resolved.startswith("static/") or (bang and ASSET_EXT_RE.search(path)):
            return f"{bang}[{label}]({SITE_BASE}/{resolved})"
        repo_resolved = posixpath.normpath(posixpath.join(repo_dir, path))
        return f"{bang}[{label}](https://github.com/{REPO}/blob/{BRANCH}/{repo_resolved}{frag})"

    fence = False
    out: list[str] = []
    for line in text.split("\n"):
        if line.lstrip().startswith("```"):
            fence = not fence
            out.append(line)
            continue
        out.append(line if fence else LINK_RE.sub(repl, line))
    return "\n".join(out)


def title_from(body: str, fallback: str) -> str:
    m = re.search(r"^#{1,2}\s+(.+)$", body, flags=re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fallback


DIV_LINE_RE = re.compile(r"^\s*</?div[^>]*>\s*$", re.MULTILINE)


def build_page(raw: str, rel: str, repo_path: str, nav_title: str | None) -> tuple[str, str]:
    """Return (markdown, title) for one page."""
    site_dir = posixpath.dirname(rel)
    repo_dir = posixpath.dirname(repo_path)
    # mkdocs-material md_in_html wrappers (e.g. grid cards) block GFM
    # rendering of their contents; the inner markdown stands on its own.
    body = DIV_LINE_RE.sub("", raw)
    body = convert_tabs(body)
    body = rewrite_links(body, site_dir, repo_dir, PAGE_SET)

    title = nav_title or title_from(body, fallback=rel)
    source_url = f"{SITE_BASE}/{rel[:-3]}.html"
    repo_url = f"https://github.com/{REPO}/blob/{BRANCH}/{repo_path}"

    parts: list[str] = []
    m = re.match(r"^(#\s+.+)\n+", body)
    if m:
        parts.append(m.group(1).strip())
        body = body[m.end() :]
    else:
        parts.append(f"# {title}")
    parts.append("")
    parts.append(f"*Source: [{source_url}]({source_url})*")
    parts.append(f"*Repo: [{repo_url}]({repo_url})*")
    parts.append("")
    parts.append("")
    return "\n".join(parts) + body.strip("\n") + "\n", title


PAGE_SET: set[str] = set()


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


def render_nav_list(
    entries: list[dict], titles: dict[str, str], page_set: set[str], depth: int = 0
) -> list[str]:
    lines: list[str] = []
    pad = "  " * depth
    for e in entries:
        if "page" in e:
            rel = e["page"]
            if rel not in page_set:
                continue
            title = e["title"] or titles.get(rel, rel)
            lines.append(f"{pad}- [{title}](./{rel})")
        else:
            lines.append(f"{pad}- **{e['title']}**")
            lines.extend(render_nav_list(e["children"], titles, page_set, depth + 1))
    return lines


def build_top_readme(nav: list[dict], titles: dict[str, str], page_set: set[str]) -> str:
    lines = ["# File Browser Documentation", ""]
    lines.append(f"*Mirrored from [{SITE_BASE}/]({SITE_BASE}/).*")
    lines.append(f"*Source repo: [github.com/{REPO}](https://github.com/{REPO}).*")
    lines.append("")
    lines.extend(render_nav_list(nav, titles, page_set))
    lines.append("")

    in_nav = set(nav_pages(nav))
    extras = sorted(page_set - in_nav)
    if extras:
        lines.append("## Other")
        lines.append("")
        for rel in extras:
            lines.append(f"- [{titles.get(rel, rel)}](./{rel})")
        lines.append("")
    return "\n".join(lines)


def build_cli_readme(nav_order: list[str], titles: dict[str, str], page_set: set[str]) -> str:
    cli = [p for p in nav_order if p.startswith("cli/") and p in page_set]
    cli += sorted(p for p in page_set if p.startswith("cli/") and p not in cli)
    lines = ["# Command Line Usage", ""]
    lines.append(f"{len(cli)} pages documenting the `filebrowser` CLI.")
    lines.append("")
    for rel in cli:
        name = rel[len("cli/") :]
        lines.append(f"- [{titles.get(rel, rel)}](./{name})")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(args: argparse.Namespace) -> None:
    # The previous cache is always loaded for removal detection; --force
    # only disables the unchanged-skip.
    old_cache = load_cache()
    cache = {} if args.force else old_cache

    pages, source_fingerprint = discover_pages()
    print(
        f"  found {len(pages)} pages ({len(pages) - len(ROOT_PAGES)} in "
        f"{DOCS_PREFIX}, {len(ROOT_PAGES)} repo-root)"
    )

    previous_source = cache.get(SOURCE_CACHE_KEY, {})
    if (
        not args.force
        and previous_source.get("fingerprint") == source_fingerprint
        and source_outputs_complete(previous_source)
    ):
        total_files = len(previous_source["outputs"])
        print("  Source tree unchanged and all outputs present; skipping content downloads and conversion")
        print("\nSync complete:")
        print("  Added:       0")
        print("  Updated:     0")
        print(f"  Unchanged:   {total_files}")
        print("  Removed:     0")
        print("  Unavailable: 0")
        print(f"  Total pages: {previous_source.get('page_count', 0)}")
        return

    print("Fetching mkdocs.yml...")
    mkdocs_yml = fetch_url(MKDOCS_URL)
    if mkdocs_yml is None:
        print("ERROR: failed to fetch mkdocs.yml; leaving the existing mirror untouched", file=sys.stderr)
        sys.exit(1)
    nav = parse_nav(mkdocs_yml) if mkdocs_yml else []
    if not nav:
        print("WARNING: could not parse mkdocs nav; indexes will be flat", file=sys.stderr)

    nav_titles: dict[str, str] = {}

    def collect_titles(entries: list[dict]) -> None:
        for e in entries:
            if "page" in e and e["title"]:
                nav_titles[e["page"]] = e["title"]
            elif "children" in e:
                collect_titles(e["children"])

    collect_titles(nav)

    print(f"Fetching content (concurrency={MAX_WORKERS})...")

    def fetch_one(item: tuple[str, str]) -> tuple[str, str | None]:
        rel, repo_path = item
        return rel, fetch_url(f"{RAW_BASE}/{repo_path}")

    fetched: dict[str, str] = {}
    missing: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, item) for item in pages.items()]
        for fut in as_completed(futures):
            rel, content = fut.result()
            if content is None:
                missing.append(rel)
            else:
                fetched[rel] = content

    print(f"  fetched: {len(fetched)}")
    if missing:
        print(f"  unavailable: {len(missing)}")
        for rel in sorted(missing):
            print(f"    SKIP {rel}")
        print(
            "ERROR: source tree entries could not be fetched; leaving the existing mirror untouched",
            file=sys.stderr,
        )
        sys.exit(1)

    global PAGE_SET
    page_set = set(fetched)
    PAGE_SET = page_set

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}
    titles: dict[str, str] = {}
    output_paths: set[str] = set()

    def emit(cache_key: str, file_path: str, content: str) -> None:
        nonlocal added, updated, unchanged
        output_paths.add(os.path.relpath(file_path, DOCS_DIR))
        content_hash = sha256(content)
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[cache_key] = prev
            return
        is_new = cache_key not in cache or not os.path.exists(file_path)
        write_file(
            file_path,
            content,
            dry_run=args.dry_run,
            verbose=args.verbose,
            label="ADD" if is_new else "UPDATE",
        )
        new_cache[cache_key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    for rel in sorted(fetched):
        page_md, title = build_page(fetched[rel], rel, pages[rel], nav_titles.get(rel))
        titles[rel] = title
        emit(rel, os.path.join(DOCS_DIR, *rel.split("/")), page_md)

    emit("__readme__/_top", os.path.join(DOCS_DIR, "README.md"), build_top_readme(nav, titles, page_set))
    emit(
        "__readme__/cli",
        os.path.join(DOCS_DIR, "cli", "README.md"),
        build_cli_readme(nav_pages(nav), titles, page_set),
    )

    new_cache[SOURCE_CACHE_KEY] = {
        "fingerprint": source_fingerprint,
        "outputs": sorted(output_paths),
        "page_count": len(fetched),
        "last_updated": datetime.now(UTC).isoformat(),
    }

    # Removals
    removed = 0
    for old_key in sorted(old_cache):
        if old_key in new_cache:
            continue
        if old_key == SOURCE_CACHE_KEY:
            continue
        if old_key == "__readme__/_top":
            old_path = os.path.join(DOCS_DIR, "README.md")
        elif old_key.startswith("__readme__/"):
            old_path = os.path.join(DOCS_DIR, old_key[len("__readme__/") :], "README.md")
        else:
            old_path = os.path.join(DOCS_DIR, *old_key.split("/"))
        if not os.path.exists(old_path):
            continue
        if args.dry_run:
            print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        else:
            os.remove(old_path)
            if args.verbose:
                print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        removed += 1

    if not args.dry_run and os.path.isdir(DOCS_DIR):
        for root, _dirs, _files in os.walk(DOCS_DIR, topdown=False):
            if root != DOCS_DIR and not os.listdir(root):
                os.rmdir(root)

    if not args.dry_run:
        save_cache(new_cache)

    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Unavailable: {len(missing)}")
    print(f"  Total pages: {len(fetched)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch File Browser docs from GitHub and mirror to local markdown"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
