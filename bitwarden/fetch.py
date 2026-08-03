#!/usr/bin/env python3

"""
Bitwarden Documentation Fetcher

Fetches two sources:
  1. CLI help article from the bitwarden/help GitHub repo (Jekyll markdown)
  2. Vault Management API OpenAPI spec embedded in the bitwarden.com SPA

The bitwarden.com/help site is a JS SPA, so rendered HTML cannot be fetched
with stdlib alone. The CLI source comes from the Jekyll repo; the API spec
is extracted from the Inertia.js data-page attribute in the page HTML.
"""

import argparse
import gzip
import hashlib
import html as html_mod
import json
import os
import re
import sys
from datetime import UTC, datetime
from typing import Any, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CLI_SOURCE_URL = "https://raw.githubusercontent.com/bitwarden/help/master/_articles/miscellaneous/cli.md"
API_PAGE_URL = "https://bitwarden.com/help/vault-management-api/"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
API_DOCS_DIR = os.path.join(DOCS_DIR, "vault-management-api")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SPEC_FILE = os.path.join(SCRIPT_DIR, "openapi-vault-management.json")


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": "bitwarden-docs-fetcher/1.0",
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


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return cast(dict[str, Any], json.load(f))
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)


def write_file(path: str, content: str, args, cache: dict, new_cache: dict, cache_key: str) -> None:
    """Write a file if content changed, respecting dry-run and cache."""
    content_hash = sha256(content)
    new_cache[cache_key] = {
        "sha256": content_hash,
        "last_updated": datetime.now(UTC).isoformat(),
    }
    cached = cache.get(cache_key, {})
    if cached.get("sha256") == content_hash and os.path.exists(path):
        if args.verbose:
            print(f"  SKIP (unchanged): {path}")
        return
    if args.dry_run:
        action = "UPDATE" if os.path.exists(path) else "CREATE"
        print(f"  {action}: {path}")
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(content)
        print(f"  WROTE: {path}")


def cache_key_path(cache_key: str) -> str | None:
    """Map a cache key to its generated output path."""
    if cache_key == "cli":
        return os.path.join(DOCS_DIR, "cli.md")
    if cache_key == "api/README":
        return os.path.join(API_DOCS_DIR, "README.md")
    if not cache_key.startswith("api/"):
        return None
    relative = cache_key.removeprefix("api/")
    if relative.endswith("/README"):
        relative += ".md"
    return os.path.join(API_DOCS_DIR, relative)


def preserve_cached_scope(cache: dict, new_cache: dict, prefix: str) -> None:
    """Carry forward cache entries whose source failed and output still exists."""
    for key, entry in cache.items():
        if key != prefix and not key.startswith(prefix + "/"):
            continue
        output_path = cache_key_path(key)
        if output_path is not None and os.path.exists(output_path):
            new_cache[key] = entry


# ---------------------------------------------------------------------------
# Jekyll markdown to clean markdown (CLI doc)
# ---------------------------------------------------------------------------


def strip_frontmatter(text: str) -> tuple[str, str]:
    """Remove YAML frontmatter. Returns (body, title)."""
    title = ""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end != -1:
            fm = text[3:end]
            for line in fm.splitlines():
                if line.startswith("title:"):
                    title = line.split(":", 1)[1].strip().strip('"')
            text = text[end + 3 :].lstrip("\n")
    return text, title


def clean_jekyll(text: str) -> str:
    """Strip Jekyll/Liquid template tags and convert to clean markdown."""
    text = re.sub(r"\{%\s*capture\s+\w+\s*%\}", "", text)
    text = re.sub(r"\{%\s*endcapture\s*%\}", "", text)
    text = re.sub(r"\{\{\s*\w+\s*\|\s*markdownify\s*\}\}", "", text)

    def replace_callout(m: re.Match) -> str:
        callout_type = m.group(1).strip()
        label_map = {
            "success": "Tip",
            "warning": "Warning",
            "info": "Note",
            "note": "Note",
            "danger": "Warning",
            "primary": "Note",
        }
        return f"\n> **{label_map.get(callout_type, 'Note')}:**"

    text = re.sub(r"\{%\s*callout\s+(\w+)\s*%\}", replace_callout, text)
    text = re.sub(r"\{%\s*endcallout\s*%\}", "\n", text)
    text = re.sub(r"\{%\s*image\s+([\w./-]+)\s*%\}", "", text)
    text = re.sub(r"\{%\s*icon\s+[^%]*%\}", "", text)
    text = re.sub(r"\{%\s*(?:raw|endraw)\s*%\}", "", text)
    text = re.sub(
        r"<ul[^>]*class=\"nav nav-tabs\"[^>]*>.*?</ul>",
        "",
        text,
        flags=re.DOTALL,
    )
    text = re.sub(r"<div[^>]*class=\"tab-content\"[^>]*>", "", text)
    text = re.sub(r"<div[^>]*class=\"tab-pane[^\"]*\"[^>]*>", "", text)
    text = re.sub(r"</div>\s*(?=\n)", "", text)
    text = re.sub(r"\{:\s*[^}]+\}", "", text)
    text = re.sub(r"\{\{[^}]*\}\}", "", text)
    text = re.sub(r"\{%[^%]*%\}", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def convert_cli(raw: str) -> str:
    text, title = strip_frontmatter(raw)
    text = clean_jekyll(text)
    if title:
        text = f"# {title}\n\n{text}"
    return text


# ---------------------------------------------------------------------------
# OpenAPI spec extraction and conversion (Vault Management API)
# ---------------------------------------------------------------------------


def extract_openapi_from_page(page_html: str) -> dict | None:
    """Extract the OpenAPI spec from the Inertia.js data-page attribute."""
    m = re.search(r'data-page="([^"]+)"', page_html)
    if not m:
        return None
    try:
        data = json.loads(html_mod.unescape(m.group(1)))
    except (json.JSONDecodeError, ValueError):
        return None

    def find_body(obj: object, depth: int = 0) -> object | None:
        if depth > 10:
            return None
        if isinstance(obj, dict):
            if obj.get("slug") == "vault-management-api" and "body" in obj:
                return cast(object, obj["body"])
            for v in obj.values():
                r = find_body(v, depth + 1)
                if r is not None:
                    return r
        elif isinstance(obj, list):
            for v in obj:
                r = find_body(v, depth + 1)
                if r is not None:
                    return r
        return None

    return cast(dict[str, Any] | None, find_body(data))


def resolve_ref(ref: str, spec: dict) -> dict:
    if not ref.startswith("#/"):
        return {}
    parts = ref[2:].split("/")
    node = spec
    for part in parts:
        node = node.get(part, {})
        if not isinstance(node, dict):
            return {}
    return node


def schema_to_markdown(
    schema: dict,
    spec: dict,
    depth: int = 0,
    seen: set | None = None,
) -> str:
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
        merged: dict = {}
        merged_props: dict = {}
        merged_required: list = []
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
        parts = [schema_to_markdown(v, spec, depth, seen) for v in variants[:5]]
        label = "One of" if one_of else "Any of"
        if len(variants) > 5:
            parts.append(f"... and {len(variants) - 5} more")
        return f"{label}: " + " | ".join(parts)

    if schema_type == "array":
        items = schema.get("items", {})
        return f"array of {schema_to_markdown(items, spec, depth, seen)}"

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
            result += f" - enum: {enum_str}"
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
        example = content_data.get("example")
        if example:
            lines.append("**Example:**\n")
            lines.append("```json")
            lines.append(json.dumps(example, indent=2))
            lines.append("```\n")
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
            example = content_data.get("example")
            if example:
                lines.append("**Example:**\n")
                lines.append("```json")
                lines.append(json.dumps(example, indent=2))
                lines.append("```\n")
    return "\n".join(lines)


def slugify(text: str) -> str:
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text.lower().strip("-")


def endpoint_filename(method: str, path: str) -> str:
    slug = path.strip("/").replace("/", "-").replace("{", "").replace("}", "")
    return f"{method.lower()}-{slug}.md"


def build_endpoint_markdown(
    path: str,
    method: str,
    operation: dict,
    spec: dict,
) -> str:
    lines = []
    summary = operation.get("summary", f"{method.upper()} {path}")
    lines.append(f"# {summary}\n")

    description = operation.get("description", "")
    if description:
        lines.append(f"{description}\n")

    deprecated = operation.get("deprecated", False)
    if deprecated:
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

    parameters = operation.get("parameters", [])
    if parameters:
        lines.append(format_parameters(parameters, spec))

    request_body = operation.get("requestBody")
    if request_body:
        lines.append(format_request_body(request_body, spec))

    responses = operation.get("responses", {})
    if responses:
        lines.append(format_responses(responses, spec))

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
        lines.append(f"- [{method} {ep['path']}](./{filename}) - {summary}")
    lines.append("")
    return "\n".join(lines)


def process_openapi(spec: dict, args, cache: dict, new_cache: dict) -> None:
    """Convert OpenAPI spec to per-tag directories with per-endpoint files."""
    # Group endpoints by tag
    tag_endpoints: dict[str, list[dict]] = {}
    tag_descs: dict[str, str] = {}

    for tag_info in spec.get("tags", []):
        tag_descs[tag_info["name"]] = tag_info.get("description", "")

    for path, path_item in spec.get("paths", {}).items():
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            if method not in path_item:
                continue
            operation = path_item[method]
            tags = operation.get("tags", ["Untagged"])
            summary = operation.get("summary", f"{method.upper()} {path}")
            fname = endpoint_filename(method, path)

            for tag in tags:
                tag_endpoints.setdefault(tag, []).append(
                    {
                        "path": path,
                        "method": method,
                        "summary": summary,
                        "filename": fname,
                        "operation": operation,
                    }
                )

    # Write per-tag directories and endpoint files
    for tag, endpoints in sorted(tag_endpoints.items()):
        tag_slug = slugify(tag)
        tag_dir = os.path.join(API_DOCS_DIR, tag_slug)

        # Tag README
        readme_content = build_tag_readme(tag, tag_descs.get(tag, ""), endpoints)
        readme_path = os.path.join(tag_dir, "README.md")
        write_file(
            readme_path,
            readme_content,
            args,
            cache,
            new_cache,
            f"api/{tag_slug}/README",
        )

        # Individual endpoint files
        for ep in endpoints:
            ep_content = build_endpoint_markdown(
                ep["path"],
                ep["method"],
                ep["operation"],
                spec,
            )
            ep_path = os.path.join(tag_dir, ep["filename"])
            write_file(
                ep_path,
                ep_content,
                args,
                cache,
                new_cache,
                f"api/{tag_slug}/{ep['filename']}",
            )

    # Top-level API README
    top_lines = ["# Vault Management API\n"]
    info = spec.get("info", {})
    desc = info.get("description", "")
    if desc:
        top_lines.append(f"{desc}\n")
    top_lines.append("## Endpoint Groups\n")
    for tag in sorted(tag_endpoints.keys()):
        tag_slug = slugify(tag)
        count = len(tag_endpoints[tag])
        top_lines.append(f"- [{tag}](./{tag_slug}/) ({count} endpoints)")
    top_lines.append("")
    top_readme = "\n".join(top_lines)
    write_file(
        os.path.join(API_DOCS_DIR, "README.md"),
        top_readme,
        args,
        cache,
        new_cache,
        "api/README",
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Fetch Bitwarden CLI and Vault Management API documentation")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would change, write nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Ignore cache, regenerate everything",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Per-file logging",
    )
    args = parser.parse_args()

    cache = {} if args.force else load_cache()
    new_cache: dict[str, dict[str, str]] = {}

    # --- CLI doc ---
    print(f"Fetching CLI doc from {CLI_SOURCE_URL} ...")
    raw_cli = fetch_url(CLI_SOURCE_URL)
    if raw_cli is None:
        print("WARNING: Could not fetch CLI doc, skipping.", file=sys.stderr)
        preserve_cached_scope(cache, new_cache, "cli")
    else:
        markdown = convert_cli(raw_cli)
        write_file(
            os.path.join(DOCS_DIR, "cli.md"),
            markdown,
            args,
            cache,
            new_cache,
            "cli",
        )

    # --- Vault Management API ---
    print(f"Fetching Vault Management API from {API_PAGE_URL} ...")
    raw_page = fetch_url(API_PAGE_URL)
    if raw_page is None:
        print("WARNING: Could not fetch API page, skipping.", file=sys.stderr)
        preserve_cached_scope(cache, new_cache, "api")
    else:
        spec = extract_openapi_from_page(raw_page)
        if spec is None:
            print(
                "WARNING: Could not extract OpenAPI spec from page.",
                file=sys.stderr,
            )
            preserve_cached_scope(cache, new_cache, "api")
        else:
            # Save raw spec
            if not args.dry_run:
                os.makedirs(SCRIPT_DIR, exist_ok=True)
                with open(SPEC_FILE, "w") as f:
                    json.dump(spec, f, indent=2)
                if args.verbose:
                    print(f"  WROTE: {SPEC_FILE}")

            process_openapi(spec, args, cache, new_cache)

    # Detect removals
    old_keys = set(cache.keys())
    new_keys = set(new_cache.keys())
    removed = old_keys - new_keys
    for key in sorted(removed):
        output_path = cache_key_path(key)
        if output_path is None or not os.path.exists(output_path):
            continue
        if args.dry_run:
            print(f"  REMOVE: {output_path}")
        else:
            os.remove(output_path)
            print(f"  REMOVED: {output_path}")

    if not args.dry_run:
        save_cache(new_cache)

    print("Done.")


if __name__ == "__main__":
    main()
