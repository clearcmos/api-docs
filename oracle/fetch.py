#!/usr/bin/env python3

"""
Oracle Cloud Infrastructure API Documentation Fetcher

Fetches all OCI OpenAPI specs from Oracle's published spec index and converts
them to local markdown. Uses concurrent downloads for speed.

Requires: pyyaml (specs are YAML-only, no JSON alternative available)
"""

import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import yaml

SPEC_INDEX_URL = "https://docs.oracle.com/en-us/iaas/api/specs/index.json"
SPEC_BASE_URL = "https://docs.oracle.com/en-us/iaas/api/specs/"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
INDEX_FILE = os.path.join(SCRIPT_DIR, "spec-index.json")

MAX_WORKERS = 8
USER_AGENT = "oracle-docs-fetcher/1.0"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> bytes | None:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read()
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        print(f"  ERROR: {url}: {e}", file=sys.stderr)
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
# OpenAPI-to-markdown helpers
# ---------------------------------------------------------------------------

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


def schema_to_markdown(schema: dict, spec: dict, depth: int = 0,
                       seen: set | None = None) -> str:
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
        return result

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
        if "$ref" in param_schema:
            param_schema = resolve_ref(param_schema["$ref"], spec)
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

    for status_code, response_data in sorted(responses.items(), key=lambda x: str(x[0])):
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


def build_endpoint_markdown(path: str, method: str, operation: dict,
                            spec: dict, api_title: str) -> str:
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

    lines.append(f"**API:** `{api_title}`\n")

    # Merge path-level and operation-level parameters
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


def build_tag_readme(tag: str, tag_desc: str, endpoints: list[dict],
                     api_title: str) -> str:
    lines = [f"# {tag}\n"]
    if tag_desc:
        lines.append(f"{tag_desc}\n")
    lines.append(f"**API:** {api_title}\n")
    lines.append("## Endpoints\n")
    for ep in sorted(endpoints, key=lambda x: (x["path"], x["method"])):
        method = ep["method"].upper()
        summary = ep["summary"]
        filename = ep["filename"]
        lines.append(f"- [{method} {ep['path']}](./{filename}) -- {summary}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Spec fetching and processing
# ---------------------------------------------------------------------------

def fetch_spec_index() -> dict:
    """Fetch the OCI API spec index."""
    print("Fetching spec index...")
    data = fetch_url(SPEC_INDEX_URL)
    if data is None:
        print("ERROR: Failed to fetch spec index", file=sys.stderr)
        sys.exit(1)
    return json.loads(data.decode("utf-8"))


def fetch_spec_file(spec_path: str) -> bytes | None:
    """Fetch a single spec YAML file."""
    # spec_path is like ./specs/abc123.yaml
    filename = spec_path.lstrip("./")
    url = f"https://docs.oracle.com/en-us/iaas/api/{filename}"
    return fetch_url(url, timeout=120)


def download_specs(index: dict, verbose: bool) -> dict[str, tuple[str, dict]]:
    """Download all spec files concurrently.

    Returns {api_key: (toc_title, parsed_spec)}.
    """
    # Build download tasks: [(api_key, toc_title, spec_url), ...]
    tasks = []
    for api_key, info in index.items():
        toc_title = info.get("toc_title", api_key)
        for spec_path in info.get("specs", []):
            tasks.append((api_key, toc_title, spec_path))

    print(f"Downloading {len(tasks)} spec files with {MAX_WORKERS} workers...")

    results: dict[str, tuple[str, dict]] = {}
    errors = 0

    def _download(task):
        api_key, toc_title, spec_path = task
        data = fetch_spec_file(spec_path)
        return api_key, toc_title, spec_path, data

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(_download, t): t for t in tasks}
        done_count = 0
        for future in concurrent.futures.as_completed(futures):
            done_count += 1
            api_key, toc_title, spec_path, data = future.result()
            if data is None:
                errors += 1
                continue
            try:
                spec = yaml.safe_load(data)
                if not isinstance(spec, dict):
                    if verbose:
                        print(f"  SKIP (not a dict): {api_key}")
                    errors += 1
                    continue
                results[api_key] = (toc_title, spec)
                if verbose:
                    print(f"  [{done_count}/{len(tasks)}] {toc_title}")
                elif done_count % 20 == 0:
                    print(f"  ...{done_count}/{len(tasks)} downloaded")
            except yaml.YAMLError as e:
                print(f"  ERROR parsing {api_key}: {e}", file=sys.stderr)
                errors += 1

    print(f"Downloaded {len(results)} specs ({errors} errors)")
    return results


def process_spec(api_key: str, toc_title: str, spec: dict,
                 cache: dict, new_cache: dict, force: bool,
                 dry_run: bool, verbose: bool) -> tuple[int, int, list[tuple[str, str]]]:
    """Process a single API spec into markdown files.

    Returns (written_count, skipped_count, [(filename, title), ...] for index).
    """
    api_dir_name = sanitize_filename(api_key)
    api_dir = os.path.join(DOCS_DIR, api_dir_name)
    written = 0
    skipped = 0

    # Determine OpenAPI version
    paths = spec.get("paths", {})
    if not paths:
        return 0, 0, []

    # Collect tag descriptions
    tag_descriptions = {}
    for tag_info in spec.get("tags", []):
        tag_descriptions[tag_info["name"]] = tag_info.get("description", "")

    # Group endpoints by tag
    tag_endpoints: dict[str, list[dict]] = {}
    default_tag = api_key

    for path, path_item in paths.items():
        # Collect path-level parameters
        path_params = path_item.get("parameters", [])

        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            operation = path_item.get(method)
            if not operation:
                continue

            # Merge path-level params with operation params
            op_params = list(path_params) + operation.get("parameters", [])
            merged_op = dict(operation)
            if op_params:
                merged_op["parameters"] = op_params

            tags = operation.get("tags", [default_tag])
            summary = operation.get("summary", f"{method.upper()} {path}")

            # Build filename
            path_slug = re.sub(r"[{}]", "", path)
            path_slug = re.sub(r"[^\w/-]", "", path_slug)
            path_slug = path_slug.strip("/").replace("/", "-")
            filename = f"{method}-{sanitize_filename(path_slug)}.md"

            # Build markdown
            markdown = build_endpoint_markdown(path, method, merged_op, spec, toc_title)
            content_hash = sha256(markdown)
            cache_key = f"api:{api_key}:{method}:{path}"

            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }

            for tag in tags:
                tag_slug = sanitize_filename(tag)
                tag_dir = os.path.join(api_dir, tag_slug) if tag_slug != api_dir_name else api_dir
                full_path = os.path.join(tag_dir, filename)

                tag_endpoints.setdefault(tag, []).append({
                    "path": path,
                    "method": method,
                    "summary": summary,
                    "filename": filename,
                })

                if (not force and cache_key in cache
                        and cache[cache_key].get("sha256") == content_hash
                        and os.path.exists(full_path)):
                    skipped += 1
                    continue

                if dry_run:
                    print(f"  WOULD WRITE: {full_path}")
                    written += 1
                    continue

                os.makedirs(tag_dir, exist_ok=True)
                with open(full_path, "w") as f:
                    f.write(markdown)
                written += 1

    # Write tag READMEs
    index_entries = []
    for tag, endpoints in sorted(tag_endpoints.items()):
        tag_slug = sanitize_filename(tag)
        tag_desc = tag_descriptions.get(tag, "")
        readme_content = build_tag_readme(tag, tag_desc, endpoints, toc_title)
        readme_hash = sha256(readme_content)
        cache_key = f"index:{api_key}:{tag}"
        new_cache[cache_key] = {
            "sha256": readme_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }

        tag_dir = os.path.join(api_dir, tag_slug) if tag_slug != api_dir_name else api_dir
        readme_path = os.path.join(tag_dir, "README.md")
        index_entries.append((tag_slug, tag, len(endpoints)))

        if (not force and cache_key in cache
                and cache[cache_key].get("sha256") == readme_hash
                and os.path.exists(readme_path)):
            continue

        if not dry_run:
            os.makedirs(tag_dir, exist_ok=True)
            with open(readme_path, "w") as f:
                f.write(readme_content)

    # Write API-level README
    api_readme_lines = [f"# {toc_title}\n"]
    info = spec.get("info", {})
    desc = info.get("description", "")
    if desc:
        api_readme_lines.append(f"{desc}\n")
    version = info.get("version", "")
    if version:
        api_readme_lines.append(f"**Version:** {version}\n")

    servers = spec.get("servers", [])
    if servers:
        api_readme_lines.append("**Servers:**\n")
        for s in servers[:3]:
            api_readme_lines.append(f"- `{s.get('url', '')}`")
        if len(servers) > 3:
            api_readme_lines.append(f"- ... and {len(servers) - 3} more")
        api_readme_lines.append("")

    if index_entries:
        api_readme_lines.append("## Tags\n")
        for tag_slug, tag_name, count in sorted(index_entries, key=lambda x: x[1]):
            if tag_slug != api_dir_name:
                api_readme_lines.append(f"- [{tag_name}](./{tag_slug}/) ({count} endpoints)")
            else:
                api_readme_lines.append(f"- [{tag_name}](.) ({count} endpoints)")
        api_readme_lines.append("")

    total_endpoints = sum(len(eps) for eps in tag_endpoints.values())
    api_readme_lines.append(f"\n*{total_endpoints} endpoints total*\n")

    api_readme = "\n".join(api_readme_lines)
    if not dry_run:
        os.makedirs(api_dir, exist_ok=True)
        with open(os.path.join(api_dir, "README.md"), "w") as f:
            f.write(api_readme)

    return written, skipped, index_entries


def write_top_readme(api_summaries: list[tuple[str, str, int]],
                     dry_run: bool) -> None:
    """Write the top-level docs/README.md."""
    lines = ["# Oracle Cloud Infrastructure API Documentation\n"]
    lines.append(f"Generated from OCI OpenAPI specs. "
                 f"{len(api_summaries)} APIs.\n")
    lines.append("## APIs\n")
    for dir_name, title, endpoint_count in sorted(api_summaries, key=lambda x: x[1].lower()):
        lines.append(f"- [{title}](./{dir_name}/) ({endpoint_count} endpoints)")
    lines.append("")

    if dry_run:
        print(f"  WOULD WRITE: {os.path.join(DOCS_DIR, 'README.md')}")
    else:
        os.makedirs(DOCS_DIR, exist_ok=True)
        with open(os.path.join(DOCS_DIR, "README.md"), "w") as f:
            f.write("\n".join(lines))


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Oracle Cloud Infrastructure API documentation")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change, write nothing")
    parser.add_argument("--force", action="store_true",
                        help="Ignore cache, regenerate everything")
    parser.add_argument("--verbose", action="store_true",
                        help="Per-file logging")
    args = parser.parse_args()

    cache = {} if args.force else load_cache()
    new_cache = {}

    # Fetch and save the spec index
    index = fetch_spec_index()
    print(f"Found {len(index)} APIs in spec index")

    if not args.dry_run:
        with open(INDEX_FILE, "w") as f:
            json.dump(index, f, indent=2, sort_keys=True)
            f.write("\n")

    # Download all specs concurrently
    specs = download_specs(index, verbose=args.verbose)

    # Process each spec into markdown
    total_written = 0
    total_skipped = 0
    api_summaries = []

    print(f"Converting {len(specs)} specs to markdown...")
    for api_key, (toc_title, spec) in sorted(specs.items()):
        written, skipped, index_entries = process_spec(
            api_key, toc_title, spec, cache, new_cache,
            args.force, args.dry_run, args.verbose)
        total_written += written
        total_skipped += skipped

        # Count total endpoints for this API
        total_ep = sum(c for _, _, c in index_entries)
        if total_ep > 0:
            api_summaries.append((sanitize_filename(api_key), toc_title, total_ep))

        if args.verbose:
            print(f"  {toc_title}: {written} written, {skipped} cached")

    # Write top-level index
    write_top_readme(api_summaries, args.dry_run)

    # Remove stale files
    removed = 0
    if not args.dry_run and cache:
        for key in set(cache.keys()) - set(new_cache.keys()):
            if key.startswith("api:"):
                parts = key.split(":", 3)
                if len(parts) == 4:
                    _, ak, method, path = parts
                    path_slug = re.sub(r"[{}]", "", path)
                    path_slug = re.sub(r"[^\w/-]", "", path_slug)
                    path_slug = path_slug.strip("/").replace("/", "-")
                    filename = f"{method}-{sanitize_filename(path_slug)}.md"
                    api_dir = os.path.join(DOCS_DIR, sanitize_filename(ak))
                    fp = os.path.join(api_dir, filename)
                    if os.path.exists(fp):
                        os.remove(fp)
                        removed += 1

    if not args.dry_run:
        save_cache(new_cache)

    print(f"\nDone: {total_written} written, {total_skipped} cached, "
          f"{removed} removed, {len(api_summaries)} APIs")


if __name__ == "__main__":
    main()
