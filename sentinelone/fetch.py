#!/usr/bin/env python3

"""
SentinelOne API Documentation Fetcher

Fetches the SentinelOne API documentation from a management console and converts
it into organized markdown files grouped by category.

The S1 console serves a custom Swagger-like spec behind authentication at
/apidoc/formatted_swagger_2_1.json (with fallback to 2_0). This script requires
you to supply the console URL and authentication cookies explicitly -- there is
no hardcoded tenant URL and no browser-cookie integration.

Usage:
    python fetch.py --base-url https://your-console.sentinelone.net --cookie-file cookies.txt
    python fetch.py --base-url https://your-console.sentinelone.net --cookie "Authorization=Token eyJ..."

    Standard flags:
    python fetch.py --base-url ... --cookie-file cookies.txt --dry-run
    python fetch.py --base-url ... --cookie-file cookies.txt --force
    python fetch.py --base-url ... --cookie-file cookies.txt --verbose
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SPEC_FILE = os.path.join(SCRIPT_DIR, "api-spec.json")


# ---------------------------------------------------------------------------
# Standard helpers
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, cookie_header: str | None = None, timeout: int = 60) -> str | None:
    """Fetch a URL using stdlib. Optionally attach a Cookie header for auth."""
    headers = {
        "User-Agent": "sentinelone-api-docs-fetcher/1.0",
        "Accept": "application/json",
        "Accept-Encoding": "gzip",
    }
    if cookie_header:
        headers["Cookie"] = cookie_header
    req = Request(url, headers=headers)
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


def clean_desc(desc: str) -> str:
    """Clean a description for use inside a markdown table cell."""
    if not desc:
        return ""
    desc = re.sub(r"<[^>]+>", "", desc)
    desc = desc.replace("|", "\\|").replace("\n", " ").strip()
    if len(desc) > 150:
        desc = desc[:147] + "..."
    return desc


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def build_cookie_header(args: argparse.Namespace) -> str:
    """Return a Cookie header string from --cookie or --cookie-file."""
    if args.cookie:
        return args.cookie.strip()
    if args.cookie_file:
        with open(args.cookie_file, "r") as f:
            return f.read().strip()
    # Should not get here -- argparse validation catches it.
    print("ERROR: One of --cookie or --cookie-file is required.", file=sys.stderr)
    sys.exit(1)


def verify_auth(base_url: str, cookie_header: str) -> None:
    """Verify authentication by hitting /web/api/v2.1/system/info."""
    url = f"{base_url.rstrip('/')}/web/api/v2.1/system/info"
    print(f"Verifying access to {base_url}...")
    body = fetch_url(url, cookie_header=cookie_header)
    if body is None:
        print("  WARNING: Could not verify auth, continuing anyway...", file=sys.stderr)
        return
    try:
        data = json.loads(body)
        build = data.get("data", {}).get("build", "unknown")
        print(f"  Authenticated. Build: {build}")
    except json.JSONDecodeError:
        print("  WARNING: Auth check returned non-JSON response, continuing anyway...", file=sys.stderr)


# ---------------------------------------------------------------------------
# Spec discovery
# ---------------------------------------------------------------------------

def discover_api_spec(base_url: str, cookie_header: str) -> tuple[dict | None, str | None]:
    """Try to find and fetch the S1 API spec."""
    base = base_url.rstrip("/")
    spec_urls = [
        f"{base}/apidoc/formatted_swagger_2_1.json",
        f"{base}/apidoc/formatted_swagger_2_0.json",
    ]

    print("Discovering API spec URL...")

    for url in spec_urls:
        print(f"  Trying: {url}...", end=" ", flush=True)
        body = fetch_url(url, cookie_header=cookie_header)
        if body is None:
            print("failed")
            time.sleep(0.3)
            continue
        body = body.strip()
        if not body.startswith("{"):
            print("got response but not JSON")
            time.sleep(0.3)
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            print("invalid JSON")
            time.sleep(0.3)
            continue
        if "apiList" in data and "content" in data:
            print(f"found S1 API spec ({len(data.get('apiList', []))} categories)")
            return data, url
        if "paths" in data or "swagger" in data or "openapi" in data:
            print("found OpenAPI spec!")
            return data, url
        print("got response but not a spec")
        time.sleep(0.3)

    return None, None


# ---------------------------------------------------------------------------
# S1 custom-format endpoint -> markdown
# ---------------------------------------------------------------------------

def s1_endpoint_to_markdown(category_name: str, op_key: str, endpoint: dict) -> str:
    """Convert an S1 custom-format endpoint to markdown."""
    lines: list[str] = []
    name = endpoint.get("name", op_key)
    method = endpoint.get("requestType", "")
    url = endpoint.get("url", "")
    description = endpoint.get("description", "")
    deprecated = endpoint.get("isDeprecated", False)

    lines.append(f"# {name}")

    if deprecated:
        lines.append("\n> **DEPRECATED**: This endpoint is deprecated.\n")

    if description:
        lines.append(f"\n{description}\n")

    lines.append("\n## Request")
    lines.append(f"\n**Method:** `{method}`")
    lines.append(f"\n**Endpoint:** `{url}`")

    if endpoint.get("isDownload"):
        lines.append("\n**Note:** This endpoint returns a file download.")

    # Permissions
    required_perms = endpoint.get("requiredPermissions")
    optional_perms = endpoint.get("optionalPermissions")
    if required_perms or optional_perms:
        lines.append("\n### Permissions\n")
        if required_perms:
            if isinstance(required_perms, list):
                lines.append(f"**Required:** {', '.join(required_perms)}")
            else:
                lines.append(f"**Required:** {required_perms}")
        if optional_perms:
            if isinstance(optional_perms, list):
                lines.append(f"\n**Optional:** {', '.join(optional_perms)}")
            else:
                lines.append(f"\n**Optional:** {optional_perms}")

    # Parameters
    params = endpoint.get("parameters", {})

    path_params = params.get("path", [])
    if path_params:
        lines.append("\n### Path Parameters\n")
        lines.append("| Name | Type | Required | Description |")
        lines.append("|------|------|----------|-------------|")
        for p in path_params:
            pname = p.get("name", "")
            ptype = p.get("type", "")
            required = "Yes" if p.get("required", False) else "No"
            desc = clean_desc(p.get("description", ""))
            lines.append(f"| `{pname}` | {ptype} | {required} | {desc} |")

    query_params = params.get("query", [])
    if query_params:
        lines.append("\n### Query Parameters\n")
        lines.append("| Name | Type | Required | Description |")
        lines.append("|------|------|----------|-------------|")
        for p in query_params:
            pname = p.get("name", "")
            ptype = p.get("type", "")
            if ptype == "array":
                items = p.get("items", {})
                item_type = items.get("type", "") if isinstance(items, dict) else ""
                if item_type:
                    ptype = f"array[{item_type}]"
            required = "Yes" if p.get("required", False) else "No"
            desc = clean_desc(p.get("description", ""))
            enum = p.get("enum")
            if enum:
                desc += f" Values: `{'`, `'.join(str(e) for e in enum)}`."
            lines.append(f"| `{pname}` | {ptype} | {required} | {desc} |")

    body_params = params.get("body", [])
    if body_params:
        lines.append("\n### Body Parameters\n")
        lines.append("| Name | Type | Required | Description |")
        lines.append("|------|------|----------|-------------|")
        for p in body_params:
            pname = p.get("name", "")
            ptype = p.get("type", "")
            required = "Yes" if p.get("required", False) else "No"
            desc = clean_desc(p.get("description", ""))
            lines.append(f"| `{pname}` | {ptype} | {required} | {desc} |")

    # Body sample
    body_sample = endpoint.get("bodySample")
    if body_sample:
        lines.append("\n### Request Body Example\n")
        if isinstance(body_sample, str):
            lines.append(f"```json\n{body_sample[:3000]}\n```")
        else:
            lines.append(f"```json\n{json.dumps(body_sample, indent=2)[:3000]}\n```")

    # Responses
    responses = endpoint.get("responses", {})
    if responses:
        lines.append("\n## Responses\n")
        if isinstance(responses, dict):
            resp_items = sorted(responses.items(), key=lambda x: str(x[0]))
        elif isinstance(responses, list):
            resp_items = [
                (r.get("code", r.get("status", "?")), r)
                for r in responses
                if isinstance(r, dict)
            ]
        else:
            resp_items = []
        for status, resp_info in resp_items:
            if isinstance(resp_info, dict):
                desc = resp_info.get("description", "")
                lines.append(f"### {status}")
                if desc:
                    lines.append(f"\n{desc}\n")
                schema = resp_info.get("schema", {})
                if schema:
                    lines.append(f"```json\n{json.dumps(schema, indent=2)[:3000]}\n```\n")
            else:
                lines.append(f"### {status}\n")

    # Response sample
    resp_sample = endpoint.get("responseSample")
    if resp_sample:
        lines.append("\n## Response Example\n")
        if isinstance(resp_sample, str):
            lines.append(f"```json\n{resp_sample[:3000]}\n```")
        else:
            lines.append(f"```json\n{json.dumps(resp_sample, indent=2)[:3000]}\n```")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OpenAPI fallback endpoint -> markdown
# ---------------------------------------------------------------------------

def openapi_endpoint_to_markdown(endpoint: dict) -> str:
    """Convert a standard OpenAPI endpoint to markdown."""
    lines: list[str] = []
    method = endpoint.get("method", "")
    path = endpoint.get("path", "")
    summary = endpoint.get("summary", "")
    description = endpoint.get("description", "")

    lines.append(f"# {summary or f'{method} {path}'}")
    if endpoint.get("deprecated"):
        lines.append("\n> **DEPRECATED**: This endpoint is deprecated.\n")
    if description and description != summary:
        lines.append(f"\n{description}\n")

    lines.append(f"\n## Request\n\n**Method:** `{method}`\n\n**Endpoint:** `{path}`")

    params = endpoint.get("parameters", [])
    if params:
        lines.append("\n### Parameters\n")
        lines.append("| Name | In | Type | Required | Description |")
        lines.append("|------|----|------|----------|-------------|")
        for p in params:
            pname = p.get("name", "")
            loc = p.get("in", "")
            schema = p.get("schema", {})
            ptype = schema.get("type", "") if isinstance(schema, dict) else ""
            required = "Yes" if p.get("required", False) else "No"
            desc = clean_desc(p.get("description", ""))
            lines.append(f"| `{pname}` | {loc} | {ptype} | {required} | {desc} |")

    req_body = endpoint.get("request_body", {})
    if req_body:
        lines.append("\n### Request Body\n")
        content = req_body.get("content", {})
        for ct, si in content.items():
            lines.append(f"**Content-Type:** `{ct}`\n")
            schema = si.get("schema", {})
            if schema:
                lines.append(f"```json\n{json.dumps(schema, indent=2)[:2000]}\n```\n")

    responses = endpoint.get("responses", {})
    if responses:
        lines.append("\n## Responses\n")
        for status, ri in sorted(responses.items()):
            desc = ri.get("description", "") if isinstance(ri, dict) else ""
            lines.append(f"### {status}")
            if desc:
                lines.append(f"\n{desc}\n")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown generation -- S1 custom format
# ---------------------------------------------------------------------------

def build_s1_category_readme(cat_name: str, endpoints: list[dict]) -> str:
    lines = [f"# {cat_name}\n", "Endpoints in this category:\n"]
    for ep in endpoints:
        method = ep["method"]
        url = ep["url"]
        name = ep["name"]
        filename = ep["filename"]
        deprecated = " (DEPRECATED)" if ep.get("deprecated") else ""
        lines.append(f"- [{method} {url}](./{filename}) -- {name}{deprecated}")
    lines.append("")
    return "\n".join(lines)


def build_s1_main_readme(base_url: str, categories: list[tuple[str, str, int]], total_endpoints: int) -> str:
    lines = [
        "# SentinelOne API Documentation\n",
        "**API Version:** 2.1",
        f"**Base URL:** {base_url}",
        f"**Total Categories:** {len(categories)}",
        f"**Total Endpoints:** {total_endpoints}\n",
        "## Categories\n",
    ]
    for slug, name, count in categories:
        lines.append(f"- [{name}](./{slug}/) ({count} endpoints)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Markdown generation -- OpenAPI fallback
# ---------------------------------------------------------------------------

def build_openapi_tag_readme(tag_name: str, endpoints: list[dict]) -> str:
    lines = [f"# {tag_name}\n", "Endpoints in this category:\n"]
    for ep in endpoints:
        method = ep["method"]
        path = ep["path"]
        summary = ep.get("summary", "")
        filename = ep["filename"]
        line = f"- [{method} {path}](./{filename})"
        if summary:
            line += f" -- {summary}"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


def build_openapi_main_readme(base_url: str, info: dict, tags: list[tuple[str, str, int]], total_endpoints: int) -> str:
    lines = [
        "# SentinelOne API Documentation\n",
        f"**API Version:** {info.get('version', 'unknown')}",
        f"**Base URL:** {base_url}",
        f"**Total Endpoints:** {total_endpoints}\n",
        "## Categories\n",
    ]
    for slug, name, count in tags:
        lines.append(f"- [{name}](./{slug}/) ({count} endpoints)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync -- S1 custom format
# ---------------------------------------------------------------------------

def sync_s1(spec: dict, base_url: str, args: argparse.Namespace) -> None:
    """Generate markdown from the S1 custom spec format."""
    cache = {} if args.force else load_cache()
    new_cache: dict = {}

    api_list = spec.get("apiList", [])
    content = spec.get("content", {})

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    total_endpoints = 0
    category_index: list[tuple[str, str, int]] = []

    for category in api_list:
        cat_key = category["key"]
        cat_name = category["name"]
        operations = category.get("operations", [])
        cat_content = content.get(cat_key, {})

        cat_slug = sanitize_filename(cat_name)
        cat_dir = os.path.join(DOCS_DIR, cat_slug)

        if not args.dry_run:
            os.makedirs(cat_dir, exist_ok=True)

        # Collect endpoint metadata for the README
        ep_meta: list[dict] = []

        for op in operations:
            op_key = op["key"]
            op_name = op.get("name", op_key)
            ep_data = cat_content.get(op_key)
            if not ep_data:
                continue

            method = ep_data.get("requestType", "UNKNOWN")
            url = ep_data.get("url", "")

            filename = f"{method.lower()}-{sanitize_filename(op_key)}.md"

            ep_meta.append({
                "method": method,
                "url": url,
                "name": op_name,
                "filename": filename,
                "deprecated": op.get("isDeprecated", False),
            })

            # Build endpoint markdown
            ep_content = s1_endpoint_to_markdown(cat_name, op_key, ep_data)
            ep_path = os.path.join(cat_dir, filename)
            cache_key = f"cat:{cat_slug}:{filename}"
            content_hash = sha256(ep_content)

            if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(ep_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                is_new = cache_key not in cache or not os.path.exists(ep_path)
                if args.dry_run:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {cat_slug}/{filename}")
                else:
                    with open(ep_path, "w") as f:
                        f.write(ep_content)
                    if args.verbose:
                        print(f"  {'ADD' if is_new else 'UPDATE'} {cat_slug}/{filename}")
                new_cache[cache_key] = {
                    "sha256": content_hash,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

            total_endpoints += 1

        # Category README
        readme_content = build_s1_category_readme(cat_name, ep_meta)
        readme_path = os.path.join(cat_dir, "README.md")
        cache_key = f"cat:{cat_slug}:README"
        content_hash = sha256(readme_content)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(readme_path)
            if args.dry_run:
                print(f"  {'ADD' if is_new else 'UPDATE'} {cat_slug}/README.md")
            else:
                with open(readme_path, "w") as f:
                    f.write(readme_content)
                if args.verbose:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {cat_slug}/README.md")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

        category_index.append((cat_slug, cat_name, len(ep_meta)))

    # Top-level README
    main_content = build_s1_main_readme(base_url, category_index, total_endpoints)
    main_readme_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        with open(main_readme_path, "w") as f:
            f.write(main_content)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key not in new_cache:
            parts = old_key.split(":", 2)
            if len(parts) == 3 and parts[0] == "cat":
                old_path = os.path.join(DOCS_DIR, parts[1], parts[2])
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE {parts[1]}/{parts[2]}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE {parts[1]}/{parts[2]}")
                    removed += 1

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
    print(f"  Added:      {added}")
    print(f"  Updated:    {updated}")
    print(f"  Unchanged:  {unchanged}")
    print(f"  Removed:    {removed}")
    print(f"  Total files: {added + updated + unchanged}")
    print(f"  Total categories: {len(category_index)}")
    print(f"  Total endpoints:  {total_endpoints}")


# ---------------------------------------------------------------------------
# Sync -- OpenAPI fallback
# ---------------------------------------------------------------------------

def sync_openapi(spec: dict, base_url: str, args: argparse.Namespace) -> None:
    """Generate markdown from a standard OpenAPI/Swagger spec (fallback path)."""
    cache = {} if args.force else load_cache()
    new_cache: dict = {}

    paths = spec.get("paths", {})
    info = spec.get("info", {})

    # Parse endpoints
    endpoints: list[dict] = []
    for path, methods in sorted(paths.items()):
        for method, details in methods.items():
            if method.lower() in ("get", "post", "put", "patch", "delete"):
                endpoints.append({
                    "path": path,
                    "method": method.upper(),
                    "summary": details.get("summary", ""),
                    "description": details.get("description", ""),
                    "tags": details.get("tags", []),
                    "parameters": details.get("parameters", []),
                    "request_body": details.get("requestBody", {}),
                    "responses": details.get("responses", {}),
                    "deprecated": details.get("deprecated", False),
                })

    # Group by tag
    by_tag: dict[str, dict] = {}
    for ep in endpoints:
        tags = ep.get("tags", ["uncategorized"])
        for tag in tags:
            tag_slug = sanitize_filename(tag)
            if tag_slug not in by_tag:
                by_tag[tag_slug] = {"name": tag, "endpoints": []}

            path_slug = ep["path"].strip("/").replace("/", "-").replace("{", "").replace("}", "")
            filename = sanitize_filename(f"{ep['method'].lower()}-{path_slug}") + ".md"

            by_tag[tag_slug]["endpoints"].append({
                **ep,
                "filename": filename,
            })

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    total_endpoints = 0
    tag_index: list[tuple[str, str, int]] = []

    for tag_slug in sorted(by_tag):
        tag_data = by_tag[tag_slug]
        tag_name = tag_data["name"]
        tag_eps = sorted(tag_data["endpoints"], key=lambda e: e.get("path", ""))
        tag_dir = os.path.join(DOCS_DIR, tag_slug)

        if not args.dry_run:
            os.makedirs(tag_dir, exist_ok=True)

        for ep in tag_eps:
            ep_content = openapi_endpoint_to_markdown(ep)
            ep_path = os.path.join(tag_dir, ep["filename"])
            cache_key = f"cat:{tag_slug}:{ep['filename']}"
            content_hash = sha256(ep_content)

            if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(ep_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                is_new = cache_key not in cache or not os.path.exists(ep_path)
                if args.dry_run:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {tag_slug}/{ep['filename']}")
                else:
                    with open(ep_path, "w") as f:
                        f.write(ep_content)
                    if args.verbose:
                        print(f"  {'ADD' if is_new else 'UPDATE'} {tag_slug}/{ep['filename']}")
                new_cache[cache_key] = {
                    "sha256": content_hash,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

            total_endpoints += 1

        # Tag README
        readme_content = build_openapi_tag_readme(tag_name, tag_eps)
        readme_path = os.path.join(tag_dir, "README.md")
        cache_key = f"cat:{tag_slug}:README"
        content_hash = sha256(readme_content)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(readme_path)
            if args.dry_run:
                print(f"  {'ADD' if is_new else 'UPDATE'} {tag_slug}/README.md")
            else:
                with open(readme_path, "w") as f:
                    f.write(readme_content)
                if args.verbose:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {tag_slug}/README.md")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

        tag_index.append((tag_slug, tag_name, len(tag_eps)))

    # Top-level README
    main_content = build_openapi_main_readme(base_url, info, tag_index, total_endpoints)
    main_readme_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        with open(main_readme_path, "w") as f:
            f.write(main_content)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key not in new_cache:
            parts = old_key.split(":", 2)
            if len(parts) == 3 and parts[0] == "cat":
                old_path = os.path.join(DOCS_DIR, parts[1], parts[2])
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE {parts[1]}/{parts[2]}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE {parts[1]}/{parts[2]}")
                    removed += 1

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
    print(f"  Added:      {added}")
    print(f"  Updated:    {updated}")
    print(f"  Unchanged:  {unchanged}")
    print(f"  Removed:    {removed}")
    print(f"  Total files: {added + updated + unchanged}")
    print(f"  Total tags:  {len(tag_index)}")
    print(f"  Total endpoints: {total_endpoints}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cookie_header = build_cookie_header(args)
    base_url = args.base_url.rstrip("/")

    # Verify auth
    verify_auth(base_url, cookie_header)

    # Discover and fetch API spec
    spec, spec_url = discover_api_spec(base_url, cookie_header)
    if not spec:
        print("\nCould not discover API spec.", file=sys.stderr)
        print("Make sure your cookies are valid and the console is reachable.", file=sys.stderr)
        sys.exit(1)

    print(f"\nSpec URL: {spec_url}")
    if isinstance(spec, dict):
        print(f"Top-level keys: {list(spec.keys())}")

    # Save raw spec
    if not args.dry_run:
        raw = json.dumps(spec, indent=2, default=str)
        with open(SPEC_FILE, "w") as f:
            f.write(raw)
            f.write("\n")
        print(f"Saved raw spec to {SPEC_FILE}")

    # Route to the appropriate sync path
    if "apiList" in spec and "content" in spec:
        print("\nDetected S1 custom API spec format.")
        sync_s1(spec, base_url, args)
    elif "paths" in spec or "swagger" in spec or "openapi" in spec:
        print("\nDetected standard OpenAPI/Swagger spec format.")
        sync_openapi(spec, base_url, args)
    else:
        print("\nUnrecognized spec format. Raw JSON has been saved for manual review.",
              file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Fetch SentinelOne API docs from a management console and convert to markdown"
    )
    parser.add_argument(
        "--base-url",
        required=True,
        help="SentinelOne management console URL (e.g. https://your-console.sentinelone.net)",
    )

    auth = parser.add_mutually_exclusive_group(required=True)
    auth.add_argument(
        "--cookie",
        help="Cookie string for authentication (e.g. 'Authorization=Token eyJ...')",
    )
    auth.add_argument(
        "--cookie-file",
        help="Path to file containing cookie string for authentication",
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
