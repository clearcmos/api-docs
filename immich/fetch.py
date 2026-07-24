#!/usr/bin/env python3

"""
Immich Documentation Fetcher

Fetches both the Immich OpenAPI spec (API reference) and general documentation
from docs.immich.app, converting everything to local markdown.
"""

import argparse
import gzip
import hashlib
import html.parser
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

OPENAPI_URL = "https://docs.immich.app/openapi.json"
SITEMAP_URL = "https://docs.immich.app/sitemap.xml"
DOCS_BASE_URL = "https://docs.immich.app"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
API_DOCS_DIR = os.path.join(DOCS_DIR, "api")
GENERAL_DOCS_DIR = os.path.join(DOCS_DIR, "general")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SPEC_FILE = os.path.join(SCRIPT_DIR, "openapi.json")
MAX_WORKERS = 16

# Sections to include from general docs (URL path prefixes)
GENERAL_SECTIONS = [
    "/overview/",
    "/install/",
    "/features/",
    "/administration/",
    "/guides/",
    "/developer/",
]

# Also include these exact paths
GENERAL_EXACT = [
    "/FAQ",
    "/errors",
]


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={
        "User-Agent": "immich-docs-fetcher/1.0",
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


# ---------------------------------------------------------------------------
# OpenAPI helpers
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


def schema_to_markdown(schema: dict, spec: dict, depth: int = 0, seen: set | None = None) -> str:
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
# HTML-to-Markdown converter for Docusaurus pages
# ---------------------------------------------------------------------------

class DocusaurusExtractor(html.parser.HTMLParser):
    """Extracts the main article content from a Docusaurus page and converts to markdown."""

    BLOCK_TAGS = {
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "ul", "ol", "li", "pre", "blockquote", "table", "thead",
        "tbody", "tr", "th", "td", "hr", "br", "figure", "figcaption",
        "details", "summary", "section", "article", "header", "footer",
        "dl", "dt", "dd",
    }
    INLINE_TAGS = {"a", "strong", "b", "em", "i", "code", "span", "img", "sup", "sub", "mark"}
    SKIP_TAGS = {"script", "style", "nav", "svg", "button", "iframe", "noscript"}

    def __init__(self):
        super().__init__()
        self._in_article = False
        self._article_depth = 0
        self._skip_depth = 0
        self._tag_stack: list[dict] = []
        self._output: list[str] = []
        self._current_line = ""
        self._in_code_block = False
        self._code_lang = ""
        self._code_content = ""
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell = ""
        self._in_header_row = False
        self._list_depth = 0
        self._list_type_stack: list[str] = []
        self._list_counters: list[int] = []
        self._title = ""
        self._in_title = False
        self._in_nav = False
        self._nav_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "")

        if tag == "title":
            self._in_title = True
            return

        # Skip nav, sidebar, footer, table-of-contents, theme toggles
        if tag == "nav" or (tag == "div" and ("navbar" in cls or "sidebar" in cls)):
            self._in_nav = True
            self._nav_depth = 1
            return

        if self._in_nav:
            self._nav_depth += 1
            return

        # Skip breadcrumbs, pagination, edit links, ToC
        skip_classes = ["breadcrumbs", "pagination-nav", "theme-edit-this-page",
                        "table-of-contents", "tocCollapsible", "footer"]
        if any(sc in cls for sc in skip_classes):
            self._skip_depth = 1
            return

        if self._skip_depth > 0:
            self._skip_depth += 1
            return

        if tag in self.SKIP_TAGS:
            self._skip_depth = 1
            return

        # Detect article/main content area
        if tag == "article" or (tag == "main" and not self._in_article):
            self._in_article = True
            self._article_depth = 1
            return

        if self._in_article:
            self._article_depth += 1

        if not self._in_article:
            return

        # Handle specific tags
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_line()
            level = int(tag[1])
            self._tag_stack.append({"tag": tag, "level": level})
            self._current_line = "#" * level + " "

        elif tag == "p":
            self._flush_line()

        elif tag == "pre":
            self._flush_line()
            self._in_code_block = True
            self._code_content = ""
            self._code_lang = ""
            # Docusaurus puts language-* class on <pre>, not <code>
            for c in cls.split():
                if c.startswith("language-"):
                    self._code_lang = c[9:]
                    break

        elif tag == "code":
            if self._in_code_block:
                # Extract language from class
                for c in cls.split():
                    if c.startswith("language-"):
                        self._code_lang = c[9:]
                        break
            else:
                self._current_line += "`"

        elif tag == "a":
            href = attr_dict.get("href", "")
            self._tag_stack.append({"tag": "a", "href": href})
            self._current_line += "["

        elif tag in ("strong", "b"):
            self._current_line += "**"

        elif tag in ("em", "i"):
            self._current_line += "*"

        elif tag == "img":
            alt = attr_dict.get("alt", "")
            src = attr_dict.get("src", "")
            self._current_line += f"![{alt}]({src})"

        elif tag == "br":
            if self._in_code_block:
                self._code_content += "\n"
            else:
                self._flush_line()

        elif tag == "hr":
            self._flush_line()
            self._output.append("---")
            self._output.append("")

        elif tag in ("ul", "ol"):
            if self._current_line.strip():
                self._flush_line()
            self._list_depth += 1
            self._list_type_stack.append(tag)
            self._list_counters.append(0)

        elif tag == "li":
            self._flush_line()
            if self._list_counters:
                self._list_counters[-1] += 1
            indent = "  " * (self._list_depth - 1)
            if self._list_type_stack and self._list_type_stack[-1] == "ol":
                self._current_line = f"{indent}{self._list_counters[-1]}. "
            else:
                self._current_line = f"{indent}- "

        elif tag == "blockquote":
            self._flush_line()
            self._tag_stack.append({"tag": "blockquote"})

        elif tag == "table":
            self._flush_line()
            self._in_table = True
            self._table_rows = []

        elif tag == "thead":
            self._in_header_row = True

        elif tag == "tr":
            self._current_row = []

        elif tag in ("td", "th"):
            self._current_cell = ""

        elif tag == "details":
            self._flush_line()

        elif tag == "summary":
            self._current_line = "**"

    def handle_endtag(self, tag: str):
        if tag == "title":
            self._in_title = False
            return

        if self._in_nav:
            self._nav_depth -= 1
            if self._nav_depth <= 0:
                self._in_nav = False
            return

        if self._skip_depth > 0:
            self._skip_depth -= 1
            return

        if tag in self.SKIP_TAGS:
            self._skip_depth = 0
            return

        if tag == "article" or (tag == "main" and self._in_article and self._article_depth == 1):
            self._flush_line()
            self._in_article = False
            self._article_depth = 0
            return

        if self._in_article:
            self._article_depth -= 1

        if not self._in_article:
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_line()
            self._output.append("")
            if self._tag_stack and self._tag_stack[-1].get("tag") == tag:
                self._tag_stack.pop()

        elif tag == "p":
            self._flush_line()
            self._output.append("")

        elif tag == "pre":
            self._flush_line()
            self._output.append(f"```{self._code_lang}")
            self._output.append(self._code_content.rstrip())
            self._output.append("```")
            self._output.append("")
            self._in_code_block = False

        elif tag == "code":
            if not self._in_code_block:
                self._current_line += "`"

        elif tag == "a":
            if self._tag_stack and self._tag_stack[-1].get("tag") == "a":
                href = self._tag_stack.pop().get("href", "")
                self._current_line += f"]({href})"

        elif tag in ("strong", "b"):
            self._current_line += "**"

        elif tag in ("em", "i"):
            self._current_line += "*"

        elif tag in ("ul", "ol"):
            self._list_depth -= 1
            if self._list_type_stack:
                self._list_type_stack.pop()
            if self._list_counters:
                self._list_counters.pop()
            if self._list_depth == 0:
                self._flush_line()
                self._output.append("")

        elif tag == "li":
            self._flush_line()

        elif tag == "blockquote":
            self._flush_line()
            if self._tag_stack and self._tag_stack[-1].get("tag") == "blockquote":
                self._tag_stack.pop()
            self._output.append("")

        elif tag == "table":
            self._flush_table()
            self._in_table = False

        elif tag == "thead":
            self._in_header_row = False

        elif tag == "tr":
            if self._current_row is not None:
                self._table_rows.append(self._current_row)
                if self._in_header_row:
                    # Add separator row after header
                    self._table_rows.append(["---"] * len(self._current_row))

        elif tag in ("td", "th"):
            self._current_row.append(self._current_cell.strip())

        elif tag == "summary":
            self._current_line += "**"
            self._flush_line()
            self._output.append("")

        elif tag == "details":
            self._flush_line()
            self._output.append("")

    def handle_data(self, data: str):
        if self._in_title and not self._title:
            self._title = data.strip()
            return

        if self._in_nav or self._skip_depth > 0 or not self._in_article:
            return

        if self._in_code_block:
            self._code_content += data
            return

        if self._in_table:
            self._current_cell += data
            return

        # Check if we're in a blockquote
        in_blockquote = any(t.get("tag") == "blockquote" for t in self._tag_stack)

        text = data
        if not self._in_code_block:
            # Collapse whitespace but preserve single spaces
            text = re.sub(r"\s+", " ", text)

        if in_blockquote:
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    self._current_line += "> " + line if not self._current_line else line

        else:
            self._current_line += text

    def handle_entityref(self, name: str):
        entities = {"amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'", "nbsp": " "}
        char = entities.get(name, f"&{name};")
        if self._in_code_block:
            self._code_content += char
        elif self._in_table:
            self._current_cell += char
        elif self._in_article and not self._skip_depth and not self._in_nav:
            self._current_line += char

    def handle_charref(self, name: str):
        try:
            if name.startswith("x"):
                char = chr(int(name[1:], 16))
            else:
                char = chr(int(name))
        except (ValueError, OverflowError):
            char = f"&#{name};"
        if self._in_code_block:
            self._code_content += char
        elif self._in_table:
            self._current_cell += char
        elif self._in_article and not self._skip_depth and not self._in_nav:
            self._current_line += char

    def _flush_line(self):
        line = self._current_line.rstrip()
        if line:
            self._output.append(line)
        self._current_line = ""

    def _flush_table(self):
        if not self._table_rows:
            return
        self._output.append("")
        for row in self._table_rows:
            self._output.append("| " + " | ".join(row) + " |")
        self._output.append("")

    def get_markdown(self) -> str:
        self._flush_line()
        # Clean up output: collapse multiple blank lines
        lines = self._output
        cleaned = []
        prev_blank = False
        for line in lines:
            is_blank = not line.strip()
            if is_blank and prev_blank:
                continue
            cleaned.append(line)
            prev_blank = is_blank

        # Strip leading/trailing blank lines
        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()

        return "\n".join(cleaned) + "\n"

    def get_title(self) -> str:
        return self._title


def html_to_markdown(html_content: str) -> tuple[str, str]:
    """Convert HTML to markdown. Returns (title, markdown_content)."""
    parser = DocusaurusExtractor()
    parser.feed(html_content)
    return parser.get_title(), parser.get_markdown()


# ---------------------------------------------------------------------------
# Sitemap parsing
# ---------------------------------------------------------------------------

def parse_sitemap(xml_content: str) -> list[str]:
    """Extract URLs from a sitemap XML."""
    urls = []
    root = ET.fromstring(xml_content)
    # Handle namespace
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    for url_elem in root.findall(f".//{ns}url"):
        loc = url_elem.find(f"{ns}loc")
        if loc is not None and loc.text:
            urls.append(loc.text.strip())
    return urls


def should_include_url(url: str) -> bool:
    """Check if a URL should be included in general docs fetch."""
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")

    # Skip the bare root and /api page (we generate API docs from OpenAPI)
    if path in ("", "/", "/api"):
        return False

    # Skip privacy policy
    if path == "/privacy-policy":
        return False

    for prefix in GENERAL_SECTIONS:
        if path.startswith(prefix.rstrip("/")):
            return True

    for exact in GENERAL_EXACT:
        if path == exact:
            return True

    return False


def url_to_filepath(url: str) -> tuple[str, str]:
    """Convert a docs URL to a local directory and filename.

    Returns (section_dir, filename) relative to GENERAL_DOCS_DIR.
    """
    parsed = urlparse(url)
    path = parsed.path.strip("/")
    parts = path.split("/")

    if len(parts) == 1:
        # Top-level page like /FAQ or /errors
        return "", sanitize_filename(parts[0]) + ".md"

    section = parts[0]
    slug = "-".join(parts[1:])
    return section, sanitize_filename(slug) + ".md"


# ---------------------------------------------------------------------------
# Sync logic
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

    # API README
    main_lines = ["# Immich API Reference\n"]
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


def sync_general_docs(cache: dict, new_cache: dict, args: argparse.Namespace) -> tuple[int, int, int, int]:
    """Sync general documentation pages. Returns (added, updated, unchanged, removed)."""
    print("Fetching sitemap...")
    sitemap_xml = fetch_url(SITEMAP_URL)
    if not sitemap_xml:
        print("ERROR: Could not fetch sitemap", file=sys.stderr)
        return 0, 0, 0, 0

    all_urls = parse_sitemap(sitemap_xml)
    urls = [u for u in all_urls if should_include_url(u)]
    print(f"  Found {len(urls)} general doc pages to fetch")

    if not args.dry_run:
        os.makedirs(GENERAL_DOCS_DIR, exist_ok=True)

    added = 0
    updated = 0
    unchanged = 0
    planned_cache_keys: set[str] = set()

    for url in urls:
        section, filename = url_to_filepath(url)
        key = f"docs:{section}:{filename}" if section else f"docs::{filename}"
        planned_cache_keys.add(key)
        if section:
            planned_cache_keys.add(f"docs:{section}:README")

    print(f"Fetching general documentation (concurrency={MAX_WORKERS})...")
    fetched_pages: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = {pool.submit(fetch_url, url): url for url in urls}
        for completed, future in enumerate(as_completed(futures), start=1):
            url = futures[future]
            content = future.result()
            if content is not None:
                fetched_pages[url] = content
            if args.verbose or completed % 20 == 0:
                print(f"  [{completed}/{len(urls)}] pages")

    # Track sections for building indexes
    sections: dict[str, list[tuple[str, str, str]]] = {}  # section -> [(filename, title, slug)]

    def preserve_cached_page(
        section: str, filename: str, url: str, cache_key: str, target_path: str
    ) -> None:
        """Keep a failed page and its index entry when a local copy exists."""
        if cache_key not in cache:
            return
        entry = dict(cache[cache_key])
        new_cache[cache_key] = entry
        if not os.path.exists(target_path):
            return
        title = entry.get("title", "")
        if not title:
            try:
                with open(target_path, "r") as f:
                    first_line = f.readline().strip()
                title = first_line.removeprefix("# ").strip()
            except OSError:
                title = ""
        title = title or filename.replace(".md", "").replace("-", " ").title()
        entry["title"] = title
        entry["url"] = url
        sections.setdefault(section, []).append(
            (filename, title, filename.replace(".md", ""))
        )

    for url in sorted(urls):
        section, filename = url_to_filepath(url)
        cache_key = f"docs:{section}:{filename}" if section else f"docs::{filename}"

        if section:
            target_dir = os.path.join(GENERAL_DOCS_DIR, section)
        else:
            target_dir = GENERAL_DOCS_DIR

        target_path = os.path.join(target_dir, filename)

        page_html = fetched_pages.get(url)
        if not page_html:
            preserve_cached_page(
                section, filename, url, cache_key, target_path)
            continue

        title, markdown = html_to_markdown(page_html)
        if not markdown.strip():
            if args.verbose:
                print(f"  SKIP {section}/{filename} (empty content)")
            preserve_cached_page(
                section, filename, url, cache_key, target_path)
            continue

        # Clean title -- remove " | Immich" suffix
        if title and " | " in title:
            title = title.rsplit(" | ", 1)[0]

        content_hash = sha256(markdown)

        if section not in sections:
            sections[section] = []
        sections[section].append((filename, title or filename.replace(".md", ""), filename.replace(".md", "")))

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(target_path):
            unchanged += 1
            new_cache[cache_key] = {
                **cache[cache_key],
                "title": title,
                "url": url,
            }
        else:
            is_new = cache_key not in cache or not os.path.exists(target_path)
            if args.dry_run:
                label = section + "/" if section else ""
                print(f"  {'ADD' if is_new else 'UPDATE'} general/{label}{filename}")
            else:
                os.makedirs(target_dir, exist_ok=True)
                with open(target_path, "w") as f:
                    f.write(markdown)
                if args.verbose:
                    label = section + "/" if section else ""
                    print(f"  {'ADD' if is_new else 'UPDATE'} general/{label}{filename}")
            new_cache[cache_key] = {
                "sha256": content_hash,
                "last_updated": datetime.now(timezone.utc).isoformat(),
                "title": title,
                "url": url,
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
        for filename, title, _ in sorted(pages, key=lambda x: x[1].lower()):
            readme_lines.append(f"- [{title}](./{filename})")
        readme_lines.append("")
        readme_content = "\n".join(readme_lines)

        readme_path = os.path.join(GENERAL_DOCS_DIR, section, "README.md")
        cache_key = f"docs:{section}:README"
        content_hash = sha256(readme_content)

        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
        else:
            is_new = cache_key not in cache or not os.path.exists(readme_path)
            if not args.dry_run:
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

    # Top-level general README
    general_lines = ["# Immich Documentation\n"]
    general_lines.append("## Sections\n")
    for section in sorted(sections.keys()):
        if not section:
            continue
        section_title = section.replace("-", " ").title()
        count = len(sections[section])
        general_lines.append(f"- [{section_title}](./{section}/) ({count} pages)")
    # List top-level pages
    if "" in sections:
        general_lines.append("")
        general_lines.append("## Other Pages\n")
        for filename, title, _ in sorted(sections[""], key=lambda x: x[1].lower()):
            general_lines.append(f"- [{title}](./{filename})")
    general_lines.append("")
    general_readme = "\n".join(general_lines)

    if not args.dry_run:
        with open(os.path.join(GENERAL_DOCS_DIR, "README.md"), "w") as f:
            f.write(general_readme)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key.startswith("docs:") and old_key not in new_cache:
            if old_key in planned_cache_keys:
                new_cache[old_key] = cache[old_key]
                continue
            parts = old_key.split(":", 2)
            if len(parts) == 3:
                section = parts[1]
                fname = parts[2]
                if section:
                    old_path = os.path.join(GENERAL_DOCS_DIR, section, fname)
                else:
                    old_path = os.path.join(GENERAL_DOCS_DIR, fname)
                if os.path.exists(old_path):
                    if args.dry_run:
                        label = section + "/" if section else ""
                        print(f"  REMOVE general/{label}{fname}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            label = section + "/" if section else ""
                            print(f"  REMOVE general/{label}{fname}")
                    removed += 1

    # Clean empty dirs
    if not args.dry_run:
        for entry in os.scandir(GENERAL_DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)

    return added, updated, unchanged, removed


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()
    new_cache: dict = {}

    # --- API docs from OpenAPI spec ---
    print("Fetching Immich OpenAPI spec...")
    raw = fetch_url(OPENAPI_URL)
    if not raw:
        print("ERROR: Could not fetch OpenAPI spec", file=sys.stderr)
        sys.exit(1)

    spec = json.loads(raw)
    print(f"  OpenAPI version: {spec.get('openapi', '?')}")
    print(f"  API version: {spec.get('info', {}).get('version', '?')}")
    print(f"  Paths: {len(spec.get('paths', {}))}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)
        with open(SPEC_FILE, "w") as f:
            f.write(raw)
            f.write("\n")

    print("\nSyncing API docs...")
    api_added, api_updated, api_unchanged, api_removed = sync_api(spec, cache, new_cache, args)

    print(f"  API: +{api_added} ~{api_updated} ={api_unchanged} -{api_removed}")

    # --- General docs from HTML ---
    print("\nSyncing general docs...")
    doc_added, doc_updated, doc_unchanged, doc_removed = sync_general_docs(cache, new_cache, args)

    print(f"  Docs: +{doc_added} ~{doc_updated} ={doc_unchanged} -{doc_removed}")

    # Top-level README
    top_lines = ["# Immich Documentation\n"]
    top_lines.append("- [API Reference](./api/) -- REST API endpoints generated from OpenAPI spec")
    top_lines.append("- [General Documentation](./general/) -- Guides, features, administration, and more")
    top_lines.append("")
    if not args.dry_run:
        with open(os.path.join(DOCS_DIR, "README.md"), "w") as f:
            f.write("\n".join(top_lines))

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    total_added = api_added + doc_added
    total_updated = api_updated + doc_updated
    total_unchanged = api_unchanged + doc_unchanged
    total_removed = api_removed + doc_removed

    print(f"\nSync complete:")
    print(f"  Added:     {total_added}")
    print(f"  Updated:   {total_updated}")
    print(f"  Unchanged: {total_unchanged}")
    print(f"  Removed:   {total_removed}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Immich API and general docs, convert to markdown"
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
