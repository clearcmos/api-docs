#!/usr/bin/env python3

"""
Ollama Documentation Fetcher

Fetches https://docs.ollama.com pages as markdown. The Mintlify-hosted site
exposes a discovery index at /llms.txt and serves every page with a .md
alternate (e.g. /quickstart.md, /api/chat.md), so we parse llms.txt for the
URL list and fetch each page directly.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SITE = "https://docs.ollama.com"
LLMS_INDEX_URL = f"{SITE}/llms.txt"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

MAX_WORKERS = 8


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={"User-Agent": "ollama-api-docs-fetcher/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
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
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
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
# Index parsing
# ---------------------------------------------------------------------------

def parse_llms_index(text: str) -> list[dict]:
    """Parse llms.txt into a list of {section, title, url, summary} entries.

    Sections are introduced by '## Heading' lines. Each item is:
        - [Title](https://docs.ollama.com/path.md): optional summary
    """
    section = ""
    entries: list[dict] = []
    link_re = re.compile(
        r"^- \[([^\]]+)\]\((https://docs\.ollama\.com/[^)]+)\)\s*(?::\s*(.*))?$"
    )
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = link_re.match(line)
        if m:
            title = m.group(1).strip()
            url = m.group(2).strip()
            summary = (m.group(3) or "").strip()
            entries.append({
                "section": section,
                "title": title,
                "url": url,
                "summary": summary,
            })
    return entries


def path_for_md(url: str) -> tuple[str, str, str]:
    """Map a .md URL to (category, slug, file_path).

    https://docs.ollama.com/quickstart.md       -> ("", "quickstart", docs/quickstart.md)
    https://docs.ollama.com/api/chat.md         -> ("api", "chat", docs/api/chat.md)
    https://docs.ollama.com/index.md            -> ("", "index", docs/README.md)
    https://docs.ollama.com/api/introduction.md -> ("api", "introduction", docs/api/introduction.md)
    """
    rel = url[len(f"{SITE}/"):]
    if rel.endswith(".md"):
        rel = rel[:-3]
    parts = rel.split("/")
    if len(parts) == 1:
        category = ""
        slug = parts[0]
        # Don't route /index to docs/README.md - the auto-generated catalogue
        # README would clobber it. Use index.md so both can coexist.
        file_path = os.path.join(DOCS_DIR, f"{slug}.md")
    else:
        category = parts[0]
        slug = "/".join(parts[1:])
        file_path = os.path.join(DOCS_DIR, *parts) + ".md"
    return category, slug, file_path


# ---------------------------------------------------------------------------
# Page formatting
# ---------------------------------------------------------------------------

PREAMBLE_RE = re.compile(
    r"^> ## Documentation Index\n"
    r"> Fetch the complete documentation index at: [^\n]+\n"
    r"> Use this file to discover all available pages before exploring further\.\n+",
)


def clean_page(raw: str) -> str:
    """Strip Mintlify's boilerplate index banner from the top of each page."""
    return PREAMBLE_RE.sub("", raw, count=1).lstrip("\n")


def build_page_markdown(raw: str, title: str, source_url: str) -> str:
    body = clean_page(raw).rstrip() + "\n"
    has_h1 = re.match(r"^# .+", body, flags=re.MULTILINE)
    preamble: list[str] = []
    if not has_h1 and title:
        preamble.append(f"# {title}")
        preamble.append("")
    preamble.append(f"*Source: [{source_url}]({source_url})*")
    preamble.append("")
    return "\n".join(preamble) + body


def build_category_readme(category: str, pages: list[dict]) -> str:
    display = category.replace("-", " ").title() if category else "Top Level"
    lines = [f"# {display}", ""]
    n = len(pages)
    lines.append(f"{n} page{'s' if n != 1 else ''}.")
    lines.append("")
    for p in sorted(pages, key=lambda x: x["slug"]):
        lines.append(f"- [{p['title']}](./{p['slug']}.md)")
        if p.get("summary"):
            lines.append(f"  - {p['summary']}")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(categories: dict[str, list[dict]], openapi_links: list[dict]) -> str:
    lines = ["# Ollama Documentation", ""]
    lines.append(f"*Mirrored from [{SITE}]({SITE}).*")
    lines.append("")
    if "" in categories:
        lines.append("## Top-level pages")
        lines.append("")
        for p in sorted(categories[""], key=lambda x: x["slug"]):
            lines.append(f"- [{p['title']}](./{p['slug']}.md)")
        lines.append("")
    lines.append("## Categories")
    lines.append("")
    for cat in sorted(c for c in categories if c):
        display = cat.replace("-", " ").title()
        count = len(categories[cat])
        lines.append(f"- [{display}](./{cat}/) ({count} pages)")
    lines.append("")
    if openapi_links:
        lines.append("## OpenAPI Specs")
        lines.append("")
        for item in openapi_links:
            lines.append(f"- [{item['title']}]({item['url']})")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print("Fetching llms.txt...")
    index_text = fetch_url(LLMS_INDEX_URL)
    if not index_text:
        print("ERROR: failed to fetch llms.txt", file=sys.stderr)
        sys.exit(1)

    entries = parse_llms_index(index_text)
    doc_entries = [e for e in entries if e["section"].lower() != "openapi specs"
                   and e["url"].endswith(".md")]
    openapi_links = [e for e in entries if e["section"].lower() == "openapi specs"]
    print(f"  doc pages:    {len(doc_entries)}")
    print(f"  openapi refs: {len(openapi_links)}")

    print(f"Fetching {len(doc_entries)} pages (concurrency={MAX_WORKERS})...")

    fetched: dict[str, str] = {}
    missing: list[str] = []

    def fetch_one(entry: dict) -> tuple[dict, str | None]:
        return entry, fetch_url(entry["url"])

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, e) for e in doc_entries]
        for fut in as_completed(futures):
            entry, content = fut.result()
            if content is None:
                missing.append(entry["url"])
            else:
                fetched[entry["url"]] = content

    print(f"  fetched: {len(fetched)}")
    if missing:
        print(f"  unavailable (.md 404): {len(missing)}")
        if args.verbose:
            for u in sorted(missing):
                print(f"    SKIP {u}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    new_cache: dict = {}
    categories: dict[str, list[dict]] = {}

    for entry in doc_entries:
        url = entry["url"]
        raw = fetched.get(url)
        if raw is None:
            continue
        category, slug, file_path = path_for_md(url)
        content = build_page_markdown(raw, entry["title"], url)
        content_hash = sha256(content)

        categories.setdefault(category, []).append({
            "slug": slug if not (category == "" and slug == "index") else "index",
            "title": entry["title"],
            "summary": entry["summary"],
        })

        cache_key = url[len(SITE) + 1:]  # path within docs.ollama.com
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[cache_key] = prev
            continue

        is_new = cache_key not in cache or not os.path.exists(file_path)
        label = "ADD" if is_new else "UPDATE"
        write_file(file_path, content, dry_run=args.dry_run, verbose=args.verbose, label=label)
        new_cache[cache_key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Category READMEs (skip top-level "" so we don't overwrite README.md)
    for category, pages in categories.items():
        if not category:
            continue
        readme_content = build_category_readme(category, pages)
        readme_path = os.path.join(DOCS_DIR, category, "README.md")
        cache_key = f"__readme__/{category}"
        content_hash = sha256(readme_content)
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = prev
            continue
        is_new = cache_key not in cache or not os.path.exists(readme_path)
        write_file(readme_path, readme_content, dry_run=args.dry_run, verbose=args.verbose,
                   label="ADD" if is_new else "UPDATE")
        new_cache[cache_key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Top-level README. If llms.txt provides an index page, prefer that for body
    # content but always append the auto-generated category listing for navigation.
    top_readme = build_top_readme(categories, openapi_links)
    top_path = os.path.join(DOCS_DIR, "README.md")
    top_key = "__readme__/_top"
    top_hash = sha256(top_readme)
    prev = cache.get(top_key, {})
    if prev.get("sha256") == top_hash and os.path.exists(top_path):
        unchanged += 1
        new_cache[top_key] = prev
    else:
        is_new = top_key not in cache or not os.path.exists(top_path)
        write_file(top_path, top_readme, dry_run=args.dry_run, verbose=args.verbose,
                   label="ADD" if is_new else "UPDATE")
        new_cache[top_key] = {
            "sha256": top_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Removals
    removed = 0
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        if old_key.startswith("__readme__/"):
            name = old_key[len("__readme__/"):]
            if name == "_top":
                old_path = os.path.join(DOCS_DIR, "README.md")
            else:
                old_path = os.path.join(DOCS_DIR, name, "README.md")
        else:
            # path looks like "api/chat.md" or "quickstart.md"
            rel_no_ext = old_key[:-3] if old_key.endswith(".md") else old_key
            parts = rel_no_ext.split("/")
            if len(parts) == 1 and parts[0] == "index":
                # index lived at top README; skip (handled by _top)
                continue
            old_path = os.path.join(DOCS_DIR, *parts) + ".md"
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

    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Unavailable: {len(missing)}")
    print(f"  Categories:  {sum(1 for c in categories if c)}")
    total = sum(len(p) for p in categories.values())
    print(f"  Total pages: {total}")
    for cat in sorted(categories):
        name = cat if cat else "(root)"
        print(f"    {name}: {len(categories[cat])}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Ollama documentation and mirror to local markdown"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true",
                        help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
