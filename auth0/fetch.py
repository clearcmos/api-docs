#!/usr/bin/env python3

"""
Auth0 API Documentation Fetcher

Fetches API documentation for all Auth0 APIs from the official docs site.
Uses the llms.txt index to discover pages and llms-full.txt for content,
since individual doc pages are rendered client-side and don't serve raw markdown.

Covers four APIs:
  - Authentication API
  - Management API v2
  - My Account API
  - My Organization API
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

LLMS_INDEX_URL = "https://auth0.com/docs/llms.txt"
LLMS_FULL_URL = "https://auth0.com/docs/llms-full.txt"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

# Maps URL path prefix to a short API name used for directory layout.
API_PREFIXES = {
    "api/authentication/": "authentication",
    "api/management/v2/": "management-v2",
    "api/myaccount/": "myaccount",
    "api/myorganization/": "myorganization",
}

# Page size for paginated Management API list endpoints.
MGMT_PAGE_SIZE = 50


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 120) -> str | None:
    req = Request(url, headers={"User-Agent": "auth0-api-docs-fetcher/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        print(f"ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
        return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "-", name)
    return name.lower().strip("-")


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
    if dry_run:
        print(f"  {label} {os.path.relpath(path, DOCS_DIR)}")
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        if verbose:
            print(f"  {label} {os.path.relpath(path, DOCS_DIR)}")


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------

def discover_api_urls(index_text: str) -> list[str]:
    """Extract API documentation URLs from the llms.txt index."""
    urls = []
    for line in index_text.split("\n"):
        m = re.search(r"\((https://auth0\.com/docs/api/[^)]+\.md)\)", line)
        if m:
            urls.append(m.group(1))
    return urls


def parse_full_text(full_text: str) -> dict[str, str]:
    """Split llms-full.txt into {source_url: page_content} mapping.

    Each page starts with:
        # Title
        Source: https://auth0.com/docs/...
    """
    pages: dict[str, str] = {}
    chunks = re.split(r"\n(?=# [^\n]+\nSource: https://auth0\.com/docs/)", full_text)
    for chunk in chunks:
        m = re.match(r"# [^\n]+\nSource: (https://auth0\.com/docs/[^\n]+)", chunk)
        if m:
            pages[m.group(1)] = chunk.strip()
    return pages


def classify_url(url: str) -> tuple[str, str, str] | None:
    """Return (api_name, tag, slug) for an API doc URL, or None if not API."""
    path = url.replace("https://auth0.com/docs/", "").removesuffix(".md")

    for prefix, api_name in API_PREFIXES.items():
        if path.startswith(prefix):
            remainder = path[len(prefix):]
            parts = remainder.split("/")
            if len(parts) == 2:
                tag, slug = parts
                return api_name, tag, slug
            elif len(parts) == 1 and parts[0] == "index":
                return api_name, "", "index"
            elif len(parts) == 1:
                return api_name, "", parts[0]
            break

    return None


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------

def build_page_markdown(content: str, source_url: str) -> str:
    """Clean up raw llms-full.txt page content into final markdown."""
    lines = content.split("\n")
    out = []
    for line in lines:
        # Keep Source line as a metadata comment
        if line.startswith("Source: "):
            out.append(f"*Source: [{line[8:]}]({line[8:]})*\n")
            continue
        # Strip custom JSX-like tags that aren't useful in static markdown
        if re.match(r"^\s*<(ApiReleaseLifecycle|Scopes|AuthDocsPipeline)\s*/?>", line):
            continue
        out.append(line)
    return "\n".join(out).strip() + "\n"


def build_tag_readme(api_name: str, tag: str, pages: list[dict]) -> str:
    """Build a README.md for a tag directory listing its pages."""
    title = tag.replace("-", " ").title()
    lines = [f"# {title}\n"]
    lines.append(f"**API:** {api_name}\n")
    lines.append("## Endpoints\n")
    for p in sorted(pages, key=lambda x: x["slug"]):
        filename = f"{p['slug']}.md"
        lines.append(f"- [{p['title']}](./{filename})")
    lines.append("")
    return "\n".join(lines)


def build_api_readme(api_name: str, tags: dict[str, list[dict]]) -> str:
    """Build a top-level README.md for an API grouping."""
    display = api_name.replace("-", " ").title()
    lines = [f"# {display}\n"]
    lines.append("## Categories\n")
    for tag in sorted(tags.keys()):
        if tag == "":
            for p in sorted(tags[tag], key=lambda x: x["slug"]):
                lines.append(f"- [{p['title']}](./{p['slug']}.md)")
        else:
            count = len(tags[tag])
            display_tag = tag.replace("-", " ").title()
            lines.append(f"- [{display_tag}](./{tag}/) ({count} endpoints)")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(api_names: dict[str, int]) -> str:
    """Build the top-level docs/README.md."""
    lines = ["# Auth0 API Documentation\n"]
    lines.append("## APIs\n")
    for name in sorted(api_names.keys()):
        display = name.replace("-", " ").title()
        count = api_names[name]
        lines.append(f"- [{display}](./{name}/) ({count} pages)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    # Step 1: Fetch the index and full content
    print("Fetching Auth0 docs index...")
    index_text = fetch_url(LLMS_INDEX_URL)
    if not index_text:
        sys.exit(1)

    api_urls = discover_api_urls(index_text)
    print(f"  Found {len(api_urls)} API doc pages in index")

    # Classify URLs
    classified: list[tuple[str, str, str, str]] = []  # (api, tag, slug, url)
    for url in api_urls:
        info = classify_url(url)
        if info:
            api_name, tag, slug = info
            classified.append((api_name, tag, slug, url))

    print(f"  Classified {len(classified)} pages across APIs")

    # Build set of source URLs we need content for (strip .md suffix for matching)
    needed_sources = set()
    url_to_clean = {}
    for api_name, tag, slug, url in classified:
        # llms-full.txt uses URLs without .md suffix
        clean = url.removesuffix(".md")
        needed_sources.add(clean)
        url_to_clean[url] = clean

    print("Fetching Auth0 docs full content...")
    full_text = fetch_url(LLMS_FULL_URL, timeout=180)
    if not full_text:
        sys.exit(1)

    all_pages = parse_full_text(full_text)
    print(f"  Parsed {len(all_pages)} total pages from full text")

    # Match content to classified pages
    matched = 0
    for api_name, tag, slug, url in classified:
        clean = url_to_clean[url]
        if clean in all_pages:
            matched += 1

    print(f"  Matched {matched}/{len(classified)} API pages to content")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    new_cache: dict = {}

    # Organize by api -> tag -> pages
    api_tags: dict[str, dict[str, list[dict]]] = {}

    for api_name, tag, slug, url in classified:
        clean = url_to_clean[url]
        raw_content = all_pages.get(clean)
        if not raw_content:
            if args.verbose:
                print(f"  SKIP (no content) {api_name}/{tag}/{slug}")
            continue

        # Extract title from first line
        first_line = raw_content.split("\n")[0]
        title = first_line.lstrip("# ").strip()

        content = build_page_markdown(raw_content, url)
        content_hash = sha256(content)

        if tag:
            file_path = os.path.join(DOCS_DIR, api_name, tag, f"{slug}.md")
            cache_key = f"{api_name}:{tag}:{slug}"
        else:
            file_path = os.path.join(DOCS_DIR, api_name, f"{slug}.md")
            cache_key = f"{api_name}::{slug}"

        api_tags.setdefault(api_name, {}).setdefault(tag, []).append({
            "slug": slug,
            "title": title,
            "tag": tag,
        })

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(file_path)
            write_file(file_path, content, dry_run=args.dry_run, verbose=args.verbose,
                       label="ADD" if is_new else "UPDATE")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

    # Write tag READMEs
    for api_name, tags in api_tags.items():
        for tag, pages in tags.items():
            if not tag:
                continue
            readme_content = build_tag_readme(api_name, tag, pages)
            readme_path = os.path.join(DOCS_DIR, api_name, tag, "README.md")
            cache_key = f"{api_name}:{tag}:README"
            content_hash = sha256(readme_content)

            if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
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

    # Write per-API READMEs
    for api_name, tags in api_tags.items():
        readme_content = build_api_readme(api_name, tags)
        readme_path = os.path.join(DOCS_DIR, api_name, "README.md")
        cache_key = f"{api_name}::README"
        content_hash = sha256(readme_content)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
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

    # Write top-level README
    api_counts = {}
    for api_name, tags in api_tags.items():
        api_counts[api_name] = sum(len(pages) for pages in tags.values())
    top_readme = build_top_readme(api_counts)
    top_readme_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)
        with open(top_readme_path, "w") as f:
            f.write(top_readme)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key not in new_cache:
            parts = old_key.split(":")
            if len(parts) == 3:
                api_name, tag, slug = parts
                if tag:
                    old_path = os.path.join(DOCS_DIR, api_name, tag, f"{slug}.md")
                else:
                    old_path = os.path.join(DOCS_DIR, api_name, f"{slug}.md")
                if os.path.exists(old_path):
                    if args.dry_run:
                        rel = os.path.relpath(old_path, DOCS_DIR)
                        print(f"  REMOVE {rel}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            rel = os.path.relpath(old_path, DOCS_DIR)
                            print(f"  REMOVE {rel}")
                    removed += 1

    # Clean up empty directories
    if not args.dry_run:
        for api_name in API_PREFIXES.values():
            api_dir = os.path.join(DOCS_DIR, api_name)
            if not os.path.isdir(api_dir):
                continue
            for entry in os.scandir(api_dir):
                if entry.is_dir() and not os.listdir(entry.path):
                    os.rmdir(entry.path)
                    if args.verbose:
                        print(f"  RMDIR {entry.name}/")

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    total_pages = sum(len(pages) for tags in api_tags.values() for pages in tags.values())
    total_tags = sum(1 for tags in api_tags.values() for t in tags if t)

    print(f"\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Total files: {added + updated + unchanged}")
    print(f"  Total tags:  {total_tags}")
    print(f"  Total pages: {total_pages}")
    for api_name in sorted(api_tags):
        count = sum(len(p) for p in api_tags[api_name].values())
        print(f"    {api_name}: {count}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Auth0 API docs and convert to local markdown"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-generate everything ignoring cache",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Detailed per-file logging"
    )
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
