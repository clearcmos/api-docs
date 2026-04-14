#!/usr/bin/env python3

import argparse
import hashlib
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DISCOVERY_URL = "https://discovery.googleapis.com/discovery/v1/apis"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
DEPRECATED_DIR = os.path.join(DOCS_DIR, "deprecated")
INDEX_FILE = os.path.join(SCRIPT_DIR, "google-api-discovery.json")
MAX_WORKERS = 30


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 30) -> str | None:
    """Fetch URL content. Returns None on failure."""
    req = Request(url, headers={"User-Agent": "sync-google-api-docs/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        return None


def api_id_to_filename(api_id: str) -> str:
    """Convert API id like 'meet:v1' or 'admin:directory_v1' to a filename.

    Single-version APIs: {name}.md
    Multi-version APIs: {name}-{version}.md  (colon replaced with dash)
    """
    return api_id.replace(":", "-") + ".md"



def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def fetch_directory() -> list[dict]:
    """Fetch the Google API discovery directory."""
    raw = fetch_url(DISCOVERY_URL)
    if not raw:
        print("ERROR: Failed to fetch discovery directory", file=sys.stderr)
        sys.exit(1)
    data = json.loads(raw)
    return data.get("items", [])


def select_apis(items: list[dict]) -> dict[str, dict]:
    """Build dict of api_id -> directory item for all APIs."""
    return {item["id"]: item for item in items}


def fetch_discovery_doc(item: dict, verbose: bool) -> tuple[str, str | None, dict]:
    """Fetch a single discovery doc. Returns (api_id, content_or_none, item)."""
    api_id = item["id"]
    url = item["discoveryRestUrl"]
    if verbose:
        print(f"  Fetching {api_id} from {url}")

    content = fetch_url(url)
    if not content:
        if verbose:
            print(f"  SKIP {api_id}: fetch failed")
        return api_id, None, item

    # Validate it's a discovery doc
    try:
        doc = json.loads(content)
    except json.JSONDecodeError:
        if verbose:
            print(f"  SKIP {api_id}: invalid JSON")
        return api_id, None, item

    if doc.get("kind") != "discovery#restDescription":
        if verbose:
            print(f"  SKIP {api_id}: not a discovery doc (kind={doc.get('kind')})")
        return api_id, None, item

    # Re-serialize with consistent formatting (sort_keys for stable hashing)
    formatted = json.dumps(doc, indent=2, sort_keys=True)
    return api_id, formatted, item


def build_index(selected: dict[str, dict]) -> str:
    """Build the google-api-discovery.json index from selected APIs."""
    entries = []
    for api_id, item in sorted(selected.items()):
        url = item["discoveryRestUrl"]
        # Extract subdomain from URL
        subdomain = url.split("//")[1].split(".")[0] if "//" in url else ""
        entries.append(
            {
                "subdomain": subdomain,
                "url": url,
                "version": item["version"],
                "title": item.get("title", ""),
                "description": item.get("description", ""),
            }
        )
    return json.dumps(entries, indent=2) + "\n"


def sync(args: argparse.Namespace) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)

    # Load cache
    cache = {} if args.force else load_cache()

    # Fetch directory
    print("Fetching API discovery directory...")
    items = fetch_directory()
    print(f"  Found {len(items)} API entries")

    # Select APIs (all entries — each has a unique id)
    selected = select_apis(items)
    print(f"  Selected {len(selected)} APIs to sync")

    # Build reverse map: filename -> api_id from cache
    cached_files: dict[str, str] = {}
    for api_id in cache:
        cached_files[api_id_to_filename(api_id)] = api_id

    # Fetch all discovery docs in parallel
    print("Fetching discovery documents...")
    results: dict[str, tuple[str | None, dict]] = {}
    skipped = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {
            pool.submit(fetch_discovery_doc, item, args.verbose): api_id
            for api_id, item in selected.items()
        }
        for future in as_completed(futures):
            api_id, content, item = future.result()
            if content is None:
                skipped += 1
            results[api_id] = (content, item)

    # Process results
    added = 0
    updated = 0
    unchanged = 0
    failed = skipped
    new_cache = {}

    for api_id in sorted(results):
        content, item = results[api_id]
        if content is None:
            continue

        filename = api_id_to_filename(api_id)
        filepath = os.path.join(DOCS_DIR, filename)
        content_hash = sha256(content)
        revision = json.loads(content).get("revision", "")

        # Check cache
        cached = cache.get(api_id, {})
        if cached.get("sha256") == content_hash and os.path.exists(filepath):
            unchanged += 1
            new_cache[api_id] = cached
            if args.verbose:
                print(f"  UNCHANGED {api_id}")
            continue

        # Content changed or new
        is_new = api_id not in cache or not os.path.exists(filepath)
        action = "ADD" if is_new else "UPDATE"

        if args.dry_run:
            print(f"  {action} {filename}")
        else:
            with open(filepath, "w") as f:
                f.write(content)
                f.write("\n")
            if args.verbose or is_new:
                print(f"  {action} {filename}")

        new_cache[api_id] = {
            "url": item["discoveryRestUrl"],
            "sha256": content_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "revision": revision,
        }

        if is_new:
            added += 1
        else:
            updated += 1

    # Detect removals: cached APIs no longer in directory
    deprecated = 0
    current_ids = set(results.keys())
    for old_id in sorted(cache):
        if old_id not in current_ids:
            filename = api_id_to_filename(old_id)
            src = os.path.join(DOCS_DIR, filename)
            if os.path.exists(src):
                if args.dry_run:
                    print(f"  DEPRECATE {filename}")
                else:
                    os.makedirs(DEPRECATED_DIR, exist_ok=True)
                    dst = os.path.join(DEPRECATED_DIR, filename)
                    os.rename(src, dst)
                    print(f"  DEPRECATE {filename} -> deprecated/")
                deprecated += 1

    # Write index
    index_content = build_index(
        {k: v for k, (content, v) in results.items() if content is not None}
    )
    if not args.dry_run:
        with open(INDEX_FILE, "w") as f:
            f.write(index_content)

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    # Summary
    print(f"\nSync complete:")
    print(f"  Added:      {added}")
    print(f"  Updated:    {updated}")
    print(f"  Unchanged:  {unchanged}")
    print(f"  Deprecated: {deprecated}")
    print(f"  Failed:     {failed}")
    total = added + updated + unchanged
    print(f"  Total docs: {total}")


def main():
    parser = argparse.ArgumentParser(
        description="Sync Google API discovery documents to .md files"
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change without writing files",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-download everything ignoring cache",
    )
    parser.add_argument(
        "--verbose", action="store_true", help="Detailed per-API logging"
    )
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
