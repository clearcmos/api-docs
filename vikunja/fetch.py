#!/usr/bin/env python3

"""
Vikunja API Documentation Fetcher

Fetches the Swagger 2.0 spec from a live Vikunja instance (the public demo
at try.vikunja.io by default) and converts it into markdown grouped by tag.
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

DEFAULT_SPEC_URL = "https://try.vikunja.io/api/v1/docs.json"
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
            "User-Agent": "vikunja-api-docs-fetcher/1.0",
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
    """Resolve a $ref pointer like '#/definitions/Foo'."""
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = spec
    for part in parts:
        node = node.get(part, {})
        if not isinstance(node, dict):
            return {}
    return node


def schema_to_markdown(schema: dict, spec: dict, depth: int = 0, seen: set | None = None) -> str:
    """Convert a JSON schema (Swagger 2.0 flavor) to a readable markdown description."""
    if seen is None:
        seen = set()

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return "`(circular reference)`"
        seen = seen | {ref}
        resolved = resolve_ref(ref, spec)
        if not resolved:
            return f"`{ref.split('/')[-1]}`"
        schema = resolved

    one_of = schema.get("oneOf", [])
    any_of = schema.get("anyOf", [])
    all_of = schema.get("allOf", [])

    if all_of:
        merged_props: dict = {}
        merged_required: list = []
        merged: dict = {}
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

    schema_type = schema.get("type", "")

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


def format_parameters(parameters: list[dict], spec: dict) -> tuple[str, list[dict]]:
    """Format non-body parameters as a table; return (markdown, body_params)."""
    if not parameters:
        return "", []

    resolved_params = []
    for p in parameters:
        if "$ref" in p:
            p = resolve_ref(p["$ref"], spec)
        resolved_params.append(p)

    body_params = [p for p in resolved_params if p.get("in") == "body"]
    other_params = [p for p in resolved_params if p.get("in") != "body"]

    if not other_params:
        return "", body_params

    lines = ["### Parameters\n"]
    lines.append("| Name | In | Type | Required | Description |")
    lines.append("|------|-----|------|----------|-------------|")

    for param in other_params:
        name = param.get("name", "")
        location = param.get("in", "")
        # In Swagger 2.0 non-body params, type fields live directly on the param.
        param_type = param.get("type", "string")
        if param.get("format"):
            param_type += f" ({param['format']})"
        if param_type == "array":
            items = param.get("items", {})
            item_type = items.get("type", "")
            if item_type:
                param_type = f"array of {item_type}"
        enum = param.get("enum")
        if enum:
            enum_str = ", ".join(f"`{e}`" for e in enum[:10])
            if len(enum) > 10:
                enum_str += f", ... ({len(enum)} total)"
            param_type += f" -- enum: {enum_str}"
        required = "Yes" if param.get("required", False) else "No"
        description = param.get("description", "").replace("\n", " ").replace("|", "\\|")
        lines.append(f"| `{name}` | {location} | {param_type} | {required} | {description} |")

    lines.append("")
    return "\n".join(lines), body_params


def format_body_params(body_params: list[dict], consumes: list[str], spec: dict) -> str:
    if not body_params:
        return ""

    lines = ["### Request Body\n"]
    if consumes:
        lines.append(f"**Content-Type:** {', '.join(f'`{c}`' for c in consumes)}\n")

    for bp in body_params:
        name = bp.get("name", "body")
        description = bp.get("description", "")
        required = bp.get("required", False)
        schema = bp.get("schema", {})

        if description:
            lines.append(f"**{name}** - {description}")
        else:
            lines.append(f"**{name}**")
        if required:
            lines.append("\n**Required:** Yes")
        lines.append("")
        if schema:
            lines.append("**Schema:**\n")
            lines.append(schema_to_markdown(schema, spec))
            lines.append("")

    return "\n".join(lines)


def format_responses(responses: dict, produces: list[str], spec: dict) -> str:
    if not responses:
        return ""

    lines = ["### Responses\n"]
    if produces:
        lines.append(f"**Content-Type:** {', '.join(f'`{c}`' for c in produces)}\n")

    for status_code, response_data in sorted(responses.items()):
        if "$ref" in response_data:
            response_data = resolve_ref(response_data["$ref"], spec)

        description = response_data.get("description", "")
        lines.append(f"#### {status_code}")
        if description:
            lines.append(f"\n{description}\n")
        else:
            lines.append("")

        schema = response_data.get("schema", {})
        if schema:
            lines.append("**Schema:**\n")
            lines.append(schema_to_markdown(schema, spec))
            lines.append("")

    return "\n".join(lines)


def build_base_url(spec: dict, override_host: str | None) -> str:
    schemes = spec.get("schemes") or ["https"]
    scheme = schemes[0] if schemes else "https"
    host = override_host or spec.get("host") or ""
    base_path = spec.get("basePath", "")
    if not host:
        return base_path or ""
    return f"{scheme}://{host}{base_path}"


def build_endpoint_markdown(path: str, method: str, operation: dict, spec: dict, base_url: str) -> str:
    lines = []

    summary = operation.get("summary", f"{method.upper()} {path}")
    lines.append(f"# {summary}\n")

    description = operation.get("description", "")
    if description:
        lines.append(f"{description}\n")

    if operation.get("deprecated", False):
        lines.append("**DEPRECATED**\n")

    lines.append("## Request\n")
    lines.append(f"**Method:** `{method.upper()}`\n")
    lines.append(f"**URL:** `{path}`\n")

    operation_id = operation.get("operationId", "")
    if operation_id:
        lines.append(f"**Operation ID:** `{operation_id}`\n")

    tags = operation.get("tags", [])
    if tags:
        lines.append(f"**Tags:** {', '.join(f'`{t}`' for t in tags)}\n")

    if base_url:
        lines.append(f"**Base URL:** `{base_url}`\n")

    # Parameters: body params pulled out for separate section.
    parameters = operation.get("parameters", [])
    params_md, body_params = format_parameters(parameters, spec)
    if params_md:
        lines.append(params_md)

    consumes = operation.get("consumes", spec.get("consumes", []))
    if body_params:
        lines.append(format_body_params(body_params, consumes, spec))

    produces = operation.get("produces", spec.get("produces", []))
    responses = operation.get("responses", {})
    if responses:
        lines.append(format_responses(responses, produces, spec))

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
        lines.append(f"- [{method} {ep['path']}](./{filename}) -- {summary}")
    lines.append("")
    return "\n".join(lines)


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print(f"Fetching Vikunja Swagger spec from {args.spec_url}...")
    raw = fetch_url(args.spec_url)
    if not raw:
        sys.exit(1)

    spec = json.loads(raw)
    print(f"  Swagger version: {spec.get('swagger', '?')}")
    print(f"  API version: {spec.get('info', {}).get('version', '?')}")

    if not args.dry_run:
        with open(SPEC_FILE, "w") as f:
            f.write(raw)
            f.write("\n")

    paths = spec.get("paths", {})
    print(f"  Paths: {len(paths)}")

    base_url = build_base_url(spec, args.host)

    # Swagger 2.0 puts tag descriptions at the top level under "tags".
    tag_descriptions = {t["name"]: t.get("description", "") for t in spec.get("tags", [])}

    endpoints_by_tag: dict[str, list[dict]] = {}
    for path, path_item in paths.items():
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            if method not in path_item:
                continue
            operation = path_item[method]
            if not isinstance(operation, dict):
                continue
            tags = operation.get("tags", ["Untagged"])
            summary = operation.get("summary", f"{method.upper()} {path}")
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
    new_cache: dict = {}

    for tag in sorted(endpoints_by_tag.keys()):
        endpoints = endpoints_by_tag[tag]
        safe_tag = sanitize_filename(tag)
        tag_dir = os.path.join(DOCS_DIR, safe_tag)
        tag_desc = tag_descriptions.get(tag, "")

        if not args.dry_run:
            os.makedirs(tag_dir, exist_ok=True)

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

        for ep in endpoints:
            ep_content = build_endpoint_markdown(ep["path"], ep["method"], ep["operation"], spec, base_url)
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

    # Top-level README
    main_lines = ["# Vikunja API Documentation\n"]
    info = spec.get("info", {})
    desc = info.get("description", "")
    if desc:
        main_lines.append(f"{desc}\n")
    main_lines.append(f"**Version:** {info.get('version', '?')}\n")
    if base_url:
        main_lines.append(f"**Base URL:** `{base_url}`\n")
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

    if not args.dry_run:
        for entry in os.scandir(DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)
                if args.verbose:
                    print(f"  RMDIR {entry.name}/")

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
        description="Fetch Vikunja API docs from the Swagger spec and convert to markdown"
    )
    parser.add_argument(
        "--spec-url",
        default=DEFAULT_SPEC_URL,
        help=f"URL of the Swagger JSON (default: {DEFAULT_SPEC_URL})",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="Override the host in the rendered base URL (spec's own host is often empty)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
