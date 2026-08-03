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

Each provider's docs live under docs/{org}/{name}/ and its cache keys are
prefixed with "{org}/{name}|", so multiple providers coexist in one checkout
and reconciling one never touches another's files.

The registry serves no ETag or Last-Modified (measured), so the incremental
fast path is a per-provider source fingerprint instead: provider version +
a hash of the docs manifest (immutable doc IDs plus output-relevant metadata)
+ the fetcher hash, recorded in .cache.json under "source:{org}/{name}" with
the outputs it produced. When all of it matches and every recorded output is
on disk, a routine sync is two registry requests and zero per-document ones.

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
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
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
# Doc-body workers. Measured on integrations/github (164 docs): 8 workers took
# ~4.0s, 16 took ~2.7s, both with zero failures from the registry.
MAX_WORKERS = 16


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# Transient-failure policy. A 404 is a permanent answer, so only these statuses
# are worth a second round trip.
RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})
MAX_RETRIES = 3
MAX_BACKOFF = 30.0
# Retries are bounded by total time per URL as well as attempt count. This is
# what discriminates a cheap transient failure, which is worth retrying, from an
# endpoint that is simply slow to fail: Google's retired poly and datalabeling
# discovery endpoints hang ~20s before each 502, so retrying them turned a 30s
# run into 98s while recovering nothing. A fast 503 still gets its full retries;
# a failure that already cost this long does not, and the next run picks it up.
RETRY_DEADLINE = 20.0


def out_of_budget(attempt: int, started: float, cap: int = MAX_RETRIES) -> bool:
    """True once this URL has spent its retry budget (attempts or time)."""
    return attempt >= cap or (time.monotonic() - started) >= RETRY_DEADLINE


def retry_wait(attempt: int, err: HTTPError | None = None) -> float:
    """Bounded exponential backoff, honoring Retry-After when the origin sends one."""
    if err is not None and err.headers:
        retry_after = err.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(str(retry_after)), MAX_BACKOFF)
            except ValueError:
                pass
    return float(min(1.5 * (2**attempt), MAX_BACKOFF))


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": "terraform-api-docs-fetcher/1.0",
            "Accept-Encoding": "gzip",
        },
    )
    started = time.monotonic()
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urlopen(req, timeout=timeout) as resp:
                data: bytes = resp.read()
                if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
                    data = gzip.decompress(data)
                return data.decode("utf-8")
        except HTTPError as e:
            if e.code not in RETRYABLE_STATUS or out_of_budget(attempt, started):
                print(f"ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
                return None
            time.sleep(retry_wait(attempt, e))
        except (URLError, TimeoutError, OSError) as e:
            if out_of_budget(attempt, started):
                print(f"ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
                return None
            time.sleep(retry_wait(attempt))
    return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "-", name)
    return name.lower().strip("-")


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


def strip_frontmatter(content: str) -> str:
    """Remove YAML frontmatter (lines between --- markers at start of file)."""
    if not content or not content.startswith("---"):
        return content
    parts = content.split("---", 2)
    if len(parts) >= 3:
        return parts[2].strip()
    return content


def fetcher_hash() -> str:
    """SHA256 of this script.

    Folded into the source fingerprint so a converter change is picked up by a
    routine run instead of needing --force.
    """
    with open(os.path.abspath(__file__), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def outputs_present(entry: dict) -> bool:
    """True when every output the cached source produced is still on disk.

    A source-level cache hit is only valid if its outputs survived; a deleted
    file has to force regeneration.
    """
    outputs = entry.get("outputs")
    if not outputs:
        return False
    return all(os.path.exists(os.path.join(DOCS_DIR, rel)) for rel in outputs)


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
    manifest.sort(key=lambda item: (item["category"], item["slug"], item["id"], item["title"]))
    return sha256(json.dumps(manifest, sort_keys=True, separators=(",", ":")))


def fetch_doc_content(doc_id: str) -> dict | None:
    """Fetch a single doc from the v2 JSON:API endpoint."""
    url = f"{REGISTRY_V2_DOCS}/{doc_id}"
    raw = fetch_url(url)
    if not raw:
        return None
    try:
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        attributes = data.get("data", {}).get("attributes", {})
        return attributes if isinstance(attributes, dict) else None
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
        with open(INDEX_FILE) as f:
            idx = json.load(f)
        fetched = idx.get("fetched_at", "")
        if not fetched:
            return False
        dt = datetime.fromisoformat(fetched)
        age = datetime.now(UTC) - dt
        return age.total_seconds() < INDEX_MAX_AGE_HOURS * 3600
    except (json.JSONDecodeError, OSError, ValueError):
        return False


def _fetch_index_page(page: int, page_size: int = 100) -> dict | None:
    url = f"{REGISTRY_V2_PROVIDERS}?page[size]={page_size}&page[number]={page}"
    raw = fetch_url(url, timeout=30)
    if not raw:
        return None
    page = json.loads(raw)
    return page if isinstance(page, dict) else None


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
        providers.append(
            {
                "name": a.get("full-name", ""),
                "tier": a.get("tier", "community"),
                "downloads": a.get("downloads", 0),
            }
        )

    # Fetch remaining pages concurrently
    if total_pages > 1:
        with ThreadPoolExecutor(max_workers=10) as pool:
            futures = {pool.submit(_fetch_index_page, p): p for p in range(2, total_pages + 1)}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    for item in result.get("data", []):
                        a = item.get("attributes", {})
                        providers.append(
                            {
                                "name": a.get("full-name", ""),
                                "tier": a.get("tier", "community"),
                                "downloads": a.get("downloads", 0),
                            }
                        )

    providers.sort(key=lambda p: (-p["downloads"], p["name"]))

    # Save index
    index = {
        "fetched_at": datetime.now(UTC).isoformat(),
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
        with open(INDEX_FILE) as f:
            idx = json.load(f)
        providers = idx.get("providers", []) if isinstance(idx, dict) else []
        return providers if isinstance(providers, list) else []
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
            input=text,
            capture_output=True,
            text=True,
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

        matches = providers[:20] if not query else [p for p in providers if query in p["name"].lower()][:20]

        if not matches:
            print("  No matches. Try again.")
            continue

        for i, p in enumerate(matches, 1):
            print(f"  {i:3d}. {p['name']}  ({p['tier']})")

        if len(matches) == 1:
            confirm = input(f"\nUse {matches[0]['name']}? [Y/n] ").strip().lower()
            if confirm in ("", "y", "yes"):
                return str(matches[0]["name"])
            continue

        try:
            pick = input("\nEnter number (or search again): ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return None

        if pick.isdigit():
            n = int(pick)
            if 1 <= n <= len(matches):
                return str(matches[n - 1]["name"])

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
    lines.append(
        f"**Provider:** [{provider}](https://registry.terraform.io/providers/{provider}/latest/docs)\n"
    )
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


def _provider_docs_dir(provider: str) -> str:
    """Each provider gets its own subtree: docs/{org}/{name}/. Keeps multiple
    providers from overwriting each other's files and READMEs."""
    org, name = provider.split("/", 1)
    return os.path.join(DOCS_DIR, sanitize_filename(org), sanitize_filename(name))


def sync(args: argparse.Namespace) -> None:
    provider = args.provider
    # --force disables the unchanged-skip for THIS provider, but the previous
    # cache is still needed to preserve other providers' entries and to detect
    # this provider's removals safely, so load it either way.
    prev_cache = load_cache()
    cache = {} if args.force else prev_cache
    provider_docs_dir = _provider_docs_dir(provider)
    cache_prefix = f"{provider}|"
    source_key = f"source:{provider}"

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

    # Source-manifest fast path. The registry serves no validators, but doc IDs
    # are immutable and content is fixed per published version, so an unchanged
    # version + docs manifest + fetcher proves the outputs current -- as long as
    # every output the last full run recorded is still on disk.
    prev_source = prev_cache.get(source_key, {}) if not args.force else {}
    manifest_hash = docs_manifest_hash(docs)
    if (
        prev_source.get("version") == latest_version
        and prev_source.get("manifest_sha256") == manifest_hash
        and prev_source.get("fetcher_sha256") == fetcher_hash()
        and outputs_present(prev_source)
    ):
        print(
            "  version and docs manifest unchanged, all outputs present; "
            "skipping individual document downloads"
        )
        if not args.dry_run:
            save_cache(prev_cache)
        print("\nSync complete:")
        print("  Added:        0")
        print("  Updated:      0")
        print(f"  Unchanged:    {len(prev_source['outputs'])}")
        print("  Removed:      0")
        print(f"  Total files:  {len(prev_source['outputs'])}")
        print(f"  Total docs:   {len(docs)}")
        return

    if not args.dry_run:
        with open(PROVIDER_DOCS_FILE, "w") as f:
            json.dump(version_data, f, indent=2)
            f.write("\n")

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
        os.makedirs(provider_docs_dir, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    # Start from the previous cache (not the possibly force-emptied view) so
    # other providers' entries and source fingerprints always survive; this
    # provider's own source entry is re-recorded only after a full success.
    new_cache: dict = {
        k: v for k, v in prev_cache.items() if not k.startswith(cache_prefix) and k != source_key
    }
    # Track keys rebuilt this run so we know what belongs to this provider now.
    rebuilt_keys: set[str] = set()

    category_docs: dict[str, list[dict]] = {}

    for category in sorted(categories.keys()):
        safe_category = sanitize_filename(category)
        category_dir = os.path.join(provider_docs_dir, safe_category)

        if not args.dry_run:
            os.makedirs(category_dir, exist_ok=True)

        for doc in sorted(categories[category], key=lambda x: x.get("title", x.get("slug", ""))):
            doc_id = str(doc.get("id", ""))
            title = doc.get("title", doc.get("slug", "untitled"))
            slug = doc.get("slug", title)
            filename = f"{sanitize_filename(slug)}.md"
            cache_key = f"{cache_prefix}{safe_category}:{filename}"
            file_path = os.path.join(category_dir, filename)

            attrs = doc_contents.get(doc_id)
            content = strip_frontmatter(attrs.get("content", "")) if attrs else ""
            if not content:
                # A failed or empty body is not a removal: the doc is still in
                # the authoritative list, so keep the last known-good file, its
                # cache entry, and its README line. prev_cache, not the
                # force-emptied view, so --force cannot turn a transient
                # failure into a deletion either.
                if cache_key in prev_cache and os.path.exists(file_path):
                    new_cache[cache_key] = prev_cache[cache_key]
                    rebuilt_keys.add(cache_key)
                    unchanged += 1
                    category_docs.setdefault(safe_category, []).append(
                        {
                            "title": title,
                            "filename": filename,
                        }
                    )
                continue

            content_hash = sha256(content)
            rebuilt_keys.add(cache_key)

            category_docs.setdefault(safe_category, []).append(
                {
                    "title": title,
                    "filename": filename,
                }
            )

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
                    "last_updated": datetime.now(UTC).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

        cat_pages = category_docs.get(safe_category, [])
        if cat_pages:
            readme_content = build_category_readme(category, cat_pages)
            readme_path = os.path.join(category_dir, "README.md")
            cache_key = f"{cache_prefix}{safe_category}:README.md"
            rebuilt_keys.add(cache_key)
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
                    "last_updated": datetime.now(UTC).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

    # Write provider README (top of this provider's subtree)
    top_readme = build_top_readme(provider, latest_version, category_docs)
    top_readme_path = os.path.join(provider_docs_dir, "README.md")
    if not args.dry_run:
        with open(top_readme_path, "w") as f:
            f.write(top_readme)

    # Detect removals — only consider cache keys belonging to this provider.
    # Walk the previous cache, not the force-emptied view, so --force runs
    # still notice docs that left the manifest.
    removed = 0
    for old_key in sorted(prev_cache):
        if not old_key.startswith(cache_prefix):
            continue
        if old_key in rebuilt_keys:
            continue
        relative = old_key[len(cache_prefix) :]
        parts = relative.split(":", 1)
        if len(parts) == 2:
            cat_dir, fname = parts
            old_path = os.path.join(provider_docs_dir, cat_dir, fname)
            if os.path.exists(old_path):
                if args.dry_run:
                    print(f"  REMOVE {cat_dir}/{fname}")
                else:
                    os.remove(old_path)
                    if args.verbose:
                        print(f"  REMOVE {cat_dir}/{fname}")
                removed += 1
            new_cache.pop(old_key, None)

    # Clean empty dirs inside this provider's subtree.
    if not args.dry_run and os.path.isdir(provider_docs_dir):
        for entry in os.scandir(provider_docs_dir):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)
                if args.verbose:
                    print(f"  RMDIR {entry.name}/")

    if not args.dry_run:
        # Record the source fingerprint only after every document request
        # succeeded. A partial run must retry instead of qualifying for the
        # fast path on its next invocation.
        if len(doc_contents) == len(docs):
            outputs = [os.path.relpath(top_readme_path, DOCS_DIR)]
            for key in rebuilt_keys:
                cat_dir, fname = key[len(cache_prefix) :].split(":", 1)
                outputs.append(os.path.relpath(os.path.join(provider_docs_dir, cat_dir, fname), DOCS_DIR))
            new_cache[source_key] = {
                "version": latest_version,
                "manifest_sha256": manifest_hash,
                "fetcher_sha256": fetcher_hash(),
                "outputs": sorted(outputs),
            }
        save_cache(new_cache)

    total_docs = sum(len(d) for d in category_docs.values())

    print("\nSync complete:")
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
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
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
