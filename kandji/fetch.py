#!/usr/bin/env python3

"""
Kandji API Documentation Fetcher

Fetches the Kandji API documentation from their Postman collection endpoint
and converts it into organized markdown files grouped by folder.

NOTE: The raw collection is saved as collection.json next to this script.
      Add **/collection.json to .gitignore to keep it out of version control.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from html import unescape
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

COLLECTION_URL = (
    "https://api-docs.kandji.io/api/collections/15284493/TzCTZkBe"
    "?segregateAuth=true&versionTag=latest"
)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
COLLECTION_FILE = os.path.join(SCRIPT_DIR, "collection.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={"User-Agent": "kandji-api-docs-fetcher/1.0"})
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


# ---------------------------------------------------------------------------
# HTML-to-markdown conversion (stdlib only, regex-based)
# ---------------------------------------------------------------------------

def convert_html_table(html_table: str) -> str:
    """Convert an HTML table to a markdown table."""
    headers = re.findall(r"<th[^>]*>(.*?)</th>", html_table, flags=re.DOTALL)
    if not headers:
        return ""

    rows = re.findall(r"<tr[^>]*>(.*?)</tr>", html_table, flags=re.DOTALL)

    md = "\n\n| " + " | ".join(clean_html(h) for h in headers) + " |\n"
    md += "| " + " | ".join(["---"] * len(headers)) + " |\n"

    for row in rows:
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, flags=re.DOTALL)
        if cells:
            md += "| " + " | ".join(clean_html(c).replace("\n", " ") for c in cells) + " |\n"

    return md + "\n"


def clean_html(html_text: str) -> str:
    """Convert HTML to markdown-friendly plain text using stdlib only."""
    if not html_text:
        return ""

    text = unescape(html_text)

    # Structural tags -> markdown equivalents
    text = re.sub(r"<h1[^>]*>(.*?)</h1>", r"\n# \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h2[^>]*>(.*?)</h2>", r"\n## \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h3[^>]*>(.*?)</h3>", r"\n### \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<h4[^>]*>(.*?)</h4>", r"\n#### \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<p[^>]*>(.*?)</p>", r"\1\n\n", text, flags=re.DOTALL)
    text = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", text, flags=re.DOTALL)
    text = re.sub(r"<pre[^>]*>(.*?)</pre>", r"\n```\n\1\n```\n", text, flags=re.DOTALL)
    text = re.sub(r"<ul[^>]*>(.*?)</ul>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<ol[^>]*>(.*?)</ol>", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"<li[^>]*>(.*?)</li>", r"- \1\n", text, flags=re.DOTALL)
    text = re.sub(r"<strong[^>]*>(.*?)</strong>", r"**\1**", text, flags=re.DOTALL)
    text = re.sub(r"<em[^>]*>(.*?)</em>", r"*\1*", text, flags=re.DOTALL)
    text = re.sub(
        r"<a[^>]*href=[\"']([^\"']*)[\"'][^>]*>(.*?)</a>",
        r"[\2](\1)",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r"<table[^>]*>.*?</table>",
        lambda m: convert_html_table(m.group(0)),
        text,
        flags=re.DOTALL,
    )

    # Strip remaining tags
    text = re.sub(r"<[^>]+>", "", text)

    # Collapse excessive blank lines
    text = re.sub(r"\n{3,}", "\n\n", text)

    return text.strip()


# ---------------------------------------------------------------------------
# Postman collection -> markdown
# ---------------------------------------------------------------------------

def extract_url(url) -> str:
    """Return a raw URL string from Postman's url field (may be str or dict)."""
    if isinstance(url, str):
        return url
    if isinstance(url, dict):
        return url.get("raw", "")
    return ""


def build_endpoint_markdown(item: dict) -> str:
    """Convert a single Postman request item to a markdown document."""
    lines: list[str] = []

    name = item.get("name", "Unnamed")
    lines.append(f"# {name}\n")

    request = item.get("request", {})

    # Description
    description = request.get("description", "")
    if description:
        lines.append(clean_html(description))
        lines.append("")

    method = request.get("method", "GET")
    url_raw = extract_url(request.get("url"))

    lines.append("## Request\n")
    lines.append(f"**Method:** `{method}`\n")
    lines.append(f"**URL:** `{url_raw}`\n")

    # Query parameters
    url_obj = request.get("url")
    if isinstance(url_obj, dict):
        query_params = url_obj.get("query", [])
        if query_params:
            lines.append("### Query Parameters\n")
            for param in query_params:
                param_name = param.get("key", "")
                param_value = param.get("value", "")
                param_desc = param.get("description", "")
                if isinstance(param_desc, dict):
                    param_desc = param_desc.get("content", "")
                disabled = param.get("disabled", False)
                optional = " *(optional)*" if disabled else ""
                line = f"- **{param_name}**{optional}: `{param_value}`"
                if param_desc:
                    line += f"\n  - {clean_html(param_desc)}"
                lines.append(line)
            lines.append("")

    # Headers
    headers = request.get("header", [])
    if headers:
        lines.append("### Headers\n")
        for header in headers:
            lines.append(f"- **{header.get('key', '')}**: `{header.get('value', '')}`")
        lines.append("")

    # Authentication
    auth = request.get("auth", {})
    if auth and auth.get("type"):
        lines.append("### Authentication\n")
        lines.append(f"Type: `{auth.get('type')}`\n")

    # Request body
    body = request.get("body")
    if body:
        lines.append("### Request Body\n")
        body_mode = body.get("mode", "")
        if body_mode == "raw":
            raw_body = body.get("raw", "")
            if raw_body:
                lines.append(f"```json\n{raw_body}\n```\n")
        elif body_mode == "formdata":
            form_data = body.get("formdata", [])
            if form_data:
                for fd in form_data:
                    fd_key = fd.get("key", "")
                    fd_type = fd.get("type", "text")
                    fd_desc = fd.get("description", "")
                    if isinstance(fd_desc, dict):
                        fd_desc = fd_desc.get("content", "")
                    line = f"- **{fd_key}** ({fd_type})"
                    if fd_desc:
                        line += f": {clean_html(fd_desc)}"
                    lines.append(line)
                lines.append("")
        elif body_mode == "urlencoded":
            url_encoded = body.get("urlencoded", [])
            if url_encoded:
                for ue in url_encoded:
                    ue_key = ue.get("key", "")
                    ue_value = ue.get("value", "")
                    ue_desc = ue.get("description", "")
                    if isinstance(ue_desc, dict):
                        ue_desc = ue_desc.get("content", "")
                    line = f"- **{ue_key}**: `{ue_value}`"
                    if ue_desc:
                        line += f" -- {clean_html(ue_desc)}"
                    lines.append(line)
                lines.append("")

    # Response examples
    responses = item.get("response", [])
    if responses:
        lines.append("## Response Examples\n")
        for resp in responses:
            resp_name = resp.get("name", "Example")
            status = resp.get("status", "")
            code = resp.get("code", "")
            resp_body = resp.get("body", "")

            lines.append(f"### {resp_name}\n")
            if status and code:
                lines.append(f"**Status:** `{code} {status}`\n")

            if resp_body:
                try:
                    formatted = json.dumps(json.loads(resp_body), indent=2)
                except (json.JSONDecodeError, TypeError, ValueError):
                    formatted = resp_body
                lines.append(f"```json\n{formatted}\n```\n")

    return "\n".join(lines)


def build_method_filename(item: dict) -> str:
    """Build a filename like {method}-{slugified-name}.md for an endpoint."""
    request = item.get("request", {})
    method = request.get("method", "get").lower()
    name = item.get("name", "unnamed")
    return f"{method}-{sanitize_filename(name)}.md"


def build_folder_readme(folder_name: str, folder_desc: str, endpoints: list[dict], subfolders: list[str]) -> str:
    """Build a README.md for a folder, listing sub-folders and endpoints."""
    lines = [f"# {folder_name}\n"]

    if folder_desc:
        lines.append(clean_html(folder_desc))
        lines.append("")

    if subfolders:
        lines.append("## Sub-sections\n")
        for sf in subfolders:
            lines.append(f"- [{sf['name']}](./{sf['safe_name']}/)")
        lines.append("")

    if endpoints:
        lines.append("## Endpoints\n")
        for ep in endpoints:
            method = ep.get("request", {}).get("method", "GET").upper()
            name = ep.get("name", "Unnamed")
            filename = build_method_filename(ep)
            url_raw = extract_url(ep.get("request", {}).get("url"))
            lines.append(f"- [{method} {name}](./{filename})")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Recursive collection walker
# ---------------------------------------------------------------------------

def walk_folder(folder: dict, prefix: str, cache: dict, new_cache: dict,
                args: argparse.Namespace, counters: dict) -> None:
    """Recursively process a Postman folder, writing markdown files."""
    folder_name = folder.get("name", "unknown")
    folder_desc = folder.get("description", "")
    safe_name = sanitize_filename(folder_name)
    folder_path = os.path.join(DOCS_DIR, prefix, safe_name) if prefix else os.path.join(DOCS_DIR, safe_name)
    rel_prefix = os.path.join(prefix, safe_name) if prefix else safe_name

    if not args.dry_run:
        os.makedirs(folder_path, exist_ok=True)

    items = folder.get("item", [])

    # Separate endpoints from subfolders
    endpoints = [it for it in items if "request" in it]
    subfolders = [it for it in items if "item" in it and "request" not in it]
    subfolder_info = [
        {"name": sf.get("name", "unknown"), "safe_name": sanitize_filename(sf.get("name", "unknown"))}
        for sf in subfolders
    ]

    # Write folder README
    readme_content = build_folder_readme(folder_name, folder_desc, endpoints, subfolder_info)
    readme_file = os.path.join(folder_path, "README.md")
    cache_key = f"folder:{rel_prefix}:README"
    content_hash = sha256(readme_content)

    _write_file(readme_file, readme_content, cache_key, content_hash,
                cache, new_cache, args, counters, f"{rel_prefix}/README.md")

    # Write endpoint files
    for ep in endpoints:
        ep_content = build_endpoint_markdown(ep)
        ep_filename = build_method_filename(ep)
        ep_file = os.path.join(folder_path, ep_filename)
        cache_key = f"folder:{rel_prefix}:{ep_filename}"
        content_hash = sha256(ep_content)

        _write_file(ep_file, ep_content, cache_key, content_hash,
                     cache, new_cache, args, counters, f"{rel_prefix}/{ep_filename}")

    # Recurse into subfolders
    for sf in subfolders:
        walk_folder(sf, rel_prefix, cache, new_cache, args, counters)


def _write_file(filepath: str, content: str, cache_key: str,
                content_hash: str, cache: dict, new_cache: dict,
                args: argparse.Namespace, counters: dict, display_path: str) -> None:
    """Write a single file with cache-aware skip/add/update logic."""
    if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(filepath):
        counters["unchanged"] += 1
        new_cache[cache_key] = cache[cache_key]
        return

    is_new = cache_key not in cache or not os.path.exists(filepath)
    label = "ADD" if is_new else "UPDATE"

    if args.dry_run:
        print(f"  {label} {display_path}")
    else:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content)
        if args.verbose:
            print(f"  {label} {display_path}")

    new_cache[cache_key] = {
        "sha256": content_hash,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    if is_new:
        counters["added"] += 1
    else:
        counters["updated"] += 1


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    # Fetch the Postman collection
    print("Fetching Kandji Postman collection...")
    raw = fetch_url(COLLECTION_URL)
    if not raw:
        sys.exit(1)

    data = json.loads(raw)
    print(f"  Collection: {data.get('info', {}).get('name', '?')}")

    # Save raw collection JSON
    if not args.dry_run:
        with open(COLLECTION_FILE, "w", encoding="utf-8") as f:
            f.write(raw)
            f.write("\n")

    items = data.get("item", [])
    info = data.get("info", {})
    print(f"  Top-level folders: {len([i for i in items if 'item' in i])}")
    print(f"  Top-level endpoints: {len([i for i in items if 'request' in i])}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    new_cache: dict = {}
    counters = {"added": 0, "updated": 0, "unchanged": 0}

    # Process each top-level item
    top_folders = []
    top_endpoints = []
    for item in items:
        if "item" in item and "request" not in item:
            top_folders.append(item)
            walk_folder(item, "", cache, new_cache, args, counters)
        elif "request" in item:
            # Rare: a top-level endpoint not inside a folder
            top_endpoints.append(item)
            ep_content = build_endpoint_markdown(item)
            ep_filename = build_method_filename(item)
            ep_file = os.path.join(DOCS_DIR, ep_filename)
            cache_key = f"root:{ep_filename}"
            content_hash = sha256(ep_content)
            _write_file(ep_file, ep_content, cache_key, content_hash,
                        cache, new_cache, args, counters, ep_filename)

    # Write top-level README
    main_lines = [f"# {info.get('name', 'Kandji API Documentation')}\n"]
    desc = info.get("description", "")
    if desc:
        main_lines.append(clean_html(desc))
        main_lines.append("")
    main_lines.append("## API Categories\n")
    for folder in top_folders:
        fname = folder.get("name", "unknown")
        safe = sanitize_filename(fname)
        count = _count_endpoints(folder)
        main_lines.append(f"- [{fname}](./{safe}/) ({count} endpoints)")
    if top_endpoints:
        main_lines.append("")
        main_lines.append("## Top-level Endpoints\n")
        for ep in top_endpoints:
            method = ep.get("request", {}).get("method", "GET").upper()
            name = ep.get("name", "Unnamed")
            filename = build_method_filename(ep)
            main_lines.append(f"- [{method} {name}](./{filename})")
    main_lines.append("")
    main_content = "\n".join(main_lines)

    main_readme_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        with open(main_readme_path, "w", encoding="utf-8") as f:
            f.write(main_content)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key not in new_cache:
            parts = old_key.split(":", 2)
            if len(parts) == 3:
                if parts[0] == "folder":
                    old_path = os.path.join(DOCS_DIR, parts[1], parts[2])
                elif parts[0] == "root":
                    old_path = os.path.join(DOCS_DIR, parts[2])
                else:
                    continue
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE {old_key}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE {old_key}")
                    removed += 1

    # Clean empty directories
    if not args.dry_run:
        _clean_empty_dirs(DOCS_DIR)

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    total_endpoints = _count_endpoints({"item": items})
    print(f"\nSync complete:")
    print(f"  Added:      {counters['added']}")
    print(f"  Updated:    {counters['updated']}")
    print(f"  Unchanged:  {counters['unchanged']}")
    print(f"  Removed:    {removed}")
    print(f"  Total files: {counters['added'] + counters['updated'] + counters['unchanged']}")
    print(f"  Total endpoints: {total_endpoints}")


def _count_endpoints(folder: dict) -> int:
    """Recursively count endpoints in a Postman folder."""
    count = 0
    for item in folder.get("item", []):
        if "request" in item:
            count += 1
        if "item" in item:
            count += _count_endpoints(item)
    return count


def _clean_empty_dirs(root: str) -> None:
    """Remove empty directories under root, bottom-up."""
    for dirpath, dirnames, filenames in os.walk(root, topdown=False):
        if dirpath == root:
            continue
        if not os.listdir(dirpath):
            os.rmdir(dirpath)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Kandji API docs from Postman collection and convert to markdown"
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
        "--verbose",
        action="store_true",
        help="Detailed per-file logging",
    )
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
