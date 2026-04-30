#!/usr/bin/env python3

"""
1Password Developer Documentation Fetcher

Fetches all human-readable pages under https://developer.1password.com/docs/
as markdown. Every doc page has a .md alternate served as text/markdown,
so we discover URLs via sitemap.xml and llms.txt (union), then fetch each
page directly.

The sitemap lists category landing pages (e.g. /docs/cli), while llms.txt
lists overview pages (e.g. /docs/cli/overview) and provides titles plus
section groupings. Both are used: sitemap is authoritative for what
exists, llms.txt supplies readable metadata.
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

SITE = "https://developer.1password.com"
SITEMAP_URL = f"{SITE}/sitemap.xml"
LLMS_INDEX_URL = f"{SITE}/llms.txt"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

MAX_WORKERS = 8


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={"User-Agent": "1password-api-docs-fetcher/1.0"})
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
# Discovery
# ---------------------------------------------------------------------------

def parse_sitemap(xml: str) -> list[str]:
    """Extract all <loc> URLs from the sitemap."""
    return re.findall(r"<loc>([^<]+)</loc>", xml)


def parse_llms_index(text: str) -> tuple[list[tuple[str, str, str]], dict[str, str]]:
    """Parse llms.txt.

    Returns:
      entries: list of (section, title, url_no_md) in document order.
      titles: mapping of url_no_md -> title.
    """
    entries: list[tuple[str, str, str]] = []
    titles: dict[str, str] = {}
    section = ""
    link_re = re.compile(
        r"^- \[([^\]]+)\]\((https://developer\.1password\.com/docs/[^)]+?)\.md\)"
    )
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = link_re.match(line)
        if m:
            title = m.group(1).strip()
            url = m.group(2)
            entries.append((section, title, url))
            titles.setdefault(url, title)
    return entries, titles


def normalize_docs_url(url: str) -> str | None:
    """Return canonical docs URL without trailing slash or .md suffix.

    Returns None if the URL is not under /docs/.
    """
    if not url.startswith(f"{SITE}/docs/"):
        return None
    url = url.rstrip("/")
    if url.endswith(".md"):
        url = url[:-3]
    return url


def path_for(url: str) -> tuple[str, str, str]:
    """Map a canonical docs URL to (category, subpath, file_path).

    Single-segment URLs (e.g. /docs/cli) are category landing pages and
    get stored as <category>/index.md so they live inside the category
    directory alongside their children.
    """
    rel = url[len(f"{SITE}/docs/"):]
    parts = rel.split("/")
    category = parts[0] if parts else "_root"
    subpath = rel
    if len(parts) == 1:
        file_path = os.path.join(DOCS_DIR, category, "index.md")
    else:
        file_path = os.path.join(DOCS_DIR, *parts) + ".md"
    return category, subpath, file_path


# ---------------------------------------------------------------------------
# Page formatting
# ---------------------------------------------------------------------------

def build_page_markdown(raw: str, title: str | None, source_url: str) -> str:
    """Normalize the fetched markdown and prepend a source link."""
    body = raw.rstrip() + "\n"
    header_title = title
    if not header_title:
        m = re.match(r"^# (.+)$", body, flags=re.MULTILINE)
        if m:
            header_title = m.group(1).strip()
    preamble_lines: list[str] = []
    if header_title and not body.lstrip().startswith(f"# {header_title}"):
        preamble_lines.append(f"# {header_title}")
        preamble_lines.append("")
    preamble_lines.append(f"*Source: [{source_url}]({source_url})*")
    preamble_lines.append("")
    return "\n".join(preamble_lines) + body


def build_category_readme(category: str, pages: list[dict]) -> str:
    display = category.replace("-", " ").title()
    lines = [f"# {display}", ""]
    n = len(pages)
    lines.append(f"{n} page{'s' if n != 1 else ''}.")
    lines.append("")
    for p in sorted(pages, key=lambda x: x["subpath"]):
        if p["subpath"] == category:
            link = "index.md"
        else:
            link = p["subpath"][len(category) + 1:] + ".md"
        lines.append(f"- [{p['title']}](./{link})")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(categories: dict[str, list[dict]]) -> str:
    lines = ["# 1Password Developer Documentation", ""]
    lines.append(f"*Mirrored from [{SITE}/docs/]({SITE}/docs/).*")
    lines.append("")
    lines.append("## Categories")
    lines.append("")
    for cat in sorted(categories):
        display = cat.replace("-", " ").title()
        count = len(categories[cat])
        lines.append(f"- [{display}](./{cat}/) ({count} pages)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def discover_urls() -> tuple[list[str], dict[str, str]]:
    """Discover all docs URLs. Returns (urls, titles)."""
    print("Fetching sitemap...")
    sitemap_xml = fetch_url(SITEMAP_URL)
    if not sitemap_xml:
        print("ERROR: failed to fetch sitemap", file=sys.stderr)
        sys.exit(1)

    print("Fetching llms.txt...")
    llms_text = fetch_url(LLMS_INDEX_URL)
    if not llms_text:
        print("ERROR: failed to fetch llms.txt", file=sys.stderr)
        sys.exit(1)

    sitemap_urls = parse_sitemap(sitemap_xml)
    _, titles = parse_llms_index(llms_text)

    llms_urls = list(titles.keys())
    urls: set[str] = set()
    for u in sitemap_urls + llms_urls:
        canonical = normalize_docs_url(u)
        if canonical:
            urls.add(canonical)

    sorted_urls = sorted(urls)
    print(f"  sitemap: {len(sitemap_urls)} entries")
    print(f"  llms.txt: {len(llms_urls)} entries")
    print(f"  merged docs URLs: {len(sorted_urls)}")
    return sorted_urls, titles


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    urls, titles = discover_urls()

    print(f"Fetching {len(urls)} pages (concurrency={MAX_WORKERS})...")

    fetched: dict[str, str] = {}
    missing: list[str] = []

    def fetch_one(url: str) -> tuple[str, str | None]:
        return url, fetch_url(url + ".md")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, u) for u in urls]
        for fut in as_completed(futures):
            url, content = fut.result()
            if content is None:
                missing.append(url)
            else:
                fetched[url] = content

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

    for url in sorted(fetched):
        raw = fetched[url]
        category, subpath, file_path = path_for(url)
        title = titles.get(url)
        if not title:
            m = re.match(r"^# (.+)$", raw, flags=re.MULTILINE)
            title = m.group(1).strip() if m else subpath
        content = build_page_markdown(raw, titles.get(url), url)
        content_hash = sha256(content)

        categories.setdefault(category, []).append({
            "subpath": subpath,
            "title": title,
            "url": url,
        })

        cache_key = subpath
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

    # Category READMEs
    for category, pages in categories.items():
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

    # Top-level README
    top_readme = build_top_readme(categories)
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
            parts = old_key.split("/")
            if len(parts) == 1:
                old_path = os.path.join(DOCS_DIR, parts[0], "index.md")
            else:
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

    # Prune empty directories bottom-up
    if not args.dry_run and os.path.isdir(DOCS_DIR):
        for root, dirs, files in os.walk(DOCS_DIR, topdown=False):
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
    print(f"  Categories:  {len(categories)}")
    print(f"  Total pages: {sum(len(p) for p in categories.values())}")
    for cat in sorted(categories):
        print(f"    {cat}: {len(categories[cat])}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch 1Password developer docs and mirror to local markdown"
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
