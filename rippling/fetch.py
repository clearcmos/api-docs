#!/usr/bin/env python3

"""
Rippling REST API Documentation Fetcher

Fetches the complete Rippling REST API documentation from developer.rippling.com
and saves it as organized markdown files.

The site is a Docusaurus app that bundles content in webpack chunks. This script:
1. Downloads the main JS bundle to extract route -> content hash mappings
2. Downloads the runtime JS bundle to get chunk ID -> filename mappings
3. Fetches each content chunk and extracts documentation:
   - Reference pages (.api.mdx): base64+zlib compressed OpenAPI operation JSON
   - Guide/essential pages (.md): compiled MDX/JSX converted to markdown
4. Saves everything as organized markdown in docs/
"""

import argparse
import base64
import gzip
import hashlib
import json
import os
import re
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE_URL = "https://developer.rippling.com"
MAX_WORKERS = 16

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")


# ---------------------------------------------------------------------------
# Standard helpers
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, description: str = "", timeout: int = 30) -> str | None:
    """Fetch a URL with error handling."""
    req = Request(url, headers={
        "User-Agent": "rippling-api-docs-fetcher/1.0",
        "Accept-Encoding": "gzip",
    })
    try:
        if description:
            print(f"  Fetching: {description}")
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8")
    except (HTTPError, URLError, TimeoutError, OSError) as e:
        print(f"  Error fetching {url}: {e}", file=sys.stderr)
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
# OpenAPI helpers (for .api.mdx spec fragments)
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

    if not isinstance(schema, dict):
        return str(schema) if schema else "any"

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return "`(circular reference)`"
        seen = seen | {ref}
        schema = resolve_ref(ref, spec)
        if not schema:
            return f"`{ref.split('/')[-1]}`"

    schema_type = schema.get("type", "")
    # Handle list-type type fields (e.g. ["string", "null"])
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
            if not isinstance(sub, dict):
                continue
            if "$ref" in sub:
                sub = resolve_ref(sub["$ref"], spec)
            if not isinstance(sub, dict):
                continue
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

    rb_required = request_body.get("required", False)
    if rb_required:
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
            lines.append(f"```json\n{json.dumps(example, indent=2)}\n```\n")

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


# ---------------------------------------------------------------------------
# Webpack / Docusaurus bundle extraction
# ---------------------------------------------------------------------------

def extract_js_bundle_urls(html_content: str) -> tuple[str | None, str | None]:
    """Extract the main.js and runtime.js URLs from the HTML page."""
    main_match = re.search(r'src="(/assets/js/main\.[^"]+\.js)"', html_content)
    runtime_match = re.search(r'src="(/assets/js/runtime~main\.[^"]+\.js)"', html_content)

    main_url = BASE_URL + main_match.group(1) if main_match else None
    runtime_url = BASE_URL + runtime_match.group(1) if runtime_match else None

    return main_url, runtime_url


def parse_route_mappings(main_js: str) -> list[tuple[str, str]]:
    """Extract route -> chunk ID mappings from main.js for REST API pages."""
    routes = re.findall(
        r'path:"(/documentation/rest-api/[^"]+)",component:p\("[^"]+","([^"]+)"\)',
        main_js,
    )
    return routes


def parse_content_hashes(main_js: str, routes: list[tuple[str, str]]) -> dict[str, str]:
    """Map each route to its content hash from the registry in main.js."""
    content_hashes = {}
    for route, chunk_id in routes:
        registry_key = f"{route}-{chunk_id}"
        pattern = re.escape(f'"{registry_key}"') + r':\{[^}]*"content":"([^"]+)"'
        m = re.search(pattern, main_js)
        if m:
            content_hashes[route] = m.group(1)
    return content_hashes


def parse_webpack_loaders(main_js: str, content_hashes: dict[str, str]) -> dict[str, dict]:
    """Map each content hash to its webpack chunk IDs and source file info."""
    webpack_info = {}
    for route, ch in content_hashes.items():
        # Try quoted key first, then unquoted JS identifier
        for search in [f'"{ch}":[', f'{ch}:[']:
            idx = main_js.find(search)
            if idx >= 0:
                if search == f'{ch}:[' and idx > 0 and main_js[idx - 1] not in ',{':
                    continue
                break
        else:
            continue

        bracket_start = main_js.find('[', idx)
        bracket_count = 0
        end = bracket_start
        while end < len(main_js):
            if main_js[end] == '[':
                bracket_count += 1
            elif main_js[end] == ']':
                bracket_count -= 1
                if bracket_count == 0:
                    break
            end += 1

        loader = main_js[idx:end + 1]
        all_chunks = re.findall(r'n\.e\((\d+)\)', loader)
        module_id = re.search(r'n\.bind\(n,(\d+)\)', loader)
        source = re.search(r'"(@site/[^"]+)"', loader)

        webpack_info[route] = {
            'content_hash': ch,
            'all_chunks': all_chunks,
            'module_id': module_id.group(1) if module_id else None,
            'source': source.group(1) if source else None,
        }
    return webpack_info


def parse_chunk_filename_map(runtime_js: str) -> dict[str, str]:
    """Extract webpack chunk ID -> filename mapping from runtime.js."""
    idx = runtime_js.find('t.u=e=>"assets/js/"')
    if idx < 0:
        return {}

    # Find all mapping objects within the t.u function
    end_candidates = [m.start() for m in re.finditer(r',t\.\w+=', runtime_js[idx + 10:])]
    func_end = idx + 10 + end_candidates[0] if end_candidates else len(runtime_js)
    func_body = runtime_js[idx:func_end]

    maps = re.findall(r'\{([^}]+)\}\[e\]', func_body)
    if len(maps) < 2:
        return {}

    name_map = {}
    for item in re.findall(r'(\d+)\s*:\s*"([^"]+)"', maps[0]):
        name_map[item[0]] = item[1]

    hash_map = {}
    for item in re.findall(r'(\d+)\s*:\s*"([^"]+)"', maps[1]):
        hash_map[item[0]] = item[1]

    chunk_urls = {}
    for chunk_id in set(list(name_map.keys()) + list(hash_map.keys())):
        name = name_map.get(chunk_id, chunk_id)
        ch = hash_map.get(chunk_id, chunk_id)
        chunk_urls[chunk_id] = f"assets/js/{name}.{ch}.js"

    return chunk_urls


def determine_content_chunk(webpack_info_entry: dict, chunk_url_map: dict) -> str | None:
    """Find the content chunk URL for a given page.

    For pages with multiple chunks (Promise.all), the content is in the
    last unique chunk (others are shared component/framework chunks).
    """
    all_chunks = webpack_info_entry.get('all_chunks', [])
    if not all_chunks:
        return None

    # The last chunk in the list is the content-specific one
    chunk_id = all_chunks[-1] if len(all_chunks) > 1 else all_chunks[0]
    return chunk_url_map.get(chunk_id)


# ---------------------------------------------------------------------------
# Content extraction from webpack chunks
# ---------------------------------------------------------------------------

def extract_api_spec_from_chunk(chunk_js: str) -> dict | None:
    """Extract the base64+zlib compressed OpenAPI operation from an API reference chunk."""
    api_match = re.search(r'api:"([^"]+)"', chunk_js)
    if not api_match:
        return None

    try:
        decoded = base64.b64decode(api_match.group(1))
        decompressed = zlib.decompress(decoded)
        return json.loads(decompressed)
    except Exception:
        return None


def extract_frontmatter(chunk_js: str) -> dict:
    """Extract frontmatter fields from a chunk's compiled module."""
    frontmatter = {}

    title_match = re.search(r'title:"([^"]*)"', chunk_js)
    if title_match:
        frontmatter['title'] = title_match.group(1)

    desc_match = re.search(r'description:"([^"]*)"', chunk_js)
    if desc_match:
        frontmatter['description'] = desc_match.group(1)

    label_match = re.search(r'sidebar_label:"([^"]*)"', chunk_js)
    if label_match:
        frontmatter['sidebar_label'] = label_match.group(1)

    return frontmatter


def jsx_to_markdown(chunk_js: str) -> str | None:
    """Convert compiled JSX/MDX content to markdown.

    Parses the compiled React JSX tree structure to extract text content
    and convert it back to markdown format.
    """
    func_match = re.search(r'function c\(e\)\{', chunk_js)
    if not func_match:
        return None

    func_start = func_match.start()
    func_end_match = re.search(r'function [a-z]\(', chunk_js[func_start + 20:])
    if func_end_match:
        func_body = chunk_js[func_start:func_start + 20 + func_end_match.start()]
    else:
        func_body = chunk_js[func_start:]

    segments = []

    # Find all children:"text" with their preceding tag context
    for m in re.finditer(r'children:"((?:[^"\\]|\\.)*)"', func_body):
        text = m.group(1)
        # Unescape
        text = text.replace('\\n', '\n').replace('\\t', '\t')
        text = text.replace("\\'", "'").replace('\\"', '"')
        text = text.encode().decode('unicode_escape', errors='replace')

        # Find the tag by looking backwards for s.TAG
        preceding = func_body[max(0, m.start() - 150):m.start()]

        tag_match = re.search(r's\.(h[1-6]|p|li|td|th|code|strong|em|pre|a),\{', preceding)
        container_match = re.search(r's\.(table|thead|tbody|tr|ul|ol),', preceding)

        if tag_match:
            tag = tag_match.group(1)
        elif container_match:
            tag = container_match.group(1)
        else:
            tag = 'text'

        segments.append((tag, text))

    # Convert segments to markdown
    md_lines = []
    table_data = {'headers': [], 'rows': [], 'current_row': []}
    i = 0

    while i < len(segments):
        tag, text = segments[i]

        if tag.startswith('h') and len(tag) == 2:
            level = int(tag[1])
            md_lines.append(f"\n{'#' * level} {text}\n")
        elif tag == 'p':
            md_lines.append(f"\n{text}\n")
        elif tag == 'li':
            md_lines.append(f"- {text}")
        elif tag == 'th':
            table_data['headers'].append(text)
        elif tag == 'td':
            table_data['current_row'].append(text)
            if table_data['headers'] and len(table_data['current_row']) >= len(table_data['headers']):
                table_data['rows'].append(table_data['current_row'])
                table_data['current_row'] = []
        elif tag == 'code':
            md_lines.append(f"`{text}`")
        elif tag == 'strong':
            md_lines.append(f"**{text}**")
        elif tag == 'em':
            md_lines.append(f"*{text}*")
        elif tag == 'pre':
            md_lines.append(f"\n```\n{text}\n```\n")
        elif tag == 'text':
            if text.strip():
                md_lines.append(text)

        # Flush table when we hit a non-table tag after collecting table data
        if tag not in ('th', 'td', 'tr', 'thead', 'tbody', 'table') and table_data['headers']:
            md_lines.append("")
            md_lines.append("| " + " | ".join(table_data['headers']) + " |")
            md_lines.append("| " + " | ".join(["---"] * len(table_data['headers'])) + " |")
            for row in table_data['rows']:
                md_lines.append("| " + " | ".join(row) + " |")
            md_lines.append("")
            table_data = {'headers': [], 'rows': [], 'current_row': []}

        i += 1

    # Flush any remaining table
    if table_data['headers']:
        md_lines.append("")
        md_lines.append("| " + " | ".join(table_data['headers']) + " |")
        md_lines.append("| " + " | ".join(["---"] * len(table_data['headers'])) + " |")
        for row in table_data['rows']:
            md_lines.append("| " + " | ".join(row) + " |")
        md_lines.append("")

    return '\n'.join(md_lines)


# ---------------------------------------------------------------------------
# Markdown builders
# ---------------------------------------------------------------------------

def build_endpoint_markdown(api_spec: dict, frontmatter: dict) -> str:
    """Convert an OpenAPI operation spec fragment to markdown."""
    lines = []

    title = frontmatter.get('title', api_spec.get('summary', api_spec.get('operationId', 'API Endpoint')))
    lines.append(f"# {title}\n")

    description = api_spec.get('description', frontmatter.get('description', ''))
    if description:
        lines.append(f"{description}\n")

    tags = api_spec.get('tags', [])
    if tags:
        lines.append(f"**Tags:** {', '.join(f'`{t}`' for t in tags)}\n")

    op_id = api_spec.get('operationId', '')
    if op_id:
        lines.append(f"**Operation ID:** `{op_id}`\n")

    method = api_spec.get('method', '').upper()
    path = api_spec.get('path', '')
    if method and path:
        lines.append("## Request\n")
        lines.append(f"**Method:** `{method}`\n")
        lines.append(f"**Endpoint:** `{path}`\n")

    servers = api_spec.get('servers', [])
    if servers:
        lines.append(f"**Base URL:** `{servers[0].get('url', '')}`\n")

    # The spec fragments are self-contained -- use the spec itself as the
    # root for $ref resolution (they typically don't have external refs).
    parameters = api_spec.get('parameters', [])
    if parameters:
        lines.append(format_parameters(parameters, api_spec))

    request_body = api_spec.get('requestBody')
    if request_body:
        lines.append(format_request_body(request_body, api_spec))

    responses = api_spec.get('responses', {})
    if responses:
        lines.append(format_responses(responses, api_spec))

    security = api_spec.get('security', [])
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


def build_guide_markdown(chunk_js: str, frontmatter: dict) -> str:
    """Build markdown for a guide/essential page from compiled JSX."""
    markdown = jsx_to_markdown(chunk_js)
    slug = frontmatter.get('title', '')

    if not markdown or len(markdown.strip()) < 50:
        title = frontmatter.get('title', 'Untitled')
        desc = frontmatter.get('description', '')
        markdown = f"# {title}\n\n{desc}\n"

    # Prepend title if not already in content
    title = frontmatter.get('title', '')
    if title and not markdown.strip().startswith(f"# {title}"):
        markdown = f"# {title}\n{markdown}"

    return markdown


def categorize_route(route: str) -> str:
    """Categorize a route into a directory based on the first path segment after rest-api/."""
    path = route.replace('/documentation/rest-api/', '')
    parts = path.split('/')
    if len(parts) >= 2:
        return parts[0]
    return 'overview'


def build_category_readme(category: str, entries: list[tuple[str, str, str]]) -> str:
    """Build a category-level README.md."""
    lines = [f"# Rippling REST API - {category.replace('-', ' ').title()}\n"]
    lines.append("## Pages\n")
    for title, fname, desc in sorted(entries):
        desc_text = f" -- {desc}" if desc else ""
        lines.append(f"- [{title}](./{fname}){desc_text}")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(categories: dict[str, list]) -> str:
    """Build the top-level docs/README.md."""
    lines = [
        "# Rippling REST API Documentation\n",
        "Complete API reference and guides for the Rippling REST API,",
        "fetched from developer.rippling.com.\n",
        "## Sections\n",
    ]
    for cat in sorted(categories.keys()):
        count = len(categories[cat])
        display = cat.replace('-', ' ').title()
        lines.append(f"- [{display}](./{cat}/) ({count} pages)")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def write_file(path: str, content: str, cache_key: str, cache: dict,
               new_cache: dict, counters: dict, args: argparse.Namespace,
               display_path: str) -> None:
    """Write a file with cache checking. Mutates new_cache and counters."""
    content_hash = sha256(content)

    if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(path):
        counters['unchanged'] += 1
        new_cache[cache_key] = cache[cache_key]
        return

    is_new = cache_key not in cache or not os.path.exists(path)
    label = "ADD" if is_new else "UPDATE"

    if args.dry_run:
        print(f"  {label} {display_path}")
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if args.verbose:
            print(f"  {label} {display_path}")

    new_cache[cache_key] = {
        "sha256": content_hash,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    if is_new:
        counters['added'] += 1
    else:
        counters['updated'] += 1


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()
    new_cache = {}
    counters = {'added': 0, 'updated': 0, 'unchanged': 0, 'removed': 0}

    # ------------------------------------------------------------------
    # Step 1: Fetch the landing page to get JS bundle URLs
    # ------------------------------------------------------------------
    print("Fetching landing page for bundle URLs...")
    html = fetch_url(f"{BASE_URL}/documentation/rest-api", "landing page")
    if not html:
        print("Failed to fetch landing page.", file=sys.stderr)
        sys.exit(1)

    main_url, runtime_url = extract_js_bundle_urls(html)
    if not main_url or not runtime_url:
        print("Failed to find JS bundle URLs in page HTML.", file=sys.stderr)
        sys.exit(1)

    print(f"  Main bundle: {main_url}")
    print(f"  Runtime bundle: {runtime_url}")

    # ------------------------------------------------------------------
    # Step 2: Download JS bundles
    # ------------------------------------------------------------------
    print("\nDownloading JS bundles...")
    main_js = fetch_url(main_url, "main.js")
    runtime_js = fetch_url(runtime_url, "runtime.js")
    if not main_js or not runtime_js:
        print("Failed to download JS bundles.", file=sys.stderr)
        sys.exit(1)

    print(f"  Main bundle: {len(main_js):,} bytes")
    print(f"  Runtime bundle: {len(runtime_js):,} bytes")

    # ------------------------------------------------------------------
    # Step 3: Parse route mappings
    # ------------------------------------------------------------------
    print("\nParsing route mappings...")
    routes = parse_route_mappings(main_js)
    print(f"  Found {len(routes)} REST API routes")

    content_hashes = parse_content_hashes(main_js, routes)
    print(f"  Mapped {len(content_hashes)} content hashes")

    webpack_info = parse_webpack_loaders(main_js, content_hashes)
    print(f"  Resolved {len(webpack_info)} webpack loaders")

    chunk_url_map = parse_chunk_filename_map(runtime_js)
    print(f"  Chunk filename map: {len(chunk_url_map)} entries")

    # ------------------------------------------------------------------
    # Step 4: Group routes by category
    # ------------------------------------------------------------------
    categories: dict[str, list[str]] = {}
    for route in webpack_info:
        cat = categorize_route(route)
        categories.setdefault(cat, []).append(route)

    print("\nCategories:")
    for cat in sorted(categories.keys()):
        print(f"  {cat}: {len(categories[cat])} pages")

    # ------------------------------------------------------------------
    # Step 5: Fetch and process each page
    # ------------------------------------------------------------------
    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    fail_count = 0
    route_chunk_urls: dict[str, str] = {}

    def preserve_cached_route(
        category: str, filename: str, slug: str, cache_key: str,
        entries: list[tuple[str, str, str]],
    ) -> None:
        """Keep a failed route and its catalogue entry when a local copy exists."""
        filepath = os.path.join(DOCS_DIR, category, filename)
        if cache_key not in cache or not os.path.exists(filepath):
            return
        entry = dict(cache[cache_key])
        title = entry.get("title", "")
        if not title:
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    title = f.readline().strip().removeprefix("# ").strip()
            except OSError:
                title = ""
        title = title or slug
        description = entry.get("description", "")
        entry["title"] = title
        entry["description"] = description
        new_cache[cache_key] = entry
        entries.append((title, filename, description))

    for route, info in webpack_info.items():
        chunk_path = determine_content_chunk(info, chunk_url_map)
        if chunk_path:
            route_chunk_urls[route] = f"{BASE_URL}/{chunk_path}"

    unique_chunk_urls = sorted(set(route_chunk_urls.values()))
    print(f"\nFetching {len(unique_chunk_urls)} content chunks "
          f"(concurrency={MAX_WORKERS})...")

    chunk_contents: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_url, url): url for url in unique_chunk_urls}
        for completed, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            content = future.result()
            if content is not None:
                chunk_contents[url] = content
            if args.verbose or completed % 50 == 0:
                print(f"  [{completed}/{len(unique_chunk_urls)}] chunks")

    for category in sorted(categories.keys()):
        cat_routes = sorted(categories[category])
        cat_dir = os.path.join(DOCS_DIR, category)

        print(f"\nProcessing: {category} ({len(cat_routes)} pages)")

        cat_readme_entries: list[tuple[str, str, str]] = []

        for route in cat_routes:
            info = webpack_info[route]
            slug = route.split('/')[-1]
            filename = sanitize_filename(slug) + '.md'
            cache_key = f"cat:{category}:{filename}"

            # Get the content chunk URL
            chunk_url = route_chunk_urls.get(route)
            if not chunk_url:
                print(f"  SKIP (no chunk): {slug}")
                fail_count += 1
                preserve_cached_route(
                    category, filename, slug, cache_key, cat_readme_entries)
                continue

            chunk_js = chunk_contents.get(chunk_url)
            if not chunk_js:
                print(f"  FAIL: {slug}")
                fail_count += 1
                preserve_cached_route(
                    category, filename, slug, cache_key, cat_readme_entries)
                continue

            # Check if this is a JS chunk or an HTML fallback (404-like)
            if chunk_js.startswith('<!doctype') or chunk_js.startswith('<html'):
                print(f"  SKIP (HTML response): {slug}")
                fail_count += 1
                preserve_cached_route(
                    category, filename, slug, cache_key, cat_readme_entries)
                continue

            # Extract frontmatter
            frontmatter = extract_frontmatter(chunk_js)
            source = info.get('source', '')
            is_api_page = source.endswith('.api.mdx')

            if is_api_page:
                api_spec = extract_api_spec_from_chunk(chunk_js)
                if api_spec:
                    markdown = build_endpoint_markdown(api_spec, frontmatter)
                else:
                    print(f"  FAIL (no API spec): {slug}")
                    fail_count += 1
                    preserve_cached_route(
                        category, filename, slug, cache_key, cat_readme_entries)
                    continue
            else:
                markdown = build_guide_markdown(chunk_js, frontmatter)

            filepath = os.path.join(cat_dir, filename)
            display_title = frontmatter.get('title', slug)
            cat_readme_entries.append((display_title, filename, frontmatter.get('description', '')))

            write_file(filepath, markdown, cache_key, cache, new_cache,
                       counters, args, f"{category}/{filename}")
            if cache_key in new_cache:
                new_cache[cache_key] = {
                    **new_cache[cache_key],
                    "title": display_title,
                    "description": frontmatter.get('description', ''),
                }

        # Write category README
        readme_content = build_category_readme(category, cat_readme_entries)
        readme_path = os.path.join(cat_dir, "README.md")
        readme_key = f"cat:{category}:README"
        write_file(readme_path, readme_content, readme_key, cache, new_cache,
                   counters, args, f"{category}/README.md")

    # ------------------------------------------------------------------
    # Step 6: Top-level README
    # ------------------------------------------------------------------
    top_content = build_top_readme(categories)
    top_path = os.path.join(DOCS_DIR, "README.md")
    if not args.dry_run:
        with open(top_path, "w", encoding="utf-8") as f:
            f.write(top_content)

    # ------------------------------------------------------------------
    # Step 7: Detect removals
    # ------------------------------------------------------------------
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
                    counters['removed'] += 1

    # Clean up empty category directories
    if not args.dry_run and os.path.exists(DOCS_DIR):
        for entry in os.scandir(DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)
                if args.verbose:
                    print(f"  RMDIR {entry.name}/")

    # ------------------------------------------------------------------
    # Step 8: Save cache
    # ------------------------------------------------------------------
    if not args.dry_run:
        save_cache(new_cache)

    # ------------------------------------------------------------------
    # Summary
    # ------------------------------------------------------------------
    print(f"\nSync complete:")
    print(f"  Added:      {counters['added']}")
    print(f"  Updated:    {counters['updated']}")
    print(f"  Unchanged:  {counters['unchanged']}")
    print(f"  Removed:    {counters['removed']}")
    print(f"  Failed:     {fail_count}")
    print(f"  Categories: {len(categories)}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Rippling REST API docs from developer.rippling.com and convert to markdown"
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
