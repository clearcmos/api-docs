#!/usr/bin/env python3

"""
Notion API Documentation Fetcher

Notion does not publish an OpenAPI spec, but the Mintlify-powered docs site
exposes a complete index at https://developers.notion.com/llms.txt and serves
a clean markdown alternate for every page at <url>.md. Reference endpoint
pages also embed a per-endpoint OpenAPI YAML block in the markdown, so the
.md alternates are effectively spec + prose in one document.

This fetcher:
  1. Downloads llms.txt as the master index
  2. Parses each `- [Title](url.md): description` entry
  3. Concurrently fetches every .md URL
  4. Writes one file per page under docs/{group}[/subgroup]/{slug}.md
  5. Generates README.md indexes per directory
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

LLMS_INDEX_URL = "https://developers.notion.com/llms.txt"
BASE_URL = "https://developers.notion.com/"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

MAX_WORKERS = 24


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": "notion-api-docs-fetcher/1.0",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data: bytes = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        print(f"ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
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
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        if verbose:
            print(f"  {label} {rel}")


# ---------------------------------------------------------------------------
# Index parsing
# ---------------------------------------------------------------------------

ENTRY_RE = re.compile(
    r"^- \[(?P<title>[^\]]+)\]\((?P<url>https://developers\.notion\.com/[^)]+\.md)\)(?:: (?P<desc>.*))?$"
)


def parse_index(index_text: str) -> list[dict]:
    """Parse llms.txt into a list of {title, url, description} entries."""
    entries = []
    for line in index_text.split("\n"):
        m = ENTRY_RE.match(line.strip())
        if m:
            entries.append(
                {
                    "title": m.group("title").strip(),
                    "url": m.group("url").strip(),
                    "description": (m.group("desc") or "").strip(),
                }
            )
    return entries


def classify(url: str) -> tuple[str, str, str]:
    """Return (group, subgroup, slug) for a doc URL.

    Examples:
      reference/intro.md           -> ("reference", "", "intro")
      compliance/overview.md       -> ("compliance", "", "overview")
      guides/mcp/overview.md       -> ("guides", "mcp", "overview")
      page/changelog.md            -> ("page", "", "changelog")
    """
    path = url[len(BASE_URL) :].removesuffix(".md")
    parts = path.split("/")
    if len(parts) == 2:
        return parts[0], "", parts[1]
    if len(parts) == 3:
        return parts[0], parts[1], parts[2]
    # Fallback: stuff everything after the first segment into slug
    return parts[0], "", "-".join(parts[1:]) or "index"


# ---------------------------------------------------------------------------
# Page formatting
# ---------------------------------------------------------------------------


def build_page_markdown(raw: str, entry: dict) -> str:
    """Prepend a small frontmatter header to the raw markdown alternate."""
    header = [
        f"# {entry['title']}",
        "",
        f"*Source: [{entry['url']}]({entry['url']})*",
    ]
    if entry["description"]:
        header.extend(["", entry["description"]])
    header.extend(["", "---", ""])

    body = raw.strip()
    # Notion's .md alternates often start with a `>` blockquote that just
    # repeats "Documentation Index ... fetch llms.txt ..." - strip that prelude
    # if present so the page opens with its actual content.
    body = re.sub(
        r"^> ## Documentation Index\n(?:> [^\n]*\n)*\n*",
        "",
        body,
    )
    # Some pages also lead with a blockquote summary that duplicates the
    # description we already put in the header. Leave it - it's the page lede.
    return "\n".join(header) + body.strip() + "\n"


# ---------------------------------------------------------------------------
# README builders
# ---------------------------------------------------------------------------


def humanize(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").title()


def build_dir_readme(title: str, entries: list[dict], subdirs: list[str]) -> str:
    lines = [f"# {humanize(title)}", ""]
    if subdirs:
        lines.append("## Subcategories")
        lines.append("")
        for sub in sorted(subdirs):
            lines.append(f"- [{humanize(sub)}](./{sub}/)")
        lines.append("")
    if entries:
        lines.append("## Pages")
        lines.append("")
        for e in sorted(entries, key=lambda x: x["slug"]):
            link = f"./{e['slug']}.md"
            if e["description"]:
                lines.append(f"- [{e['title']}]({link}) - {e['description']}")
            else:
                lines.append(f"- [{e['title']}]({link})")
        lines.append("")
    return "\n".join(lines)


def build_top_readme(group_counts: dict[str, int]) -> str:
    lines = [
        "# Notion API Documentation",
        "",
        f"*Generated from {LLMS_INDEX_URL}*",
        "",
        "## Sections",
        "",
    ]
    for group in sorted(group_counts):
        lines.append(f"- [{humanize(group)}](./{group}/) ({group_counts[group]} pages)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print(f"Fetching index from {LLMS_INDEX_URL}...")
    index_text = fetch_url(LLMS_INDEX_URL)
    if not index_text:
        sys.exit(1)

    entries = parse_index(index_text)
    print(f"  Found {len(entries)} pages in index")

    # Classify each entry
    classified: list[dict] = []
    for e in entries:
        group, subgroup, slug = classify(e["url"])
        classified.append({**e, "group": group, "subgroup": subgroup, "slug": slug})

    # Concurrently fetch every .md URL
    print(f"Fetching {len(classified)} pages (workers={MAX_WORKERS})...")
    contents: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
        futures = {ex.submit(fetch_url, e["url"]): e["url"] for e in classified}
        for fut in as_completed(futures):
            url = futures[fut]
            try:
                body = fut.result()
            except Exception as exc:
                print(f"ERROR: {url}: {exc}", file=sys.stderr)
                continue
            if body is not None:
                contents[url] = body

    print(f"  Fetched {len(contents)}/{len(classified)} pages successfully")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}

    # Track structure for README generation
    # {group: {subgroup: [entries]}} - subgroup="" means flat in the group dir
    structure: dict[str, dict[str, list[dict]]] = {}

    for e in classified:
        raw = contents.get(e["url"])
        parts = [DOCS_DIR, e["group"]]
        if e["subgroup"]:
            parts.append(e["subgroup"])
        parts.append(f"{e['slug']}.md")
        file_path = os.path.join(*parts)

        cache_key = f"{e['group']}:{e['subgroup']}:{e['slug']}"
        structure.setdefault(e["group"], {}).setdefault(e["subgroup"], []).append(e)

        if raw is None:
            if cache_key in cache and os.path.exists(file_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                structure[e["group"]][e["subgroup"]].pop()
            continue

        content = build_page_markdown(raw, e)
        content_hash = sha256(content)
        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
            continue

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

    structure = {
        group: {sub: items for sub, items in subs.items() if items}
        for group, subs in structure.items()
        if any(subs.values())
    }

    # Per-subgroup READMEs
    for group, subs in structure.items():
        for sub, items in subs.items():
            if not sub:
                continue
            readme = build_dir_readme(sub, items, subdirs=[])
            path = os.path.join(DOCS_DIR, group, sub, "README.md")
            cache_key = f"{group}:{sub}:_README"
            h = sha256(readme)
            if cache.get(cache_key, {}).get("sha256") == h and os.path.exists(path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                is_new = cache_key not in cache or not os.path.exists(path)
                write_file(
                    path,
                    readme,
                    dry_run=args.dry_run,
                    verbose=args.verbose,
                    label="ADD" if is_new else "UPDATE",
                )
                new_cache[cache_key] = {
                    "sha256": h,
                    "last_updated": datetime.now(UTC).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

    # Per-group READMEs
    for group, subs in structure.items():
        flat_entries = subs.get("", [])
        subdir_names = [s for s in subs if s]
        readme = build_dir_readme(group, flat_entries, subdir_names)
        path = os.path.join(DOCS_DIR, group, "README.md")
        cache_key = f"{group}::_README"
        h = sha256(readme)
        if cache.get(cache_key, {}).get("sha256") == h and os.path.exists(path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(path)
            write_file(
                path, readme, dry_run=args.dry_run, verbose=args.verbose, label="ADD" if is_new else "UPDATE"
            )
            new_cache[cache_key] = {
                "sha256": h,
                "last_updated": datetime.now(UTC).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

    # Top-level README
    group_counts = {g: sum(len(v) for v in subs.values()) for g, subs in structure.items()}
    top_readme = build_top_readme(group_counts)
    top_path = os.path.join(DOCS_DIR, "README.md")
    cache_key = "::_README"
    h = sha256(top_readme)
    if cache.get(cache_key, {}).get("sha256") == h and os.path.exists(top_path):
        unchanged += 1
        new_cache[cache_key] = cache[cache_key]
    else:
        is_new = cache_key not in cache or not os.path.exists(top_path)
        write_file(
            top_path,
            top_readme,
            dry_run=args.dry_run,
            verbose=args.verbose,
            label="ADD" if is_new else "UPDATE",
        )
        new_cache[cache_key] = {
            "sha256": h,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        parts = old_key.split(":")
        if len(parts) != 3:
            continue
        group, sub, slug = parts
        if slug == "_README":
            if sub:
                old_path = os.path.join(DOCS_DIR, group, sub, "README.md")
            elif group:
                old_path = os.path.join(DOCS_DIR, group, "README.md")
            else:
                old_path = os.path.join(DOCS_DIR, "README.md")
        else:
            if sub:
                old_path = os.path.join(DOCS_DIR, group, sub, f"{slug}.md")
            else:
                old_path = os.path.join(DOCS_DIR, group, f"{slug}.md")
        if os.path.exists(old_path):
            if args.dry_run:
                print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
            else:
                os.remove(old_path)
                if args.verbose:
                    print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
            removed += 1

    # Clean up empty dirs
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

    total = added + updated + unchanged
    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Total files: {total}")
    for group in sorted(structure):
        count = sum(len(v) for v in structure[group].values())
        print(f"    {group}: {count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Notion API docs and convert to local markdown")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
