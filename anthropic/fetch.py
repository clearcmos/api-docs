#!/usr/bin/env python3

"""
Anthropic Claude Code Documentation Fetcher

Discovers all documentation pages from code.claude.com/docs/llms.txt
and converts them into organized local markdown files grouped by category.
"""

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://code.claude.com/docs/en"
INDEX_URL = "https://code.claude.com/docs/llms.txt"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
MAX_WORKERS = 30


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 30) -> str | None:
    req = Request(url, headers={
        "User-Agent": "anthropic-docs-fetcher/1.0",
        "Accept": "text/html,text/plain,text/markdown,*/*",
        "Accept-Encoding": "gzip",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        print(f"  ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
        return None


def discover_pages() -> list[str]:
    """Fetch llms.txt and extract all documentation page URLs.

    Returns URLs with .md extension, which serve raw markdown content.
    """
    raw = fetch_url(INDEX_URL)
    if not raw:
        print("ERROR: Could not fetch llms.txt index", file=sys.stderr)
        sys.exit(1)

    urls = []
    for line in raw.splitlines():
        line = line.strip()
        # Match URLs like https://code.claude.com/docs/en/...
        for match in re.finditer(r"https://code\.claude\.com/docs/en/[\w./-]+", line):
            url = match.group(0)
            # Ensure URL ends with .md (the site serves raw markdown at .md URLs)
            if not url.endswith(".md"):
                url += ".md"
            urls.append(url)

    # Deduplicate while preserving order
    seen = set()
    unique = []
    for url in urls:
        if url not in seen:
            seen.add(url)
            unique.append(url)

    return unique


def url_to_category_and_slug(url: str) -> tuple[str, str]:
    """Extract category and slug from a URL.

    Examples:
        .../en/overview.md            -> ("general", "overview")
        .../en/agent-sdk/overview.md  -> ("agent-sdk", "overview")
        .../en/whats-new/2026-w13.md  -> ("whats-new", "2026-w13")
        .../en/whats-new/index.md     -> ("whats-new", "index")
    """
    path = url.replace(f"{BASE_URL}/", "")
    # Strip .md extension
    path = re.sub(r"\.md$", "", path)
    parts = path.strip("/").split("/")
    if len(parts) >= 2:
        return parts[0], "/".join(parts[1:])
    return "general", parts[0]


def strip_html_tags(text: str) -> str:
    """Remove HTML tags from text."""
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text)


def clean_markdown(raw_md: str) -> str:
    """Clean up fetched markdown content.

    Removes the docs-index blockquote header, strips JSX component tags
    while preserving their content, and normalizes formatting.
    """
    lines = raw_md.splitlines()
    cleaned = []
    skip_header = True

    for line in lines:
        # Skip the leading docs-index blockquote until we hit the first H1
        if skip_header:
            if line.startswith("# "):
                skip_header = False
            else:
                continue
        cleaned.append(line)

    result = "\n".join(cleaned).strip()

    # Strip JSX-style component tags but keep their content
    # e.g. <Tabs>, <Tab title="...">, <Accordion>, <Info>, <Tip>, <Warning>, etc.
    result = re.sub(r"<(Tabs|Tab|Accordion|AccordionGroup|Info|Tip|Warning|Note|Steps|Step|Card|CardGroup|CodeGroup|Callout|Frame)[^>]*/?>", "", result)
    result = re.sub(r"</(Tabs|Tab|Accordion|AccordionGroup|Info|Tip|Warning|Note|Steps|Step|Card|CardGroup|CodeGroup|Callout|Frame)>", "", result)

    # Remove excessive blank lines (3+ -> 2)
    result = re.sub(r"\n{3,}", "\n\n", result)

    return result


def fetch_page_content(url: str, verbose: bool = False) -> str | None:
    """Fetch a documentation page and extract its markdown content."""
    raw = fetch_url(url)
    if raw is None:
        return None

    content = clean_markdown(raw)

    if not content:
        if verbose:
            print(f"  WARNING: No content extracted from {url}", file=sys.stderr)
        return None

    return content


def build_category_readme(category: str, pages: list[dict]) -> str:
    """Build a README.md index for a category."""
    # Use the category name as title, prettified
    title = category.replace("-", " ").title()
    if category == "general":
        title = "Claude Code Documentation"
    elif category == "agent-sdk":
        title = "Agent SDK"

    lines = [f"# {title}\n"]
    lines.append("## Pages\n")
    for page in sorted(pages, key=lambda p: p["slug"]):
        name = page["title"] or page["slug"].replace("-", " ").title()
        lines.append(f"- [{name}](./{page['filename']})")
    lines.append("")
    return "\n".join(lines)


def build_main_readme(categories: dict[str, list[dict]]) -> str:
    """Build the top-level docs/README.md."""
    lines = ["# Anthropic Claude Code Documentation\n"]
    lines.append("Documentation fetched from [code.claude.com/docs](https://code.claude.com/docs/en/overview).\n")
    lines.append("## Categories\n")

    # Put general first, then alphabetical
    ordered = []
    if "general" in categories:
        ordered.append("general")
    for cat in sorted(categories.keys()):
        if cat != "general":
            ordered.append(cat)

    for cat in ordered:
        pages = categories[cat]
        display = cat.replace("-", " ").title()
        if cat == "general":
            display = "General"
        elif cat == "agent-sdk":
            display = "Agent SDK"
        lines.append(f"- [{display}](./{cat}/) ({len(pages)} pages)")
    lines.append("")
    return "\n".join(lines)


def extract_title(content: str) -> str:
    """Extract the first H1 heading from markdown content."""
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    # Discover all pages
    print("Discovering documentation pages from llms.txt...")
    urls = discover_pages()
    print(f"  Found {len(urls)} pages")

    # Group by category
    page_plan: dict[str, list[dict]] = {}
    for url in urls:
        category, slug = url_to_category_and_slug(url)
        if category not in page_plan:
            page_plan[category] = []
        filename = slug.replace("/", "-") + ".md"
        page_plan[category].append({
            "url": url,
            "slug": slug,
            "filename": filename,
            "title": "",
        })

    categories = sorted(page_plan.keys())
    print(f"  Categories: {', '.join(categories)}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added_items: list[str] = []
    updated_items: list[str] = []
    removed_items: list[str] = []
    unchanged = 0
    errors = 0
    new_cache = {}

    total_pages = sum(len(pages) for pages in page_plan.values())
    work_items: list[tuple[str, dict]] = []

    for category in categories:
        pages = page_plan[category]
        cat_dir = os.path.join(DOCS_DIR, category)

        if not args.dry_run:
            os.makedirs(cat_dir, exist_ok=True)

        work_items.extend((category, page) for page in pages)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_item = {
            executor.submit(
                fetch_page_content, page["url"], args.verbose
            ): (category, page)
            for category, page in work_items
        }

        for fetched, future in enumerate(as_completed(future_to_item), start=1):
            category, page = future_to_item[future]
            cat_dir = os.path.join(DOCS_DIR, category)
            cache_key = f"{category}:{page['filename']}"

            # Check cache first -- if not force, we still need to fetch to
            # compare content, but we can check if the URL was seen before
            if args.verbose or fetched % 10 == 0:
                print(f"  [{fetched}/{total_pages}] {category}/{page['slug']}")

            content = future.result()
            if content is None:
                errors += 1
                # Keep old cache entry if it exists
                if cache_key in cache:
                    new_cache[cache_key] = cache[cache_key]
                continue

            page["title"] = extract_title(content)
            content_hash = sha256(content)

            if (
                cache.get(cache_key, {}).get("sha256") == content_hash
                and os.path.exists(os.path.join(cat_dir, page["filename"]))
            ):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
                # Preserve title from content for README generation
                continue

            is_new = cache_key not in cache or not os.path.exists(
                os.path.join(cat_dir, page["filename"])
            )

            if args.dry_run:
                print(f"  {'ADD' if is_new else 'UPDATE'} {category}/{page['filename']}")
            else:
                with open(os.path.join(cat_dir, page["filename"]), "w") as f:
                    f.write(content)
                    f.write("\n")
                if args.verbose:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {category}/{page['filename']}")

            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "title": page["title"],
                "url": page["url"],
            }

            label = page["title"] or page["slug"]
            location = f"{category}/{page['filename']}"
            entry = f"{label} ({location})"
            if is_new:
                added_items.append(entry)
            else:
                updated_items.append(entry)

    # For pages where we skipped fetching (cache hit), restore title from cache
    for category in categories:
        for page in page_plan[category]:
            if not page["title"]:
                cache_key = f"{category}:{page['filename']}"
                page["title"] = new_cache.get(cache_key, {}).get(
                    "title", cache.get(cache_key, {}).get("title", "")
                )

    # Write category READMEs
    for category in categories:
        pages = page_plan[category]
        readme_content = build_category_readme(category, pages)
        readme_path = os.path.join(DOCS_DIR, category, "README.md")
        cache_key = f"{category}:README.md"
        content_hash = sha256(readme_content)

        if (
            cache.get(cache_key, {}).get("sha256") == content_hash
            and os.path.exists(readme_path)
        ):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(readme_path)
            if args.dry_run:
                print(f"  {'ADD' if is_new else 'UPDATE'} {category}/README.md")
            else:
                with open(readme_path, "w") as f:
                    f.write(readme_content)
                if args.verbose:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {category}/README.md")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            entry = f"{category} index ({category}/README.md)"
            if is_new:
                added_items.append(entry)
            else:
                updated_items.append(entry)

    # Write top-level README
    main_content = build_main_readme(page_plan)
    main_readme_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        with open(main_readme_path, "w") as f:
            f.write(main_content)

    # Detect removals
    for old_key in sorted(cache):
        if old_key not in new_cache:
            parts = old_key.split(":", 1)
            if len(parts) == 2:
                old_path = os.path.join(DOCS_DIR, parts[0], parts[1])
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE {parts[0]}/{parts[1]}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE {parts[0]}/{parts[1]}")
                    old_title = cache.get(old_key, {}).get("title", "")
                    label = old_title or parts[1].removesuffix(".md")
                    removed_items.append(f"{label} ({parts[0]}/{parts[1]})")

    # Clean up empty directories
    if not args.dry_run:
        for entry in os.scandir(DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)
                if args.verbose:
                    print(f"  RMDIR {entry.name}/")

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    print(f"\nSync complete:")
    print(f"  Added:      {len(added_items)}")
    print(f"  Updated:    {len(updated_items)}")
    print(f"  Unchanged:  {unchanged}")
    print(f"  Errors:     {errors}")
    print(f"  Removed:    {len(removed_items)}")
    print(f"  Categories: {len(categories)}")
    print(f"  Total pages: {total_pages}")

    def print_section(label: str, items: list[str]) -> None:
        if not items:
            return
        print(f"\n{label}:")
        for item in sorted(items, key=str.lower):
            print(f"  - {item}")

    print_section("Added pages", added_items)
    print_section("Updated pages", updated_items)
    print_section("Removed pages", removed_items)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Anthropic Claude Code docs and convert to local markdown"
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
