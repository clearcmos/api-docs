#!/usr/bin/env python3

"""
Radarr API Documentation Fetcher

Fetches the official Radarr OpenAPI (v3) spec from GitHub and converts it
into organized markdown files grouped by tag.

Radarr (like the other Servarr apps) publishes a generated OpenAPI 3.0 spec
in its source tree. Every operation has null summary/operationId/description,
so the markdown is derived from the path, method, tag and resolved schemas.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

OPENAPI_URL = "https://raw.githubusercontent.com/Radarr/Radarr/develop/src/Radarr.Api.V3/openapi.json"
APP_NAME = "Radarr"
SOURCE_DOCS_URL = "https://radarr.video/docs/api/"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SPEC_FILE = os.path.join(SCRIPT_DIR, "openapi.json")


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": "radarr-api-docs-fetcher/1.0",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return cast(str, data.decode("utf-8"))
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        print(f"ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
        return None


def sanitize_filename(name: str) -> str:
    name = re.sub(r"[^\w\s-]", "", name)
    name = re.sub(r"[-\s]+", "-", name)
    return name.lower().strip("-")


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return cast(dict[str, Any], json.load(f))
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def resolve_ref(ref: str, spec: dict) -> dict:
    """Resolve a $ref pointer like '#/components/schemas/Foo'."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = spec
    for part in parts:
        node = node.get(part, {})
        if not isinstance(node, dict):
            return {}
    return node


def resolve_server_url(spec: dict) -> str:
    """Resolve a server URL, substituting default values for {variables}."""
    servers = spec.get("servers", [])
    if not servers:
        return ""
    server = servers[0]
    url = cast(str, server.get("url", ""))
    for var, info in server.get("variables", {}).items():
        url = url.replace("{" + var + "}", str(info.get("default", "")))
    return url


def schema_to_markdown(schema: dict, spec: dict, depth: int = 0, seen: set | None = None) -> str:
    """Convert a JSON schema to a readable markdown description."""
    if seen is None:
        seen = set()

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return "`(circular reference)`"
        seen = seen | {ref}
        schema = resolve_ref(ref, spec)
        if not schema:
            return f"`{ref.split('/')[-1]}`"

    schema_type = schema.get("type", "")
    one_of = schema.get("oneOf", [])
    any_of = schema.get("anyOf", [])
    all_of = schema.get("allOf", [])

    if all_of:
        # Merge allOf schemas
        merged = {}
        merged_props = {}
        merged_required = []
        for sub in all_of:
            if "$ref" in sub:
                sub = resolve_ref(sub["$ref"], spec)
            merged_props.update(sub.get("properties", {}))
            merged_required.extend(sub.get("required", []))
            merged.update(sub)
        merged["properties"] = merged_props
        if merged_required:
            merged["required"] = merged_required
        return schema_to_markdown(merged, spec, depth, seen)

    if one_of or any_of:
        variants = one_of or any_of
        parts = []
        for v in variants[:5]:
            parts.append(schema_to_markdown(v, spec, depth, seen))
        label = "One of" if one_of else "Any of"
        if len(variants) > 5:
            parts.append(f"... and {len(variants) - 5} more")
        return f"{label}: " + " | ".join(parts)

    if schema_type == "array":
        items = schema.get("items", {})
        items_md = schema_to_markdown(items, spec, depth, seen)
        return f"array of {items_md}"

    if schema_type == "object" or "properties" in schema:
        props = schema.get("properties", {})
        required = set(schema.get("required", []))
        if not props:
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                return f"object (values: {schema_to_markdown(additional, spec, depth, seen)})"
            return "object"

        if depth > 1:
            return f"object ({len(props)} properties)"

        lines = []
        for name, prop in sorted(props.items()):
            req_mark = " **required**" if name in required else ""
            desc = prop.get("description", "").replace("\n", " ").strip()
            prop_type = schema_to_markdown(prop, spec, depth + 1, seen)
            if desc:
                lines.append(f"  - `{name}` ({prop_type}){req_mark}: {desc}")
            else:
                lines.append(f"  - `{name}` ({prop_type}){req_mark}")
        return "object\n" + "\n".join(lines)

    if schema_type:
        fmt = schema.get("format", "")
        enum = schema.get("enum")
        result = schema_type
        if fmt:
            result += f" ({fmt})"
        if enum:
            enum_str = ", ".join(f"`{e}`" for e in enum[:10])
            if len(enum) > 10:
                enum_str += f", ... ({len(enum)} total)"
            result += f" -- enum: {enum_str}"
        return cast(str, result)

    return "any"


def format_parameters(parameters: list[dict], spec: dict) -> str:
    if not parameters:
        return ""

    lines = ["### Parameters\n"]
    lines.append("| Name | In | Type | Required | Description |")
    lines.append("|------|-----|------|----------|-------------|")

    for param in parameters:
        if "$ref" in param:
            param = resolve_ref(param["$ref"], spec)
        name = param.get("name", "")
        location = param.get("in", "")
        param_schema = param.get("schema", {})
        param_type = param_schema.get("type", "string")
        if param_schema.get("format"):
            param_type += f" ({param_schema['format']})"
        required = "Yes" if param.get("required", False) else "No"
        description = param.get("description", "").replace("\n", " ").replace("|", "\\|")
        lines.append(f"| `{name}` | {location} | {param_type} | {required} | {description} |")

    lines.append("")
    return "\n".join(lines)


def format_request_body(request_body: dict, spec: dict) -> str:
    if not request_body:
        return ""

    if "$ref" in request_body:
        request_body = resolve_ref(request_body["$ref"], spec)

    lines = ["### Request Body\n"]

    description = request_body.get("description", "")
    if description:
        lines.append(f"{description}\n")

    required = request_body.get("required", False)
    if required:
        lines.append("**Required:** Yes\n")

    content = request_body.get("content", {})
    for content_type, content_data in content.items():
        lines.append(f"**Content-Type:** `{content_type}`\n")
        schema = content_data.get("schema", {})
        if schema:
            lines.append("**Schema:**\n")
            lines.append(schema_to_markdown(schema, spec))
            lines.append("")

    return "\n".join(lines)


def format_responses(responses: dict, spec: dict) -> str:
    if not responses:
        return ""

    lines = ["### Responses\n"]

    for status_code, response_data in sorted(responses.items()):
        if "$ref" in response_data:
            response_data = resolve_ref(response_data["$ref"], spec)

        description = response_data.get("description", "")
        lines.append(f"#### {status_code}")
        if description:
            lines.append(f"\n{description}\n")
        else:
            lines.append("")

        content = response_data.get("content", {})
        for content_type, content_data in content.items():
            lines.append(f"**Content-Type:** `{content_type}`\n")
            schema = content_data.get("schema", {})
            if schema:
                lines.append("**Schema:**\n")
                lines.append(schema_to_markdown(schema, spec))
                lines.append("")

    return "\n".join(lines)


def endpoint_summary(operation: dict, method: str, path: str) -> str | None:
    """Servarr specs leave summary null; fall back to None so callers can omit it."""
    summary = operation.get("summary")
    return summary if summary else None


def build_endpoint_markdown(path: str, method: str, operation: dict, spec: dict) -> str:
    lines = []

    summary = endpoint_summary(operation, method, path)
    lines.append(f"# {summary or f'{method.upper()} {path}'}\n")

    description = operation.get("description")
    if description:
        lines.append(f"{description}\n")

    deprecated = operation.get("deprecated", False)
    if deprecated:
        lines.append("**DEPRECATED**\n")

    lines.append("## Request\n")
    lines.append(f"**Method:** `{method.upper()}`\n")
    lines.append(f"**URL:** `{path}`\n")

    operation_id = operation.get("operationId")
    if operation_id:
        lines.append(f"**Operation ID:** `{operation_id}`\n")

    tags = operation.get("tags", [])
    if tags:
        lines.append(f"**Tags:** {', '.join(f'`{t}`' for t in tags)}\n")

    server_url = resolve_server_url(spec)
    if server_url:
        lines.append(f"**Base URL:** `{server_url}`\n")

    parameters = operation.get("parameters", [])
    if parameters:
        lines.append(format_parameters(parameters, spec))

    request_body = operation.get("requestBody")
    if request_body:
        lines.append(format_request_body(request_body, spec))

    responses = operation.get("responses", {})
    if responses:
        lines.append(format_responses(responses, spec))

    security = operation.get("security", spec.get("security", []))
    if security:
        lines.append("### Security\n")
        for sec in security:
            for scheme, scopes in sec.items():
                if scopes:
                    lines.append(f"- **{scheme}**: {', '.join(scopes)}")
                else:
                    lines.append(f"- **{scheme}**")
        lines.append("")

    return "\n".join(lines)


def build_tag_readme(tag: str, tag_desc: str, endpoints: list[dict]) -> str:
    lines = [f"# {tag}\n"]
    if tag_desc:
        lines.append(f"{tag_desc}\n")
    lines.append("## Endpoints\n")
    for ep in sorted(endpoints, key=lambda x: (x["path"], x["method"])):
        method = ep["method"].upper()
        summary = ep["summary"]
        filename = ep["filename"]
        if summary:
            lines.append(f"- [{method} {ep['path']}](./{filename}) -- {summary}")
        else:
            lines.append(f"- [{method} {ep['path']}](./{filename})")
    lines.append("")
    return "\n".join(lines)


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    # Fetch the OpenAPI spec
    print(f"Fetching {APP_NAME} OpenAPI spec...")
    raw = fetch_url(OPENAPI_URL)
    if not raw:
        sys.exit(1)

    spec = json.loads(raw)
    sha256(raw)
    print(f"  OpenAPI version: {spec.get('openapi', '?')}")
    print(f"  API version: {spec.get('info', {}).get('version', '?')}")

    # Save the raw spec
    if not args.dry_run:
        with open(SPEC_FILE, "w") as f:
            f.write(raw)
            f.write("\n")

    paths = spec.get("paths", {})
    print(f"  Paths: {len(paths)}")

    # Build tag descriptions lookup (Servarr specs have no top-level tags array)
    tag_descriptions = {}
    for tag_info in spec.get("tags", []) or []:
        tag_descriptions[tag_info["name"]] = tag_info.get("description", "")

    # Group endpoints by tag
    endpoints_by_tag: dict[str, list[dict]] = {}
    for path, path_item in paths.items():
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            if method not in path_item:
                continue
            operation = path_item[method]
            tags = operation.get("tags", ["Untagged"]) or ["Untagged"]
            summary = endpoint_summary(operation, method, path)
            safe_name = sanitize_filename(f"{method}-{path.replace('/', '-')}")
            filename = f"{safe_name}.md"

            for tag in tags:
                endpoints_by_tag.setdefault(tag, []).append(
                    {
                        "path": path,
                        "method": method,
                        "summary": summary,
                        "filename": filename,
                        "operation": operation,
                    }
                )

    print(f"  Tags: {len(endpoints_by_tag)}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    new_cache = {}

    for tag in sorted(endpoints_by_tag.keys()):
        endpoints = endpoints_by_tag[tag]
        safe_tag = sanitize_filename(tag)
        tag_dir = os.path.join(DOCS_DIR, safe_tag)
        tag_desc = tag_descriptions.get(tag, "")

        if not args.dry_run:
            os.makedirs(tag_dir, exist_ok=True)

        # Write tag README
        readme_content = build_tag_readme(tag, tag_desc, endpoints)
        readme_path = os.path.join(tag_dir, "README.md")
        cache_key = f"tag:{safe_tag}:README"
        content_hash = sha256(readme_content)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(readme_path)
            if args.dry_run:
                print(f"  {'ADD' if is_new else 'UPDATE'} {safe_tag}/README.md")
            else:
                with open(readme_path, "w") as f:
                    f.write(readme_content)
                if args.verbose:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {safe_tag}/README.md")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(UTC).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

        # Write endpoint files
        for ep in endpoints:
            ep_content = build_endpoint_markdown(ep["path"], ep["method"], ep["operation"], spec)
            ep_path = os.path.join(tag_dir, ep["filename"])
            cache_key = f"tag:{safe_tag}:{ep['filename']}"
            content_hash = sha256(ep_content)

            if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(ep_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                is_new = cache_key not in cache or not os.path.exists(ep_path)
                if args.dry_run:
                    print(f"  {'ADD' if is_new else 'UPDATE'} {safe_tag}/{ep['filename']}")
                else:
                    with open(ep_path, "w") as f:
                        f.write(ep_content)
                    if args.verbose:
                        print(f"  {'ADD' if is_new else 'UPDATE'} {safe_tag}/{ep['filename']}")
                new_cache[cache_key] = {
                    "sha256": content_hash,
                    "last_updated": datetime.now(UTC).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

    # Write top-level README
    info = spec.get("info", {})
    main_lines = [f"# {info.get('title', APP_NAME)} API Documentation\n"]
    desc = info.get("description", "")
    if desc:
        main_lines.append(f"{desc}\n")
    main_lines.append(f"**Version:** {info.get('version', '?')}\n")
    server_url = resolve_server_url(spec)
    if server_url:
        main_lines.append(f"**Base URL:** `{server_url}` (default; adjust host/port to your instance)\n")
    main_lines.append(f"**Source:** [{SOURCE_DOCS_URL}]({SOURCE_DOCS_URL})\n")

    sec_schemes = spec.get("components", {}).get("securitySchemes", {})
    if sec_schemes:
        main_lines.append("## Authentication\n")
        for name, scheme in sec_schemes.items():
            loc = scheme.get("in", "")
            key = scheme.get("name", name)
            sdesc = scheme.get("description", "")
            main_lines.append(f"- **{name}** (`{scheme.get('type', '')}`): `{key}` in {loc} -- {sdesc}")
        main_lines.append("")

    main_lines.append("## API Categories\n")
    for tag in sorted(endpoints_by_tag.keys()):
        safe_tag = sanitize_filename(tag)
        count = len(endpoints_by_tag[tag])
        main_lines.append(f"- [{tag}](./{safe_tag}/) ({count} endpoints)")
    main_lines.append("")
    main_content = "\n".join(main_lines)

    main_readme_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        with open(main_readme_path, "w") as f:
            f.write(main_content)

    # Detect removals: files in cache no longer generated
    removed = 0
    for old_key in sorted(cache):
        if old_key not in new_cache:
            parts = old_key.split(":", 2)
            if len(parts) == 3 and parts[0] == "tag":
                old_path = os.path.join(DOCS_DIR, parts[1], parts[2])
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE {parts[1]}/{parts[2]}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE {parts[1]}/{parts[2]}")
                    removed += 1

    # Clean up empty tag directories
    if not args.dry_run:
        for entry in os.scandir(DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)
                if args.verbose:
                    print(f"  RMDIR {entry.name}/")

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    total_endpoints = sum(len(eps) for eps in endpoints_by_tag.values())

    print("\nSync complete:")
    print(f"  Added:      {added}")
    print(f"  Updated:    {updated}")
    print(f"  Unchanged:  {unchanged}")
    print(f"  Removed:    {removed}")
    print(f"  Total files: {added + updated + unchanged}")
    print(f"  Total tags:  {len(endpoints_by_tag)}")
    print(f"  Total endpoints: {total_endpoints}")


def main():
    parser = argparse.ArgumentParser(
        description=f"Fetch {APP_NAME} API docs from the OpenAPI spec and convert to markdown"
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
    sync(args)


if __name__ == "__main__":
    main()
