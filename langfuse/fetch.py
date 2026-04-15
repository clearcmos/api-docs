#!/usr/bin/env python3

"""
Langfuse Documentation Fetcher

Fetches two documentation sources:

1. API Reference: Downloads the OpenAPI YAML spec from cloud.langfuse.com
   and converts each endpoint into organized markdown files grouped by tag.
   Output: docs/api/

2. Self-Hosting Docs: Downloads MDX content files from the langfuse-docs
   GitHub repository (content/self-hosting/**), strips JSX elements, and
   saves as markdown preserving directory structure.
   Output: docs/self-hosting/

Uses pyyaml (acceptable per project conventions for YAML-only specs).
All other deps are stdlib only.
"""

import argparse
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:
    print(
        "ERROR: pyyaml is required for this fetcher (YAML-only spec).\n"
        "Install it with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)

OPENAPI_SPEC_URL = "https://cloud.langfuse.com/generated/api/openapi.yml"
GITHUB_TREE_URL = "https://api.github.com/repos/langfuse/langfuse-docs/git/trees/main?recursive=1"
GITHUB_RAW_BASE = "https://raw.githubusercontent.com/langfuse/langfuse-docs/refs/heads/main"
SELF_HOSTING_PREFIX = "content/self-hosting"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
API_DOCS_DIR = os.path.join(DOCS_DIR, "api")
SELF_HOSTING_DOCS_DIR = os.path.join(DOCS_DIR, "self-hosting")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SPEC_FILE = os.path.join(SCRIPT_DIR, "openapi.yaml")


# ---------------------------------------------------------------------------
# Standard helpers
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={"User-Agent": "langfuse-docs-fetcher/1.0"})
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
# OpenAPI helpers
# ---------------------------------------------------------------------------

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
    # OpenAPI 3.1 allows type to be a list (e.g. ["string", "null"])
    if isinstance(schema_type, list):
        schema_type = " | ".join(str(t) for t in schema_type)

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

    if schema_type == "array" or (isinstance(schema.get("type"), str) and schema.get("type") == "array"):
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
        param_type = param_schema.get("type", "string")
        if isinstance(param_type, list):
            param_type = " | ".join(str(t) for t in param_type)
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


def build_endpoint_markdown(path: str, method: str, operation: dict, spec: dict) -> str:
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

    servers = spec.get("servers", [])
    if servers:
        lines.append(f"**Base URL:** `{servers[0].get('url', '')}`\n")

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
        lines.append(f"- [{method} {ep['path']}](./{filename}) -- {summary}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# MDX-to-markdown conversion
# ---------------------------------------------------------------------------

def mdx_to_markdown(mdx_content: str) -> str:
    """Convert MDX content to clean markdown.

    Strips JSX import statements and custom component tags while preserving
    markdown content, code blocks, and tables.
    """
    lines = mdx_content.split("\n")
    output_lines = []
    in_code_block = False

    for line in lines:
        # Track code blocks to avoid stripping content inside them
        if line.strip().startswith("```"):
            in_code_block = not in_code_block
            output_lines.append(line)
            continue

        if in_code_block:
            output_lines.append(line)
            continue

        # Skip import statements
        if re.match(r"^import\s+", line):
            continue

        # Skip export statements (e.g. export const metadata = ...)
        if re.match(r"^export\s+", line):
            continue

        # Skip JSX-only lines (self-closing components)
        if re.match(r"^\s*<[A-Z]\w+[^>]*/>\s*$", line):
            continue

        # Strip JSX wrapper component open/close tags, keep inner content
        line = re.sub(
            r"<(Callout|Note|Warning|Tip|Info|Steps|Step|Tabs|Tab|Frame|"
            r"Accordion|AccordionGroup|Card|Cards|CodeGroup|"
            r"Expandable|ResponseField|ParamField|OptionTable)[^>]*>",
            "",
            line,
        )
        line = re.sub(
            r"</(Callout|Note|Warning|Tip|Info|Steps|Step|Tabs|Tab|Frame|"
            r"Accordion|AccordionGroup|Card|Cards|CodeGroup|"
            r"Expandable|ResponseField|ParamField|OptionTable)>",
            "",
            line,
        )

        # Convert media components to placeholder
        line = re.sub(r"<CloudflareVideo[^/]*/?>", "[Video]", line)
        line = re.sub(r"<Frame[^>]*>", "", line)
        line = re.sub(r"</Frame>", "", line)

        output_lines.append(line)

    result = "\n".join(output_lines)
    # Clean up excessive blank lines
    result = re.sub(r"\n{3,}", "\n\n", result)
    return result.strip() + "\n"


# ---------------------------------------------------------------------------
# Part 1: sync_api -- OpenAPI spec to markdown
# ---------------------------------------------------------------------------

def sync_api(spec: dict, cache: dict, new_cache: dict, args: argparse.Namespace) -> tuple[int, int, int, int]:
    """Sync OpenAPI spec to markdown. Returns (added, updated, unchanged, removed)."""
    paths = spec.get("paths", {})

    tag_descriptions = {}
    for tag_info in spec.get("tags", []):
        tag_descriptions[tag_info["name"]] = tag_info.get("description", "")

    endpoints_by_tag: dict[str, list[dict]] = {}
    for path, path_item in paths.items():
        for method in ("get", "post", "put", "patch", "delete", "head", "options"):
            if method not in path_item:
                continue
            operation = path_item[method]
            tags = operation.get("tags", ["Untagged"])
            summary = operation.get("summary", f"{method.upper()} {path}")
            safe_name = sanitize_filename(f"{method}-{path.replace('/', '-')}")
            filename = f"{safe_name}.md"

            for tag in tags:
                if tag not in endpoints_by_tag:
                    endpoints_by_tag[tag] = []
                endpoints_by_tag[tag].append({
                    "path": path,
                    "method": method,
                    "summary": summary,
                    "filename": filename,
                    "operation": operation,
                })

    if not args.dry_run:
        os.makedirs(API_DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0

    for tag in sorted(endpoints_by_tag.keys()):
        endpoints = endpoints_by_tag[tag]
        safe_tag = sanitize_filename(tag)
        tag_dir = os.path.join(API_DOCS_DIR, safe_tag)
        tag_desc = tag_descriptions.get(tag, "")

        if not args.dry_run:
            os.makedirs(tag_dir, exist_ok=True)

        # Tag README
        readme_content = build_tag_readme(tag, tag_desc, endpoints)
        readme_path = os.path.join(tag_dir, "README.md")
        cache_key = f"api:{safe_tag}:README"
        content_hash = sha256(readme_content)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(readme_path)
            if args.dry_run:
                print(f"  {'ADD' if is_new else 'UPDATE'} api/{safe_tag}/README.md")
            else:
                with open(readme_path, "w") as f:
                    f.write(readme_content)
                if args.verbose:
                    print(f"  {'ADD' if is_new else 'UPDATE'} api/{safe_tag}/README.md")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

        # Endpoint files
        for ep in endpoints:
            ep_content = build_endpoint_markdown(ep["path"], ep["method"], ep["operation"], spec)
            ep_path = os.path.join(tag_dir, ep["filename"])
            cache_key = f"api:{safe_tag}:{ep['filename']}"
            content_hash = sha256(ep_content)

            if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(ep_path):
                unchanged += 1
                new_cache[cache_key] = cache[cache_key]
            else:
                is_new = cache_key not in cache or not os.path.exists(ep_path)
                if args.dry_run:
                    print(f"  {'ADD' if is_new else 'UPDATE'} api/{safe_tag}/{ep['filename']}")
                else:
                    with open(ep_path, "w") as f:
                        f.write(ep_content)
                    if args.verbose:
                        print(f"  {'ADD' if is_new else 'UPDATE'} api/{safe_tag}/{ep['filename']}")
                new_cache[cache_key] = {
                    "sha256": content_hash,
                    "last_updated": datetime.now(timezone.utc).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1

    # API-level README
    main_lines = ["# Langfuse API Reference\n"]
    info = spec.get("info", {})
    desc = info.get("description", "")
    if desc:
        main_lines.append(f"{desc}\n")
    main_lines.append(f"**Version:** {info.get('version', '?')}\n")
    servers = spec.get("servers", [])
    if servers:
        main_lines.append(f"**Base URL:** `{servers[0].get('url', '')}`\n")
    main_lines.append("## API Categories\n")
    for tag in sorted(endpoints_by_tag.keys()):
        safe_tag = sanitize_filename(tag)
        count = len(endpoints_by_tag[tag])
        main_lines.append(f"- [{tag}](./{safe_tag}/) ({count} endpoints)")
    main_lines.append("")
    main_content = "\n".join(main_lines)

    if not args.dry_run:
        with open(os.path.join(API_DOCS_DIR, "README.md"), "w") as f:
            f.write(main_content)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key.startswith("api:") and old_key not in new_cache:
            parts = old_key.split(":", 2)
            if len(parts) == 3:
                old_path = os.path.join(API_DOCS_DIR, parts[1], parts[2])
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE api/{parts[1]}/{parts[2]}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE api/{parts[1]}/{parts[2]}")
                    removed += 1

    # Clean empty dirs
    if not args.dry_run:
        for entry in os.scandir(API_DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)

    return added, updated, unchanged, removed


# ---------------------------------------------------------------------------
# Part 2: sync_self_hosting -- GitHub MDX docs to markdown
# ---------------------------------------------------------------------------

def sync_self_hosting(cache: dict, new_cache: dict, args: argparse.Namespace) -> tuple[int, int, int, int]:
    """Sync self-hosting docs from GitHub. Returns (added, updated, unchanged, removed)."""
    print("Fetching GitHub file tree...")
    tree_json = fetch_url(GITHUB_TREE_URL)
    if not tree_json:
        print("ERROR: Could not fetch GitHub file tree", file=sys.stderr)
        return 0, 0, 0, 0

    tree_data = json.loads(tree_json)
    all_entries = tree_data.get("tree", [])

    # Filter to self-hosting content
    mdx_files = []
    meta_files = []
    for entry in all_entries:
        path = entry.get("path", "")
        if not path.startswith(SELF_HOSTING_PREFIX) or entry.get("type") != "blob":
            continue
        if path.endswith(".mdx"):
            mdx_files.append(path)
        elif path.endswith("meta.json"):
            meta_files.append(path)

    print(f"  Found {len(mdx_files)} MDX files, {len(meta_files)} meta.json files")

    if not mdx_files and not meta_files:
        print("  No self-hosting docs found.")
        return 0, 0, 0, 0

    # Fetch all files concurrently
    def _fetch_file(file_path: str) -> tuple[str, str | None]:
        url = f"{GITHUB_RAW_BASE}/{file_path}"
        return file_path, fetch_url(url)

    all_files_to_fetch = mdx_files + meta_files
    fetched: dict[str, str] = {}

    print(f"  Fetching {len(all_files_to_fetch)} files from GitHub...")
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_file, fp): fp for fp in all_files_to_fetch}
        for future in as_completed(futures):
            file_path, content = future.result()
            if content is not None:
                fetched[file_path] = content
            elif args.verbose:
                print(f"  FAIL: {file_path}")

    if not args.dry_run:
        os.makedirs(SELF_HOSTING_DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0

    # Track sections for building indexes
    # section -> [(rel_md_path, title)]
    sections: dict[str, list[tuple[str, str]]] = {}

    for file_path in sorted(mdx_files):
        if file_path not in fetched:
            continue

        # Compute relative path from content/self-hosting/
        rel_path = file_path[len(SELF_HOSTING_PREFIX) + 1:]
        # Convert .mdx -> .md
        md_rel_path = re.sub(r"\.mdx$", ".md", rel_path)

        # Convert MDX to markdown
        md_content = mdx_to_markdown(fetched[file_path])

        # Extract title from frontmatter
        title_match = re.search(r"^title:\s*[\"']?(.+?)[\"']?\s*$", md_content, re.MULTILINE)
        title = title_match.group(1) if title_match else os.path.basename(rel_path).replace(".mdx", "")

        content_hash = sha256(md_content)
        cache_key = f"selfhost:{md_rel_path}"

        # Track in sections
        parts = md_rel_path.split("/")
        if len(parts) == 1:
            section = ""
        else:
            section = parts[0]
        if section not in sections:
            sections[section] = []
        sections[section].append((md_rel_path, title))

        target_path = os.path.join(SELF_HOSTING_DOCS_DIR, md_rel_path)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(target_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(target_path)
            if args.dry_run:
                print(f"  {'ADD' if is_new else 'UPDATE'} self-hosting/{md_rel_path}")
            else:
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with open(target_path, "w") as f:
                    f.write(md_content)
                if args.verbose:
                    print(f"  {'ADD' if is_new else 'UPDATE'} self-hosting/{md_rel_path}")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

    # Build section README indexes
    for section, pages in sorted(sections.items()):
        if not section:
            continue
        section_title = section.replace("-", " ").title()
        readme_lines = [f"# {section_title}\n"]
        for md_path, title in sorted(pages, key=lambda x: x[1].lower()):
            # md_path is like "section/file.md", we need just the filename for a link within the section dir
            filename = os.path.basename(md_path)
            readme_lines.append(f"- [{title}](./{filename})")
        readme_lines.append("")
        readme_content = "\n".join(readme_lines)

        readme_path = os.path.join(SELF_HOSTING_DOCS_DIR, section, "README.md")
        cache_key = f"selfhost:{section}/README"
        content_hash = sha256(readme_content)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(readme_path)
            if not args.dry_run:
                os.makedirs(os.path.dirname(readme_path), exist_ok=True)
                with open(readme_path, "w") as f:
                    f.write(readme_content)
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

    # Self-hosting level README
    sh_lines = ["# Langfuse Self-Hosting Documentation\n"]
    sh_lines.append("Documentation fetched from the langfuse-docs GitHub repository.\n")
    sh_lines.append("## Sections\n")
    for section in sorted(sections.keys()):
        if not section:
            continue
        section_title = section.replace("-", " ").title()
        count = len(sections[section])
        sh_lines.append(f"- [{section_title}](./{section}/) ({count} pages)")
    # List top-level pages
    if "" in sections:
        sh_lines.append("")
        sh_lines.append("## Pages\n")
        for md_path, title in sorted(sections[""], key=lambda x: x[1].lower()):
            sh_lines.append(f"- [{title}](./{md_path})")
    sh_lines.append("")
    sh_readme = "\n".join(sh_lines)

    if not args.dry_run:
        with open(os.path.join(SELF_HOSTING_DOCS_DIR, "README.md"), "w") as f:
            f.write(sh_readme)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key.startswith("selfhost:") and old_key not in new_cache:
            rel = old_key[len("selfhost:"):]
            old_path = os.path.join(SELF_HOSTING_DOCS_DIR, rel)
            if os.path.exists(old_path):
                if args.dry_run:
                    print(f"  REMOVE self-hosting/{rel}")
                else:
                    os.remove(old_path)
                    if args.verbose:
                        print(f"  REMOVE self-hosting/{rel}")
                removed += 1

    # Clean empty dirs
    if not args.dry_run and os.path.isdir(SELF_HOSTING_DOCS_DIR):
        for entry in os.scandir(SELF_HOSTING_DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)

    return added, updated, unchanged, removed


# ---------------------------------------------------------------------------
# Main sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()
    new_cache: dict = {}

    # --- Part 1: API docs from OpenAPI YAML spec ---
    print("Fetching Langfuse OpenAPI spec...")
    raw_spec = fetch_url(OPENAPI_SPEC_URL)
    if not raw_spec:
        print("ERROR: Could not fetch OpenAPI spec", file=sys.stderr)
        sys.exit(1)

    spec = yaml.safe_load(raw_spec)
    print(f"  OpenAPI version: {spec.get('openapi', '?')}")
    print(f"  API version: {spec.get('info', {}).get('version', '?')}")
    print(f"  Paths: {len(spec.get('paths', {}))}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)
        with open(SPEC_FILE, "w") as f:
            f.write(raw_spec)

    print("\nSyncing API docs...")
    api_added, api_updated, api_unchanged, api_removed = sync_api(spec, cache, new_cache, args)
    print(f"  API: +{api_added} ~{api_updated} ={api_unchanged} -{api_removed}")

    # --- Part 2: Self-hosting docs from GitHub ---
    print("\nSyncing self-hosting docs...")
    sh_added, sh_updated, sh_unchanged, sh_removed = sync_self_hosting(cache, new_cache, args)
    print(f"  Self-hosting: +{sh_added} ~{sh_updated} ={sh_unchanged} -{sh_removed}")

    # Top-level README
    top_lines = ["# Langfuse Documentation\n"]
    top_lines.append("- [API Reference](./api/) -- REST API endpoints generated from OpenAPI spec")
    top_lines.append("- [Self-Hosting Documentation](./self-hosting/) -- Guides for self-hosting Langfuse")
    top_lines.append("")
    if not args.dry_run:
        with open(os.path.join(DOCS_DIR, "README.md"), "w") as f:
            f.write("\n".join(top_lines))

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    total_added = api_added + sh_added
    total_updated = api_updated + sh_updated
    total_unchanged = api_unchanged + sh_unchanged
    total_removed = api_removed + sh_removed

    print(f"\nSync complete:")
    print(f"  Added:     {total_added}")
    print(f"  Updated:   {total_updated}")
    print(f"  Unchanged: {total_unchanged}")
    print(f"  Removed:   {total_removed}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Langfuse API and self-hosting docs, convert to markdown"
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
    sync(args)


if __name__ == "__main__":
    main()
