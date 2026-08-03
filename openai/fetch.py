#!/usr/bin/env python3

"""
OpenAI Developers Documentation Fetcher

Fetches https://developers.openai.com pages as markdown. The site exposes a
top-level /llms.txt discovery index that lists every doc page across the API,
Ads, Apps SDK, Codex, and Agentic Commerce products. Each entry is a .md URL
that serves clean markdown, so we parse llms.txt for the URL list and fetch
each page directly.

URL paths are mirrored into docs/ verbatim, so a page at
  https://developers.openai.com/api/docs/guides/agents.md
lands at
  docs/api/docs/guides/agents.md
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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SITE = "https://developers.openai.com"
LLMS_INDEX_URL = f"{SITE}/llms.txt"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

MAX_WORKERS = 30


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60, etag: str | None = None) -> tuple[str | None, str | None, bool]:
    headers = {
        "User-Agent": "openai-api-docs-fetcher/1.0",
        "Accept-Encoding": "gzip",
    }
    if etag:
        headers["If-None-Match"] = etag
    req = Request(url, headers=headers)
    try:
        with urlopen(req, timeout=timeout) as resp:
            data: bytes = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8"), resp.headers.get("ETag"), False
    except HTTPError as e:
        if e.code == 304:
            return None, e.headers.get("ETag") or etag, True
        if e.code == 404:
            return None, None, False
        print(f"ERROR: {url}: HTTP {e.code}", file=sys.stderr)
        return None, None, False
    except (URLError, TimeoutError, OSError) as e:
        print(f"ERROR: {url}: {e}", file=sys.stderr)
        return None, None, False


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            data = json.load(f)
            return data if isinstance(data, dict) else {}
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

LINK_RE = re.compile(r"^- \[([^\]]+)\]\((https://developers\.openai\.com/[^)]+)\)\s*(?::\s*(.*))?$")


def parse_llms_index(text: str) -> list[dict]:
    """Parse llms.txt into a list of {section, title, url, summary} entries.

    Sections are introduced by '## Heading' lines. Each item is:
        - [Title](https://developers.openai.com/path.md): optional summary
    """
    section = ""
    entries: list[dict] = []
    for line in text.splitlines():
        if line.startswith("## "):
            section = line[3:].strip()
            continue
        m = LINK_RE.match(line)
        if m:
            entries.append(
                {
                    "section": section,
                    "title": m.group(1).strip(),
                    "url": m.group(2).strip(),
                    "summary": (m.group(3) or "").strip(),
                }
            )
    return entries


def url_rel_path(url: str) -> str:
    """Return the URL path relative to SITE, e.g. 'api/docs/guides/agents.md'."""
    return url[len(SITE) + 1 :]


def file_path_for_url(url: str) -> str:
    """Map a .md URL to its on-disk path under DOCS_DIR."""
    rel = url_rel_path(url)
    return os.path.join(DOCS_DIR, *rel.split("/"))


def top_segment(url: str) -> str:
    """First path segment, e.g. 'api', 'ads', 'apps-sdk', 'codex', 'commerce'."""
    return url_rel_path(url).split("/", 1)[0]


# ---------------------------------------------------------------------------
# Page formatting
# ---------------------------------------------------------------------------


def build_page_markdown(raw: str, title: str, source_url: str) -> str:
    body = raw.rstrip() + "\n"
    has_h1 = re.match(r"^# .+", body, flags=re.MULTILINE)
    preamble: list[str] = []
    if not has_h1 and title:
        preamble.append(f"# {title}")
        preamble.append("")
    preamble.append(f"*Source: [{source_url}]({source_url})*")
    preamble.append("")
    return "\n".join(preamble) + body


def display_product(top: str) -> str:
    return {
        "api": "OpenAI API",
        "ads": "Ads",
        "apps-sdk": "Apps SDK",
        "codex": "Codex",
        "commerce": "Agentic Commerce",
    }.get(top, top.replace("-", " ").title())


def build_top_readme(entries: list[dict], by_top: dict[str, list[dict]]) -> str:
    lines = ["# OpenAI Developers Documentation", ""]
    lines.append(f"*Mirrored from [{SITE}]({SITE}/).*")
    lines.append("")
    lines.append(f"{len(entries)} pages across {len(by_top)} products.")
    lines.append("")
    lines.append("## Products")
    lines.append("")
    for top in sorted(by_top):
        count = len(by_top[top])
        lines.append(f"- [{display_product(top)}](./{top}/) ({count} pages)")
    lines.append("")
    lines.append("## All pages by section")
    lines.append("")
    sections: dict[str, list[dict]] = {}
    for e in entries:
        sections.setdefault(e["section"] or "(unsectioned)", []).append(e)
    for section in sorted(sections):
        lines.append(f"### {section}")
        lines.append("")
        for e in sorted(sections[section], key=lambda x: x["title"].lower()):
            rel = url_rel_path(e["url"])
            line = f"- [{e['title']}](./{rel})"
            if e["summary"]:
                line += f" - {e['summary']}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


def build_product_readme(top: str, entries: list[dict]) -> str:
    lines = [f"# {display_product(top)}", ""]
    lines.append(f"*Mirrored from [{SITE}/{top}/]({SITE}/{top}/).*")
    lines.append("")
    lines.append(f"{len(entries)} pages.")
    lines.append("")
    sections: dict[str, list[dict]] = {}
    for e in entries:
        sections.setdefault(e["section"] or "(unsectioned)", []).append(e)
    for section in sorted(sections):
        lines.append(f"## {section}")
        lines.append("")
        for e in sorted(sections[section], key=lambda x: x["title"].lower()):
            # path relative to docs/{top}/
            rel_from_top = (
                url_rel_path(e["url"])[len(top) + 1 :]
                if url_rel_path(e["url"]).startswith(top + "/")
                else url_rel_path(e["url"])
            )
            line = f"- [{e['title']}](./{rel_from_top})"
            if e["summary"]:
                line += f" - {e['summary']}"
            lines.append(line)
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print("Fetching llms.txt...")
    index_text, _, _ = fetch_url(LLMS_INDEX_URL)
    if not index_text:
        print("ERROR: failed to fetch llms.txt", file=sys.stderr)
        sys.exit(1)

    entries = parse_llms_index(index_text)
    # Keep only .md doc pages; the index also lists .txt sub-indexes and the
    # combined llms-full.txt files, which we don't mirror.
    doc_entries = [e for e in entries if e["url"].endswith(".md")]
    # Dedupe by URL: the same page can appear in "Documentation sets" and a
    # product section (e.g. apps-sdk/reference.md). Keep the more specific
    # (non-"Documentation sets") section if both exist.
    by_url: dict[str, dict] = {}
    for e in doc_entries:
        existing = by_url.get(e["url"])
        if existing is None or existing["section"] == "Documentation sets":
            by_url[e["url"]] = e
    doc_entries = list(by_url.values())

    by_top: dict[str, list[dict]] = {}
    for e in doc_entries:
        by_top.setdefault(top_segment(e["url"]), []).append(e)

    print(f"  doc pages: {len(doc_entries)}")
    for top in sorted(by_top):
        print(f"    {top}: {len(by_top[top])}")

    print(f"Fetching {len(doc_entries)} pages (concurrency={MAX_WORKERS})...")

    fetched: dict[str, tuple[str, str | None]] = {}
    validated: dict[str, str | None] = {}
    missing: list[str] = []

    def fetch_one(entry: dict) -> tuple[dict, str | None, str | None, bool]:
        url = entry["url"]
        cache_key = url_rel_path(url)
        file_path = file_path_for_url(url)
        etag = None
        if os.path.exists(file_path):
            etag = cache.get(cache_key, {}).get("etag")
        content, response_etag, not_modified = fetch_url(url, etag=etag)
        return entry, content, response_etag, not_modified

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, e) for e in doc_entries]
        for fut in as_completed(futures):
            entry, content, etag, not_modified = fut.result()
            if not_modified:
                validated[entry["url"]] = etag
            elif content is None:
                missing.append(entry["url"])
            else:
                fetched[entry["url"]] = (content, etag)

    print(f"  fetched: {len(fetched)}; ETag unchanged: {len(validated)}")
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

    for entry in doc_entries:
        url = entry["url"]
        file_path = file_path_for_url(url)
        cache_key = url_rel_path(url)
        prev = cache.get(cache_key, {})

        if url in validated:
            unchanged += 1
            new_cache[cache_key] = {
                **prev,
                "etag": validated[url],
            }
            continue

        fetched_page = fetched.get(url)
        if fetched_page is None:
            # A listed page can transiently fail or briefly return 404 during
            # a deployment. Preserve the last known-good file and cache entry.
            if prev and os.path.exists(file_path):
                unchanged += 1
                new_cache[cache_key] = prev
            continue
        raw, etag = fetched_page
        content = build_page_markdown(raw, entry["title"], url)
        content_hash = sha256(content)

        if prev.get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[cache_key] = {**prev, "etag": etag}
            continue

        is_new = cache_key not in cache or not os.path.exists(file_path)
        label = "ADD" if is_new else "UPDATE"
        write_file(file_path, content, dry_run=args.dry_run, verbose=args.verbose, label=label)
        new_cache[cache_key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(UTC).isoformat(),
            "etag": etag,
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Per-product README
    for top, top_entries in by_top.items():
        readme_content = build_product_readme(top, top_entries)
        readme_path = os.path.join(DOCS_DIR, top, "README.md")
        cache_key = f"__readme__/{top}"
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
    top_readme = build_top_readme(doc_entries, by_top)
    top_path = os.path.join(DOCS_DIR, "README.md")
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

    # Removals
    removed = 0
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        if old_key.startswith("__readme__/"):
            name = old_key[len("__readme__/") :]
            if name == "_top":
                old_path = os.path.join(DOCS_DIR, "README.md")
            else:
                old_path = os.path.join(DOCS_DIR, name, "README.md")
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
    print(f"  Products:    {len(by_top)}")
    print(f"  Total pages: {len(doc_entries)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch OpenAI Developers documentation and mirror to local markdown"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
