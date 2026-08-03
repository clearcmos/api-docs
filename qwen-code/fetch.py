#!/usr/bin/env python3

"""
Qwen Code Documentation Fetcher

Mirrors the English Qwen Code docs from the QwenLM/qwen-code-docs GitHub
repository (the source of https://qwenlm.github.io/qwen-code-docs/en/).

The rendered Nextra site does not serve raw markdown, so we go to the source:
the GitHub Trees API gives us the full file listing, and raw.githubusercontent
serves each .md/.mdx file directly. This captures everything that ships on
the site, including the User Guide, Developer Guide, design notes, blog, and
showcase. Other locales (zh, ja, de, fr, ru, pt-BR) are skipped intentionally.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = "QwenLM/qwen-code-docs"
BRANCH = "main"
CONTENT_PREFIX = "website/content/en/"
SITE_BASE = "https://qwenlm.github.io/qwen-code-docs/en"

TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SOURCE_CACHE_KEY = "__source__/tree"

MAX_WORKERS = 16


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    headers = {
        "User-Agent": "qwen-code-docs-fetcher/1.0",
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


def discover_paths() -> tuple[list[str], str]:
    """Return source paths and a hash of their Git blobs."""
    print("Fetching repository tree...")
    body = fetch_url(TREE_URL, timeout=90)
    if not body:
        print("ERROR: failed to fetch tree", file=sys.stderr)
        sys.exit(1)
    data = json.loads(body)
    if data.get("truncated"):
        print("WARNING: tree response truncated; some files may be missing", file=sys.stderr)
    blobs = [
        (t["path"], t.get("sha", ""))
        for t in data.get("tree", [])
        if t.get("type") == "blob"
        and t["path"].startswith(CONTENT_PREFIX)
        and (t["path"].endswith(".md") or t["path"].endswith(".mdx"))
    ]
    with open(__file__, "rb") as f:
        fetcher_hash = hashlib.sha256(f.read()).hexdigest()
    fingerprint = sha256(
        json.dumps(
            {
                "blobs": sorted(blobs),
                "fetcher": fetcher_hash,
            },
            separators=(",", ":"),
        )
    )
    return sorted(path for path, _ in blobs), fingerprint


def source_outputs_complete(source: dict) -> bool:
    outputs = source.get("outputs", [])
    return bool(outputs) and all(os.path.isfile(os.path.join(DOCS_DIR, rel)) for rel in outputs)


def site_url_for(rel_path: str) -> str:
    """Map a content-relative path to its rendered Nextra URL."""
    base = rel_path[:-4] if rel_path.endswith(".mdx") else rel_path[:-3]
    # Nextra renders an `index` file as the directory itself.
    if base.endswith("/index"):
        base = base[: -len("/index")]
    elif base == "index":
        return SITE_BASE + "/"
    return f"{SITE_BASE}/{base}/"


def output_path_for(rel_path: str) -> str:
    """Map content path to output path.

    Strip extension, then write as `<base>.md` (we normalize .mdx to .md).
    Preserves the source repo layout, with index files becoming directory
    READMEs so each category has an obvious entry point.
    """
    base = rel_path[:-4] if rel_path.endswith(".mdx") else rel_path[:-3]
    parts = base.split("/")
    if parts[-1] == "index":
        parts = parts[:-1]
        if not parts:
            return os.path.join(DOCS_DIR, "index.md")
        return os.path.join(DOCS_DIR, *parts, "_index.md")
    return os.path.join(DOCS_DIR, *parts) + ".md"


# ---------------------------------------------------------------------------
# Content formatting
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict[str, str], str]:
    """Tiny YAML-ish frontmatter parser: handles `key: value` only.

    Lists, nested mappings, and folded scalars are left as raw strings.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict[str, str] = {}
    for line in m.group(1).splitlines():
        if ":" not in line or line.startswith(" ") or line.startswith("-"):
            continue
        key, _, value = line.partition(":")
        meta[key.strip()] = value.strip().strip('"').strip("'")
    return meta, text[m.end() :]


def title_from(meta: dict[str, str], body: str, fallback: str) -> str:
    if "title" in meta and meta["title"]:
        return meta["title"]
    m = re.search(r"^#\s+(.+)$", body, flags=re.MULTILINE)
    if m:
        return m.group(1).strip()
    return fallback


def build_page_markdown(raw: str, source_url: str, repo_url: str) -> tuple[str, str]:
    """Return (markdown, title) for a single page."""
    meta, body = parse_frontmatter(raw)
    title = title_from(meta, body, fallback="")

    has_h1 = re.match(r"^#\s+.+", body, flags=re.MULTILINE) is not None
    parts: list[str] = []
    if not has_h1 and title:
        parts.append(f"# {title}")
        parts.append("")
    parts.append(f"*Source: [{source_url}]({source_url})*")
    parts.append(f"*Repo: [{repo_url}]({repo_url})*")
    if meta:
        meta_keys = [k for k in ("description", "author", "date", "tags") if k in meta]
        for k in meta_keys:
            parts.append(f"*{k.title()}: {meta[k]}*")
    parts.append("")
    page = "\n".join(parts) + body.rstrip() + "\n"
    return page, title


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------


def build_top_readme(pages: list[dict]) -> str:
    by_section: dict[str, list[dict]] = {}
    root_pages: list[dict] = []
    for p in pages:
        if p["section"]:
            by_section.setdefault(p["section"], []).append(p)
        else:
            root_pages.append(p)

    lines = ["# Qwen Code Documentation", ""]
    lines.append(f"*Mirrored from [{SITE_BASE}/]({SITE_BASE}/).*")
    lines.append(f"*Source repo: [github.com/{REPO}](https://github.com/{REPO}).*")
    lines.append("")
    if root_pages:
        for p in sorted(root_pages, key=lambda x: x["link"]):
            lines.append(f"- [{p['title']}](./{p['link']})")
        lines.append("")
    lines.append("## Sections")
    lines.append("")
    for section in sorted(by_section):
        display = section.replace("-", " ").title()
        count = len(by_section[section])
        lines.append(f"- [{display}](./{section}/) ({count} pages)")
    lines.append("")
    return "\n".join(lines)


def build_section_readme(section: str, pages: list[dict]) -> str:
    """Index a top-level section, organizing nested pages by subdir."""
    display = section.replace("-", " ").title()
    lines = [f"# {display}", ""]
    n = len(pages)
    lines.append(f"{n} page{'s' if n != 1 else ''} in this section.")
    lines.append("")

    direct: list[dict] = []
    by_subdir: dict[str, list[dict]] = {}
    for p in pages:
        link_in_section = p["link"][len(section) + 1 :]  # strip "section/"
        parts = link_in_section.split("/")
        if len(parts) == 1:
            direct.append(p)
        else:
            by_subdir.setdefault(parts[0], []).append(p)

    if direct:
        for p in sorted(direct, key=lambda x: x["link"]):
            name = p["link"][len(section) + 1 :]
            lines.append(f"- [{p['title']}](./{name})")
        lines.append("")
    for sub in sorted(by_subdir):
        sub_display = sub.replace("-", " ").title()
        lines.append(f"### {sub_display}")
        lines.append("")
        for p in sorted(by_subdir[sub], key=lambda x: x["link"]):
            rel_after_section = p["link"][len(section) + 1 :]
            lines.append(f"- [{p['title']}](./{rel_after_section})")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    repo_paths, source_fingerprint = discover_paths()
    print(f"  found {len(repo_paths)} en/ markdown files")

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

    print(f"Fetching content (concurrency={MAX_WORKERS})...")

    def fetch_one(rp: str) -> tuple[str, str | None]:
        return rp, fetch_url(f"{RAW_BASE}/{rp}")

    fetched: dict[str, str] = {}
    missing: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, rp) for rp in repo_paths]
        for fut in as_completed(futures):
            rp, content = fut.result()
            if content is None:
                missing.append(rp)
            else:
                fetched[rp] = content

    print(f"  fetched: {len(fetched)}")
    if missing:
        print(f"  unavailable: {len(missing)}")
        if args.verbose:
            for rp in sorted(missing):
                print(f"    SKIP {rp}")
        print(
            "ERROR: source tree entries could not be fetched; leaving the existing mirror untouched",
            file=sys.stderr,
        )
        sys.exit(1)

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    new_cache: dict = {}
    pages: list[dict] = []  # {rel, title, ...}
    output_paths: set[str] = set()

    for rp in repo_paths:
        raw = fetched.get(rp)
        if raw is None:
            continue
        content_rel = rp[len(CONTENT_PREFIX) :]  # e.g. users/quickstart.md
        rel_no_ext = content_rel[:-4] if content_rel.endswith(".mdx") else content_rel[:-3]
        source_url = site_url_for(content_rel)
        repo_url = f"https://github.com/{REPO}/blob/{BRANCH}/{rp}"
        page, title = build_page_markdown(raw, source_url, repo_url)
        file_path = output_path_for(content_rel)
        output_paths.add(os.path.relpath(file_path, DOCS_DIR))
        cache_key = content_rel

        parts = rel_no_ext.split("/")
        if rel_no_ext == "index":
            section = ""
            link = "index.md"
        elif parts[-1] == "index":
            # A section-level index (e.g. blog/index, showcase/index) becomes
            # the section's _index.md. Group it under the section.
            section = parts[0]
            link = "/".join(parts[:-1]) + "/_index.md"
        else:
            section = parts[0] if len(parts) > 1 else ""
            link = rel_no_ext + ".md"

        pages.append(
            {
                "section": section,
                "link": link,
                "title": title or content_rel,
                "source_url": source_url,
            }
        )

        content_hash = sha256(page)
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[cache_key] = prev
            continue

        is_new = cache_key not in cache or not os.path.exists(file_path)
        label = "ADD" if is_new else "UPDATE"
        write_file(file_path, page, dry_run=args.dry_run, verbose=args.verbose, label=label)
        new_cache[cache_key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Per-section READMEs (exclude root pages)
    by_section: dict[str, list[dict]] = {}
    for p in pages:
        if not p["section"]:
            continue
        by_section.setdefault(p["section"], []).append(p)

    for section, section_pages in by_section.items():
        readme_content = build_section_readme(section, section_pages)
        readme_path = os.path.join(DOCS_DIR, section, "README.md")
        output_paths.add(os.path.relpath(readme_path, DOCS_DIR))
        cache_key = f"__readme__/{section}"
        content_hash = sha256(readme_content)
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = prev
            continue
        is_new = cache_key not in cache or not os.path.exists(readme_path)
        write_file(
            readme_path,
            readme_content,
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

    # Top-level README
    top_readme = build_top_readme(pages)
    top_path = os.path.join(DOCS_DIR, "README.md")
    output_paths.add(os.path.relpath(top_path, DOCS_DIR))
    top_key = "__readme__/_top"
    top_hash = sha256(top_readme)
    prev = cache.get(top_key, {})
    if prev.get("sha256") == top_hash and os.path.exists(top_path):
        unchanged += 1
        new_cache[top_key] = prev
    else:
        is_new = top_key not in cache or not os.path.exists(top_path)
        write_file(
            top_path,
            top_readme,
            dry_run=args.dry_run,
            verbose=args.verbose,
            label="ADD" if is_new else "UPDATE",
        )
        new_cache[top_key] = {
            "sha256": top_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    new_cache[SOURCE_CACHE_KEY] = {
        "fingerprint": source_fingerprint,
        "outputs": sorted(output_paths),
        "page_count": len(pages),
        "last_updated": datetime.now(UTC).isoformat(),
    }

    # Removals
    removed = 0
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        if old_key == SOURCE_CACHE_KEY:
            continue
        if old_key.startswith("__readme__/"):
            name = old_key[len("__readme__/") :]
            if name == "_top":
                old_path = os.path.join(DOCS_DIR, "README.md")
            else:
                old_path = os.path.join(DOCS_DIR, name, "README.md")
        else:
            old_path = output_path_for(old_key)
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
            if root == DOCS_DIR:
                continue
            if not os.listdir(root):
                os.rmdir(root)
                if args.verbose:
                    print(f"  RMDIR {os.path.relpath(root, DOCS_DIR)}/")

    if not args.dry_run:
        save_cache(new_cache)

    section_counts: dict[str, int] = {}
    for p in pages:
        section = p["section"] if p["section"] else "(root)"
        section_counts[section] = section_counts.get(section, 0) + 1

    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Unavailable: {len(missing)}")
    print(f"  Total pages: {len(pages)}")
    for section in sorted(section_counts):
        print(f"    {section}: {section_counts[section]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Qwen Code English docs from GitHub and mirror to local markdown"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
