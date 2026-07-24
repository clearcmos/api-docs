#!/usr/bin/env python3
# requires-interactive

"""
Terraform Provider Documentation Fetcher

Fetches any Terraform provider's documentation from the Terraform Registry
and saves it as organized markdown files grouped by category (resources,
data-sources, guides).

Requires --provider org/name, or enters interactive provider picker when
run with a TTY and no --provider.

Uses the Terraform Registry v1/v2 API:
  1. v2 paginated endpoint to build a local provider index (cached)
  2. v1 endpoint to discover the latest provider version and doc list
  3. v2 JSON:API endpoint to fetch individual doc content

Examples:
    python fetch.py --provider hashicorp/googleworkspace
    python fetch.py --provider hashicorp/aws --dry-run
    python fetch.py                          # interactive picker
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REGISTRY_V1 = "https://registry.terraform.io/v1/providers"
REGISTRY_V2_PROVIDERS = "https://registry.terraform.io/v2/providers"
REGISTRY_V2_DOCS = "https://registry.terraform.io/v2/provider-docs"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
INDEX_FILE = os.path.join(SCRIPT_DIR, "provider-index.json")
# Raw docs list JSON saved for reference; gitignored via **/provider-docs.json
PROVIDER_DOCS_FILE = os.path.join(SCRIPT_DIR, "provider-docs.json")

INDEX_MAX_AGE_HOURS = 24
MAX_WORKERS = 16


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={
        "User-Agent": "terraform-api-docs-fetcher/1.0",
        "Accept-Encoding": "gzip",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8")
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


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (lines between --- markers at start of file)."""
    if not content or not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return content


def docs_manifest_hash(docs: list[dict]) -> str:
    """Hash the immutable Registry document IDs and output-relevant metadata."""
    manifest = [
        {
            "id": str(doc.get("id", "")),
            "category": doc.get("category", "other"),
            "slug": doc.get("slug", ""),
            "title": doc.get("title", ""),
        }
        for doc in docs
    ]
    manifest.sort(key=lambda item: (
        item["category"], item["slug"], item["id"], item["title"]
    ))
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def load_provider_docs_snapshot() -> dict:
    if not os.path.exists(PROVIDER_DOCS_FILE):
        return {}
    try:
        with open(PROVIDER_DOCS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def cache_is_complete(docs: list[dict], cache: dict) -> bool:
    expected: set[tuple[str, str]] = set()
    categories: set[str] = set()
    for doc in docs:
        category = sanitize_filename(doc.get("category", "other"))
        slug = doc.get("slug", doc.get("title", "untitled"))
        filename = f"{sanitize_filename(slug)}.md"
        expected.add((f"{category}:{filename}",
                      os.path.join(DOCS_DIR, category, filename)))
        categories.add(category)
    for category in categories:
        expected.add((f"{category}:README.md",
                      os.path.join(DOCS_DIR, category, "README.md")))
    return (
        os.path.exists(os.path.join(DOCS_DIR, "README.md"))
        and all(key in cache and os.path.exists(path) for key, path in expected)
    )


def fetch_doc_content(doc_id: str) -> dict | None:
    """Fetch a single doc from the v2 JSON:API endpoint."""
    url = f"{REGISTRY_V2_DOCS}/{doc_id}"
    raw = fetch_url(url)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        return data.get("data", {}).get("attributes", {})
    except (json.JSONDecodeError, KeyError):
        return None


# ---------------------------------------------------------------------------
# Provider index (cached list of all registry providers)
# ---------------------------------------------------------------------------

def _index_is_fresh() -> bool:
    """Check if the cached provider index exists and is recent enough."""
    if not os.path.exists(INDEX_FILE):
        return False
    try:
        with open(INDEX_FILE, "r") as f:
            idx = json.load(f)
        fetched = idx.get("fetched_at", "")
        if not fetched:
            return False
        dt = datetime.fromisoformat(fetched)
        age = datetime.now(timezone.utc) - dt
        return age.total_seconds() < INDEX_MAX_AGE_HOURS * 3600
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def _fetch_index_page(page: int, page_size: int = 100) -> dict | None:
    url = f"{REGISTRY_V2_PROVIDERS}?page[size]={page_size}&page[number]={page}"
    raw = fetch_url(url, timeout=30)
    if not raw:
        return None
    return json.loads(raw)


def refresh_provider_index() -> list[dict]:
    """Fetch all providers from the registry v2 API with pagination."""
    print("Refreshing provider index from Terraform Registry...")

    # First page to get total
    first = _fetch_index_page(1)
    if not first:
        print("ERROR: Could not fetch provider index", file=sys.stderr)
        return []

    meta = first.get("meta", {}).get("pagination", {})
    total_pages = meta.get("total-pages", 1)
    total_count = meta.get("total-count", 0)
    print(f"  {total_count} providers across {total_pages} pages")

    providers = []
    for item in first.get("data", []):
        a = item.get("attributes", {})
        providers.append({
            "name": a.get("full-name", ""),
            "tier": a.get("tier", "community"),
            "downloads": a.get("downloads", 0),
        })

    # Fetch remaining pages concurrently
    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_index_page, p): p for p in range(2, total_pages + 1)}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    for item in result.get("data", []):
                        a = item.get("attributes", {})
                        providers.append({
                            "name": a.get("full-name", ""),
                            "tier": a.get("tier", "community"),
                            "downloads": a.get("downloads", 0),
                        })

    providers.sort(key=lambda p: (-p["downloads"], p["name"]))

    # Save index
    index = {
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "total": len(providers),
        "providers": providers,
    }
    with open(INDEX_FILE, "w") as f:
        json.dump(index, f, indent=2)
        f.write("\n")

    print(f"  Cached {len(providers)} providers to provider-index.json")
    return providers


def load_provider_index(force_refresh: bool = False) -> list[dict]:
    """Load provider index from cache, refreshing if stale or forced."""
    if not force_refresh and _index_is_fresh():
        with open(INDEX_FILE, "r") as f:
            idx = json.load(f)
        return idx.get("providers", [])
    return refresh_provider_index()


# ---------------------------------------------------------------------------
# Interactive provider picker
# ---------------------------------------------------------------------------

def pick_provider_interactive(providers: list[dict]) -> str | None:
    """Let the user pick a provider interactively. Tries fzf, falls back to simple search."""
    # Build display lines: "org/name  (tier, N downloads)"
    lines = []
    for p in providers:
        dl = p["downloads"]
        if dl >= 1_000_000:
            dl_str = f"{dl / 1_000_000:.1f}M"
        elif dl >= 1_000:
            dl_str = f"{dl / 1_000:.0f}K"
        else:
            dl_str = str(dl)
        lines.append(f"{p['name']}  ({p['tier']}, {dl_str} downloads)")

    text = "\n".join(lines)

    # Try fzf first
    try:
        result = subprocess.run(
            ["fzf", "--prompt", "Provider> ", "--height=40%", "--reverse", "--tiebreak=index"],
            input=text, capture_output=True, text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip().split()[0]
        return None
    except FileNotFoundError:
        pass

    # Fallback: simple search loop
    print(f"\n{len(providers)} providers available. Type to search (empty to list top 20):\n")
    while True:
        try:
            query = input("search> ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print()
            return None

        if not query:
            matches = providers[:20]
        else:
            matches = [p for p in providers if query in p["name"].lower()][:20]

        if not matches:
            print("  No matches. Try again.")
            continue

        for i, p in enumerate(matches, 1):
            print(f"  {i:3d}. {p['name']}  ({p['tier']})")

        if len(matches) == 1:
            confirm = input(f"\nUse {matches[0]['name']}? [Y/n] ").strip().lower()
            if confirm in ("", "y", "yes"):
                return matches[0]["name"]
            continue

        try:
            pick = input("\nEnter number (or search again): ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return None

        if pick.isdigit():
            n = int(pick)
            if 1 <= n <= len(matches):
                return matches[n - 1]["name"]

        # Otherwise treat it as a new search
        if pick:
            matches = [p for p in providers if pick.lower() in p["name"].lower()][:20]
            for i, p in enumerate(matches, 1):
                print(f"  {i:3d}. {p['name']}  ({p['tier']})")


# ---------------------------------------------------------------------------
# Markdown formatting
# ---------------------------------------------------------------------------

def build_category_readme(category: str, docs: list[dict]) -> str:
    display = category.replace("-", " ").title()
    lines = [f"# {display}\n"]
    lines.append("## Documentation\n")
    for doc in sorted(docs, key=lambda x: x["title"]):
        lines.append(f"- [{doc['title']}](./{doc['filename']})")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(provider: str, version: str, categories: dict[str, list[dict]]) -> str:
    org, name = provider.split("/", 1) if "/" in provider else ("", provider)
    lines = [f"# Terraform {name.title()} Provider Documentation\n"]
    lines.append(f"**Provider:** [{provider}](https://registry.terraform.io/providers/{provider}/latest/docs)\n")
    lines.append(f"**Version:** {version}\n")
    lines.append("## Categories\n")
    for category in sorted(categories.keys()):
        display = category.replace("-", " ").title()
        safe = sanitize_filename(category)
        count = len(categories[category])
        lines.append(f"- [{display}](./{safe}/) ({count} docs)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    provider = args.provider
    cache = {} if args.force else load_cache()
    previous_snapshot = load_provider_docs_snapshot()

    v1_url = f"{REGISTRY_V1}/{provider}"

    # Step 1: Fetch provider metadata
    print(f"Fetching {provider} provider metadata...")
    raw = fetch_url(v1_url)
    if not raw:
        sys.exit(1)

    provider_info = json.loads(raw)
    latest_version = provider_info.get("version", "")
    print(f"  Latest version: {latest_version}")

    # Step 2: Fetch docs list for that version
    version_url = f"{v1_url}/{latest_version}"
    print(f"Fetching docs list for v{latest_version}...")
    raw = fetch_url(version_url)
    if not raw:
        sys.exit(1)

    version_data = json.loads(raw)
    docs = version_data.get("docs", [])
    print(f"  Found {len(docs)} documentation pages")

    snapshot_provider = (
        f"{previous_snapshot.get('namespace', '')}/"
        f"{previous_snapshot.get('name', '')}"
    ).strip("/")
    same_manifest = (
        not args.force
        and snapshot_provider == provider
        and previous_snapshot.get("version") == latest_version
        and docs_manifest_hash(previous_snapshot.get("docs", []))
            == docs_manifest_hash(docs)
    )
    if same_manifest and cache_is_complete(docs, cache):
        category_count = len({
            sanitize_filename(doc.get("category", "other")) for doc in docs
        })
        file_count = len(cache)
        print("  Manifest unchanged and local cache complete; "
              "skipping individual document downloads")
        print("\nSync complete:")
        print("  Added:        0")
        print("  Updated:      0")
        print(f"  Unchanged:    {file_count}")
        print("  Removed:      0")
        print(f"  Total files:  {file_count}")
        print(f"  Total categories: {category_count}")
        print(f"  Total docs:   {len(docs)}")
        return

    # Organize docs by category
    categories: dict[str, list[dict]] = {}
    for doc in docs:
        category = doc.get("category", "other")
        categories.setdefault(category, []).append(doc)

    print(f"  Categories: {', '.join(sorted(categories.keys()))}")

    # Step 3: Fetch all doc content concurrently
    print("Fetching individual doc content...")
    doc_contents: dict[str, dict] = {}

    def _fetch_one(doc: dict) -> tuple[str, dict | None]:
        doc_id = str(doc.get("id", ""))
        attrs = fetch_doc_content(doc_id)
        return doc_id, attrs

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_fetch_one, doc): doc for doc in docs}
        for future in as_completed(futures):
            doc_id, attrs = future.result()
            if attrs:
                doc_contents[doc_id] = attrs
            else:
                doc = futures[future]
                title = doc.get("title", doc.get("slug", "unknown"))
                print(f"  WARNING: Failed to fetch content for {title}", file=sys.stderr)

    print(f"  Fetched {len(doc_contents)}/{len(docs)} docs")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    new_cache: dict = {}

    category_docs: dict[str, list[dict]] = {}

    for category in sorted(categories.keys()):
        safe_category = sanitize_filename(category)
        category_dir = os.path.join(DOCS_DIR, safe_category)

        if not args.dry_run:
            os.makedirs(category_dir, exist_ok=True)

        for doc in sorted(categories[category], key=lambda x: x.get("title", x.get("slug", ""))):
            doc_id = str(doc.get("id", ""))
            title = doc.get("title", doc.get("slug", "untitled"))
            slug = doc.get("slug", title)
            filename = f"{sanitize_filename(slug)}.md"
            cache_key = f"{safe_category}:{filename}"
            file_path = os.path.join(category_dir, filename)

            attrs = doc_contents.get(doc_id)
            if not attrs:
                if cache_key in cache and os.path.exists(file_path):
                    new_cache[cache_key] = cache[cache_key]
                    category_docs.setdefault(safe_category, []).append({
                        "title": title,
                        "filename": filename,
                    })
                continue

            content = strip_frontmatter(attrs.get("content", ""))
            if not content:
                if cache_key in cache and os.path.exists(file_path):
                    new_cache[cache_key] = cache[cache_key]
                    category_docs.setdefault(safe_category, []).append({
                        "title": title,
                        "filename": filename,
                    })
                continue

            category_docs.setdefault(safe_category, []).append({
                "title": title,
                "filename": filename,
            })

            content_hash = sha256(content)

            if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(file_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                is_new = cache_key not in cache or not os.path.exists(file_path)
                if args.dry_run:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {safe_category}/{filename}")
                else:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)
                        f.write("\n")
                    if args.verbose:
                        print(f"  {'ADD' if is_new else 'UPDATE'} {safe_category}/{filename}")
                new_cache[cache_key] = {
                    "sha256": content_hash,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

        cat_pages = category_docs.get(safe_category, [])
        if cat_pages:
            readme_content = build_category_readme(category, cat_pages)
            readme_path = os.path.join(category_dir, "README.md")
            cache_key = f"{safe_category}:README.md"
            content_hash = sha256(readme_content)

            if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                is_new = cache_key not in cache or not os.path.exists(readme_path)
                if args.dry_run:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {safe_category}/README.md")
                else:
                    with open(readme_path, "w") as f:
                        f.write(readme_content)
                    if args.verbose:
                        print(f"  {'ADD' if is_new else 'UPDATE'} {safe_category}/README.md")
                new_cache[cache_key] = {
                    "sha256": content_hash,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

    # Write top-level README
    top_readme = build_top_readme(provider, latest_version, category_docs)
    top_readme_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        with open(top_readme_path, "w") as f:
            f.write(top_readme)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key not in new_cache:
            parts = old_key.split(":", 1)
            if len(parts) == 2:
                cat_dir, fname = parts
                old_path = os.path.join(DOCS_DIR, cat_dir, fname)
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE {cat_dir}/{fname}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE {cat_dir}/{fname}")
                    removed += 1

    # Clean empty dirs
    if not args.dry_run:
        for entry in os.scandir(DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)
                if args.verbose:
                    print(f"  RMDIR {entry.name}/")

    if not args.dry_run:
        save_cache(new_cache)
        # Commit the source snapshot only after every document request
        # succeeded. A partial run must retry instead of qualifying for the
        # source-manifest fast path on its next invocation.
        if len(doc_contents) == len(docs):
            with open(PROVIDER_DOCS_FILE, "w") as f:
                json.dump(version_data, f, indent=2)
                f.write("\n")

    total_docs = sum(len(d) for d in category_docs.values())

    print(f"\nSync complete:")
    print(f"  Added:        {added}")
    print(f"  Updated:      {updated}")
    print(f"  Unchanged:    {unchanged}")
    print(f"  Removed:      {removed}")
    print(f"  Total files:  {added + updated + unchanged}")
    print(f"  Total categories: {len(category_docs)}")
    print(f"  Total docs:   {total_docs}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Terraform provider docs from the Terraform Registry and convert to markdown"
    )
    parser.add_argument(
        "--provider",
        help="Provider to fetch (org/name, e.g. hashicorp/googleworkspace)",
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

    if not args.provider:
        if not sys.stdin.isatty():
            print("ERROR: --provider is required in non-interactive mode", file=sys.stderr)
            print("  Usage: python fetch.py --provider org/name", file=sys.stderr)
            sys.exit(1)

        providers = load_provider_index(force_refresh=args.force)
        if not providers:
            print("ERROR: Could not load provider index", file=sys.stderr)
            sys.exit(1)

        selected = pick_provider_interactive(providers)
        if not selected:
            print("No provider selected.")
            sys.exit(0)

        args.provider = selected

    # Validate format
    if "/" not in args.provider:
        print(f"ERROR: Provider must be org/name format (got '{args.provider}')", file=sys.stderr)
        sys.exit(1)

    print(f"Provider: {args.provider}\n")
    sync(args)


if __name__ == "__main__":
    main()
