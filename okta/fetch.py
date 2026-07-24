#!/usr/bin/env python3

"""
Okta Documentation Fetcher

Fetches both Okta API documentation (from three OpenAPI YAML specs on GitHub)
and Okta help documentation (from help.okta.com) and converts everything to
local markdown.

Part 1 -- API docs:
  Three OpenAPI specs from okta/okta-management-openapi-spec on GitHub:
  - Management API (user, group, app, policy management)
  - OAuth API (OAuth 2.0 and OIDC endpoints)
  - Identity Provider API (IdP federation endpoints)

Part 2 -- Help docs:
  Discovers the current Okta Identity Engine help corpus from its sitemap.
  Pages use DITA-generated HTML; each page is fetched concurrently and
  converted to markdown via stdlib html.parser. ETags avoid retransferring
  unchanged pages after the first successful sync.
"""

import argparse
import gzip
import hashlib
import html.parser
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

try:
    import yaml
except ImportError:
    print(
        "ERROR: pyyaml is required (YAML-only specs). Install with: pip install pyyaml",
        file=sys.stderr,
    )
    sys.exit(1)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_BASE = (
    "https://raw.githubusercontent.com/okta/okta-management-openapi-spec"
    "/master/dist/current"
)

API_SPECS = {
    "management": {
        "url": f"{GITHUB_BASE}/management-minimal.yaml",
        "name": "Management API",
        "spec_file": "openapi-management.yaml",
        "cache_prefix": "api-mgmt",
    },
    "oauth": {
        "url": f"{GITHUB_BASE}/oauth-minimal.yaml",
        "name": "OAuth API",
        "spec_file": "openapi-oauth.yaml",
        "cache_prefix": "api-oauth",
    },
    "idp": {
        "url": f"{GITHUB_BASE}/idp-minimal.yaml",
        "name": "Identity Provider API",
        "spec_file": "openapi-idp.yaml",
        "cache_prefix": "api-idp",
    },
}

HELP_BASE_URL = "https://help.okta.com/oie/en-us"
HELP_SITEMAP_URL = f"{HELP_BASE_URL}/sitemap.xml"

MAX_WORKERS = 30
REQUEST_DELAY = 0
MAX_RETRIES = 3
RETRY_DELAY = 5

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
API_DOCS_DIR = os.path.join(DOCS_DIR, "api")
HELP_DOCS_DIR = os.path.join(DOCS_DIR, "help")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")


# ---------------------------------------------------------------------------
# Common helpers
# ---------------------------------------------------------------------------


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={
        "User-Agent": "okta-docs-fetcher/1.0",
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


def fetch_url_with_retry(
    url: str, timeout: int = 30, etag: str | None = None
) -> tuple[str | None, str | None, bool]:
    """Fetch a URL with retries, gzip, and optional ETag validation."""
    for attempt in range(MAX_RETRIES):
        headers = {
            "User-Agent": "okta-docs-fetcher/1.0",
            "Accept-Encoding": "gzip",
        }
        if etag:
            headers["If-None-Match"] = etag
        req = Request(url, headers=headers)
        try:
            with urlopen(req, timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
                return (
                    data.decode("utf-8"),
                    resp.headers.get("ETag"),
                    False,
                )
        except HTTPError as e:
            if e.code == 304:
                return None, e.headers.get("ETag") or etag, True
            if e.code in (429, 503) and attempt < MAX_RETRIES - 1:
                wait = RETRY_DELAY * (2 ** attempt)
                print(
                    f"    {e.code} for {url}, backing off {wait}s "
                    f"(attempt {attempt + 1}/{MAX_RETRIES})",
                    file=sys.stderr,
                )
                time.sleep(wait)
                continue
            if e.code == 404:
                return None, None, False
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
                continue
            print(f"ERROR: Failed after {MAX_RETRIES} attempts: {url}: {e}", file=sys.stderr)
            return None, None, False
        except (URLError, TimeoutError, OSError) as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
                continue
            print(f"ERROR: Failed after {MAX_RETRIES} attempts: {url}: {e}", file=sys.stderr)
            return None, None, False
    return None, None, False


def fetch_etag_with_retry(
    url: str, timeout: int = 30
) -> tuple[str | None, bool]:
    """Fetch only response headers. Returns (ETag, request_succeeded)."""
    for attempt in range(MAX_RETRIES):
        req = Request(
            url,
            method="HEAD",
            headers={
                "User-Agent": "okta-docs-fetcher/1.0",
                "Accept-Encoding": "gzip",
            },
        )
        try:
            with urlopen(req, timeout=timeout) as resp:
                return resp.headers.get("ETag"), True
        except HTTPError as e:
            if e.code == 404:
                return None, True
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
                continue
            print(
                f"ERROR: HEAD failed after {MAX_RETRIES} attempts: {url}: {e}",
                file=sys.stderr,
            )
            return None, False
        except (URLError, TimeoutError, OSError) as e:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY * (2 ** attempt))
                continue
            print(
                f"ERROR: HEAD failed after {MAX_RETRIES} attempts: {url}: {e}",
                file=sys.stderr,
            )
            return None, False
    return None, False


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


def write_file(path: str, content: str, args: argparse.Namespace, label: str,
               is_new: bool) -> None:
    """Write content to a file, respecting dry-run and verbose flags."""
    if args.dry_run:
        print(f"  {'ADD' if is_new else 'UPDATE'} {label}")
    else:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
        if args.verbose:
            print(f"  {'ADD' if is_new else 'UPDATE'} {label}")


def cache_check(cache: dict, new_cache: dict, cache_key: str,
                content_hash: str, file_path: str) -> str:
    """Check cache status. Returns 'unchanged', 'new', or 'updated'."""
    if (
        cache.get(cache_key, {}).get("sha256") == content_hash
        and os.path.exists(file_path)
    ):
        new_cache[cache_key] = cache[cache_key]
        return "unchanged"
    new_cache[cache_key] = {
        "sha256": content_hash,
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }
    if cache_key not in cache or not os.path.exists(file_path):
        return "new"
    return "updated"


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


def schema_to_markdown(
    schema: dict, spec: dict, depth: int = 0, seen: set | None = None
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
    # OpenAPI 3.1 allows type as a list, e.g. ["string", "null"]
    if isinstance(schema_type, list):
        schema_type = " | ".join(str(t) for t in schema_type)

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
        parts = []
        for v in variants[:5]:
            parts.append(schema_to_markdown(v, spec, depth, seen))
        label = "One of" if one_of else "Any of"
        if len(variants) > 5:
            parts.append(f"... and {len(variants) - 5} more")
        return f"{label}: " + " | ".join(parts)

    if schema_type == "array" or "array" in str(schema_type):
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
        result = str(schema_type)
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


def build_endpoint_markdown(
    path: str, method: str, operation: dict, spec: dict
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
# MadCap Flare HTML-to-Markdown converter (stdlib html.parser)
# ---------------------------------------------------------------------------


class MadCapExtractor(html.parser.HTMLParser):
    """Extracts main content from a MadCap Flare page and converts to markdown."""

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
        self._in_content = False
        self._content_depth = 0
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
        self._in_dl = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        attr_dict = dict(attrs)
        cls = attr_dict.get("class", "") or ""
        tag_id = attr_dict.get("id", "") or ""
        style = attr_dict.get("style", "") or ""

        if tag == "title":
            self._in_title = True
            return

        # Detect the main content area -- MadCap Flare uses various IDs/classes
        if not self._in_content:
            if (
                tag_id == "mc-main-content"
                or (tag == "div" and "mc-body" in cls)
                or (tag == "div" and "body-container" in cls)
                or (tag == "div" and "okta-topics" in cls)
                or (tag == "div" and "topic-content" in cls)
                or attr_dict.get("data-mc-content-body") == "True"
            ):
                self._in_content = True
                self._content_depth = 1
                return

        if not self._in_content:
            return

        self._content_depth += 1

        # Skip hidden elements
        if "display:none" in style.lower() or "display: none" in style.lower():
            self._skip_depth = 1
            return

        if self._skip_depth > 0:
            self._skip_depth += 1
            return

        # Skip noise elements
        if tag_id and "coveo" in tag_id.lower():
            self._skip_depth = 1
            return
        if "coveo" in cls.lower():
            self._skip_depth = 1
            return

        skip_classes = [
            "replace_top_nav", "is-not-in-mobile", "oie-label",
            "footer2", "footer", "breadcrumbs", "pagination-nav",
            "navbar", "sidebar", "feedback",
        ]
        if any(sc in cls.lower() for sc in skip_classes):
            self._skip_depth = 1
            return
        if tag_id in ("feedback-tab",):
            self._skip_depth = 1
            return

        if tag in self.SKIP_TAGS:
            self._skip_depth = 1
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
            for c in cls.split():
                if c.startswith("language-"):
                    self._code_lang = c[9:]
                    break

        elif tag == "code":
            if self._in_code_block:
                for c in cls.split():
                    if c.startswith("language-"):
                        self._code_lang = c[9:]
                        break
            else:
                self._current_line += "`"

        elif tag == "a":
            href = attr_dict.get("href", "")
            self._tag_stack.append({"tag": "a", "href": href or ""})
            self._current_line += "["

        elif tag in ("strong", "b"):
            self._current_line += "**"

        elif tag in ("em", "i"):
            self._current_line += "*"

        elif tag == "img":
            alt = attr_dict.get("alt", "")
            src = attr_dict.get("src", "")
            if alt:
                self._current_line += f"![{alt}]({src})"
            elif src:
                self._current_line += f"![image]({src})"

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
            indent = "  " * (self._list_depth - 1) if self._list_depth > 0 else ""
            if self._list_type_stack and self._list_type_stack[-1] == "ol":
                num = self._list_counters[-1] if self._list_counters else 1
                self._current_line = f"{indent}{num}. "
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

        elif tag == "dl":
            self._flush_line()
            self._in_dl = True

        elif tag == "dt":
            self._flush_line()
            self._current_line = "**"

        elif tag == "dd":
            self._flush_line()
            self._current_line = ": "

        elif tag == "details":
            self._flush_line()

        elif tag == "summary":
            self._current_line = "**"

        elif tag == "div":
            # Detect note/warning/tip callout divs
            note_classes = ["note", "warning", "tip", "important", "caution"]
            if any(nc in cls.lower() for nc in note_classes):
                self._flush_line()
                self._tag_stack.append({"tag": "div", "is_note": True})
                self._current_line = "> **Note:** "
            # MadCap menu cascade spans are sometimes in divs
            # Just recurse normally for other divs

        elif tag == "span":
            # Handle MadCap UI controls
            if "uicontrol" in cls:
                self._current_line += "**"
                self._tag_stack.append({"tag": "span", "cls": "uicontrol"})
            elif "menucascade" in cls:
                self._tag_stack.append({"tag": "span", "cls": "menucascade"})

    def handle_endtag(self, tag: str):
        if tag == "title":
            self._in_title = False
            return

        if not self._in_content:
            return

        if self._skip_depth > 0:
            self._skip_depth -= 1
            if self._skip_depth == 0:
                self._content_depth -= 1
            return

        if tag in self.SKIP_TAGS:
            self._skip_depth = 0
            self._content_depth -= 1
            return

        self._content_depth -= 1

        # Check if we've exited the content area
        if self._content_depth <= 0:
            self._flush_line()
            self._in_content = False
            self._content_depth = 0
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
                if href and not href.startswith("#") and not href.startswith("javascript:"):
                    # Convert relative URLs to absolute
                    if not href.startswith("http"):
                        href = f"{HELP_BASE_URL}/content/topics/{href}"
                    self._current_line += f"]({href})"
                else:
                    self._current_line += "]"

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
                    self._table_rows.append(["---"] * max(len(self._current_row), 1))

        elif tag in ("td", "th"):
            cell = self._current_cell.strip().replace("|", "\\|").replace("\n", " ")
            self._current_row.append(cell)

        elif tag == "dl":
            self._flush_line()
            self._in_dl = False
            self._output.append("")

        elif tag == "dt":
            self._current_line += "**"
            self._flush_line()

        elif tag == "dd":
            self._flush_line()
            self._output.append("")

        elif tag == "summary":
            self._current_line += "**"
            self._flush_line()
            self._output.append("")

        elif tag == "details":
            self._flush_line()
            self._output.append("")

        elif tag == "div":
            # Check for note div ending
            if self._tag_stack and self._tag_stack[-1].get("tag") == "div" and self._tag_stack[-1].get("is_note"):
                self._flush_line()
                self._output.append("")
                self._tag_stack.pop()

        elif tag == "span":
            if self._tag_stack and self._tag_stack[-1].get("tag") == "span":
                cls = self._tag_stack[-1].get("cls", "")
                if cls == "uicontrol":
                    self._current_line += "**"
                    self._tag_stack.pop()
                elif cls == "menucascade":
                    self._tag_stack.pop()

    def handle_data(self, data: str):
        if self._in_title and not self._title:
            self._title = data.strip()
            return

        if not self._in_content or self._skip_depth > 0:
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
            text = re.sub(r"\s+", " ", text)

        if in_blockquote:
            for line in text.split("\n"):
                line = line.strip()
                if line:
                    if not self._current_line:
                        self._current_line = "> " + line
                    else:
                        self._current_line += line
        else:
            self._current_line += text

    def handle_entityref(self, name: str):
        entities = {
            "amp": "&", "lt": "<", "gt": ">", "quot": '"',
            "apos": "'", "nbsp": " ", "mdash": "\u2014", "ndash": "\u2013",
            "lsquo": "\u2018", "rsquo": "\u2019",
            "ldquo": "\u201c", "rdquo": "\u201d",
        }
        char = entities.get(name, f"&{name};")
        if self._in_code_block:
            self._code_content += char
        elif self._in_table:
            self._current_cell += char
        elif self._in_content and not self._skip_depth:
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
        elif self._in_content and not self._skip_depth:
            self._current_line += char

    def _flush_line(self):
        line = self._current_line.rstrip()
        if line:
            self._output.append(line)
        self._current_line = ""

    def _flush_table(self):
        if not self._table_rows:
            return
        # Normalize column count
        max_cols = max(len(r) for r in self._table_rows) if self._table_rows else 0
        if max_cols == 0:
            return
        for row in self._table_rows:
            while len(row) < max_cols:
                row.append("")

        self._output.append("")
        for row in self._table_rows:
            self._output.append("| " + " | ".join(row) + " |")
        self._output.append("")

    def get_markdown(self) -> str:
        self._flush_line()
        cleaned = []
        prev_blank = False
        for line in self._output:
            is_blank = not line.strip()
            if is_blank and prev_blank:
                continue
            cleaned.append(line)
            prev_blank = is_blank

        while cleaned and not cleaned[0].strip():
            cleaned.pop(0)
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()

        return "\n".join(cleaned) + "\n" if cleaned else ""

    def get_title(self) -> str:
        return self._title


def html_to_markdown(html_content: str) -> tuple[str, str]:
    """Convert Okta DITA HTML to markdown. Returns (title, markdown_content)."""
    parser = MadCapExtractor()
    parser.feed(html_content)
    return parser.get_title(), parser.get_markdown()


# ---------------------------------------------------------------------------
# Part 1: API docs (three OpenAPI YAML specs)
# ---------------------------------------------------------------------------


def sync_api_spec(
    spec_key: str,
    spec_info: dict,
    raw_yaml: str,
    spec: dict,
    cache: dict,
    new_cache: dict,
    args: argparse.Namespace,
) -> tuple[int, int, int, int]:
    """Sync a single OpenAPI spec to markdown. Returns (added, updated, unchanged, removed)."""
    prefix = spec_info["cache_prefix"]
    spec_api_dir = os.path.join(API_DOCS_DIR, spec_key)
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

    added = 0
    updated = 0
    unchanged = 0

    for tag in sorted(endpoints_by_tag.keys()):
        endpoints = endpoints_by_tag[tag]
        safe_tag = sanitize_filename(tag)
        tag_dir = os.path.join(spec_api_dir, safe_tag)
        tag_desc = tag_descriptions.get(tag, "")

        # Tag README
        readme_content = build_tag_readme(tag, tag_desc, endpoints)
        readme_path = os.path.join(tag_dir, "README.md")
        cache_key = f"{prefix}:{safe_tag}:README"
        content_hash = sha256(readme_content)

        status = cache_check(cache, new_cache, cache_key, content_hash, readme_path)
        if status == "unchanged":
            unchanged += 1
        else:
            is_new = status == "new"
            write_file(readme_path, readme_content, args, f"api/{spec_key}/{safe_tag}/README.md", is_new)
            if is_new:
                added += 1
            else:
                updated += 1

        # Endpoint files
        for ep in endpoints:
            ep_content = build_endpoint_markdown(ep["path"], ep["method"], ep["operation"], spec)
            ep_path = os.path.join(tag_dir, ep["filename"])
            cache_key = f"{prefix}:{safe_tag}:{ep['filename']}"
            content_hash = sha256(ep_content)

            status = cache_check(cache, new_cache, cache_key, content_hash, ep_path)
            if status == "unchanged":
                unchanged += 1
            else:
                is_new = status == "new"
                write_file(ep_path, ep_content, args, f"api/{spec_key}/{safe_tag}/{ep['filename']}", is_new)
                if is_new:
                    added += 1
                else:
                    updated += 1

    # Spec-level README
    info = spec.get("info", {})
    readme_lines = [f"# {info.get('title', spec_info['name'])}\n"]
    desc = info.get("description", "")
    if desc:
        readme_lines.append(f"{desc}\n")
    readme_lines.append(f"**Version:** {info.get('version', '?')}\n")
    servers = spec.get("servers", [])
    if servers:
        readme_lines.append(f"**Base URL:** `{servers[0].get('url', '')}`\n")
    if info.get("termsOfService"):
        readme_lines.append(f"**Terms of Service:** {info['termsOfService']}\n")
    if info.get("contact"):
        contact = info["contact"]
        readme_lines.append("**Contact:**\n")
        if contact.get("name"):
            readme_lines.append(f"- Name: {contact['name']}")
        if contact.get("url"):
            readme_lines.append(f"- URL: {contact['url']}")
        if contact.get("email"):
            readme_lines.append(f"- Email: {contact['email']}")
        readme_lines.append("")
    readme_lines.append("## API Categories\n")
    for tag in sorted(endpoints_by_tag.keys()):
        safe_tag = sanitize_filename(tag)
        count = len(endpoints_by_tag[tag])
        readme_lines.append(f"- [{tag}](./{safe_tag}/) ({count} endpoints)")
    readme_lines.append("")
    spec_readme = "\n".join(readme_lines)

    if not args.dry_run:
        os.makedirs(spec_api_dir, exist_ok=True)
        with open(os.path.join(spec_api_dir, "README.md"), "w", encoding="utf-8") as f:
            f.write(spec_readme)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key.startswith(f"{prefix}:") and old_key not in new_cache:
            parts = old_key.split(":", 2)
            if len(parts) == 3:
                old_path = os.path.join(spec_api_dir, parts[1], parts[2])
                if os.path.exists(old_path):
                    if args.dry_run:
                        print(f"  REMOVE api/{spec_key}/{parts[1]}/{parts[2]}")
                    else:
                        os.remove(old_path)
                        if args.verbose:
                            print(f"  REMOVE api/{spec_key}/{parts[1]}/{parts[2]}")
                    removed += 1

    # Clean empty dirs
    if not args.dry_run and os.path.isdir(spec_api_dir):
        for entry in os.scandir(spec_api_dir):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)

    return added, updated, unchanged, removed


def sync_api(
    cache: dict, new_cache: dict, args: argparse.Namespace
) -> tuple[int, int, int, int]:
    """Fetch and sync all three Okta OpenAPI specs. Returns (added, updated, unchanged, removed)."""
    total_added = 0
    total_updated = 0
    total_unchanged = 0
    total_removed = 0

    spec_summaries: list[tuple[str, str, str]] = []  # (key, name, description)

    print(f"  Fetching {len(API_SPECS)} specs concurrently...")
    raw_specs: dict[str, str | None] = {}
    with ThreadPoolExecutor(max_workers=len(API_SPECS)) as pool:
        futures = {
            pool.submit(fetch_url, info["url"]): key
            for key, info in API_SPECS.items()
        }
        for future in as_completed(futures):
            raw_specs[futures[future]] = future.result()

    for spec_key, spec_info in API_SPECS.items():
        print(f"  Processing {spec_info['name']}...")
        raw_yaml_content = raw_specs.get(spec_key)
        if not raw_yaml_content:
            print(f"  ERROR: Could not fetch {spec_info['name']}, skipping", file=sys.stderr)
            prefix = spec_info["cache_prefix"]
            for key, value in cache.items():
                if key.startswith(f"{prefix}:"):
                    new_cache[key] = value
            continue

        try:
            spec = yaml.safe_load(raw_yaml_content)
        except yaml.YAMLError as e:
            print(f"  ERROR: Could not parse {spec_info['name']} YAML: {e}", file=sys.stderr)
            continue

        info = spec.get("info", {})
        print(f"    Version: {info.get('version', '?')}")
        print(f"    Paths: {len(spec.get('paths', {}))}")

        # Save raw spec file
        if not args.dry_run:
            spec_path = os.path.join(SCRIPT_DIR, spec_info["spec_file"])
            with open(spec_path, "w", encoding="utf-8") as f:
                f.write(raw_yaml_content)

        a, u, uc, r = sync_api_spec(
            spec_key, spec_info, raw_yaml_content, spec, cache, new_cache, args
        )
        print(f"    {spec_info['name']}: +{a} ~{u} ={uc} -{r}")

        total_added += a
        total_updated += u
        total_unchanged += uc
        total_removed += r

        spec_summaries.append((
            spec_key,
            info.get("title", spec_info["name"]),
            info.get("description", ""),
        ))

    # Build top-level api/ README
    api_readme_lines = ["# Okta REST API Documentation\n"]
    api_readme_lines.append(
        "Complete API reference documentation for all Okta REST APIs, "
        "generated from official OpenAPI specifications.\n"
    )
    api_readme_lines.append("## API Categories\n")
    for key, title, desc in spec_summaries:
        api_readme_lines.append(f"### [{title}](./{key}/)\n")
        if desc:
            api_readme_lines.append(f"{desc}\n")
    api_readme_lines.append("")
    api_readme = "\n".join(api_readme_lines)

    if not args.dry_run:
        os.makedirs(API_DOCS_DIR, exist_ok=True)
        with open(os.path.join(API_DOCS_DIR, "README.md"), "w", encoding="utf-8") as f:
            f.write(api_readme)

    return total_added, total_updated, total_unchanged, total_removed


# ---------------------------------------------------------------------------
# Part 2: Help docs (DITA HTML scraping from help.okta.com)
# ---------------------------------------------------------------------------


def fetch_all_help_pages() -> dict[str, str]:
    """Fetch the current DITA site's sitemap and return {path: title}."""
    print(f"  Fetching help sitemap from {HELP_SITEMAP_URL}...")
    raw = fetch_url(HELP_SITEMAP_URL)
    if not raw:
        raise RuntimeError("Could not fetch help sitemap")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise ValueError(f"Could not parse help sitemap: {e}") from e

    pages: dict[str, str] = {}
    prefix = HELP_BASE_URL.rstrip("/")
    for loc in root.iter():
        if not loc.tag.endswith("loc") or not loc.text:
            continue
        url = loc.text.strip()
        if not url.startswith(prefix + "/"):
            continue
        path = url[len(prefix):]
        if path.lower().endswith((".htm", ".html")):
            # Titles are extracted from each page's <title>/<h1>.
            pages[path] = ""
    print(f"  Total help pages found: {len(pages)}")
    return pages


def sanitize_help_path(url_path: str) -> str:
    """Convert a help URL path to a clean filesystem path."""
    # Remove /content/topics/ prefix and .htm suffix
    path = re.sub(r"^/content/topics/", "", url_path)
    path = re.sub(r"^/content/", "", path)
    path = re.sub(r"\.htm$", "", path)
    # Sanitize characters
    path = re.sub(r"[^\w/.-]", "-", path)
    return path


def sync_help(
    cache: dict, new_cache: dict, args: argparse.Namespace
) -> tuple[int, int, int, int]:
    """Fetch and sync Okta help documentation. Returns (added, updated, unchanged, removed)."""
    try:
        all_pages = fetch_all_help_pages()
    except (RuntimeError, ValueError) as e:
        print(f"  ERROR: {e}", file=sys.stderr)
        carried = 0
        for key, value in cache.items():
            if key.startswith("help:"):
                new_cache[key] = value
                carried += 1
        return 0, 0, carried, 0

    if not all_pages:
        print("  No help pages found")
        return 0, 0, 0, 0

    pages_list = sorted(all_pages.items())

    if args.dry_run:
        print(f"  {len(pages_list)} help pages would be fetched")
        # Still mark them in new_cache for removal tracking
        for url_path, title in pages_list:
            clean_path = sanitize_help_path(url_path)
            parts = clean_path.split("/")
            filename = parts[-1] + ".md"
            subdir = "/".join(parts[:-1]) if len(parts) > 1 else ""
            cache_key = f"help:{subdir}/{filename}" if subdir else f"help:{filename}"
            # Carry forward existing cache entries
            if cache_key in cache:
                new_cache[cache_key] = cache[cache_key]
        return 0, 0, len(pages_list), 0

    added = 0
    updated = 0
    unchanged = 0
    failed = 0

    # Track sections for building indexes
    sections: dict[str, list[tuple[str, str, str]]] = {}  # section -> [(filename, title, url_path)]

    total = len(pages_list)
    completed = [0]  # Use list for mutability in closure
    lock_print = __import__("threading").Lock()

    def process_page(
        url_path: str, title: str
    ) -> tuple[str, str, str, str, str, str, str, str | None]:
        """Fetch and process a single help page."""
        clean_path = sanitize_help_path(url_path)
        parts = clean_path.split("/")
        filename = parts[-1] + ".md"
        subdir = "/".join(parts[:-1]) if len(parts) > 1 else ""
        cache_key = f"help:{subdir}/{filename}" if subdir else f"help:{filename}"

        if subdir:
            target_dir = os.path.join(HELP_DOCS_DIR, subdir)
        else:
            target_dir = HELP_DOCS_DIR
        file_path = os.path.join(target_dir, filename)
        label = f"help/{subdir}/{filename}" if subdir else f"help/{filename}"
        previous = cache.get(cache_key, {})
        display_title = previous.get("title") or title or filename.replace(".md", "")

        if REQUEST_DELAY:
            time.sleep(REQUEST_DELAY)
        url = f"{HELP_BASE_URL}{url_path}"
        request_etag = previous.get("etag") if os.path.exists(file_path) else None
        if request_etag:
            head_etag, head_succeeded = fetch_etag_with_retry(url)
            if head_succeeded and head_etag == request_etag:
                return (
                    "unchanged", cache_key, "", file_path, label, "",
                    display_title, head_etag,
                )
        raw_html, response_etag, not_modified = fetch_url_with_retry(
            url, etag=request_etag)
        if not_modified:
            return (
                "unchanged", cache_key, "", file_path, label, "",
                display_title, response_etag,
            )

        if not raw_html:
            return (
                "failed", cache_key, "", file_path, label, "",
                display_title, None,
            )

        page_title, markdown = html_to_markdown(raw_html)
        if not markdown.strip() or len(markdown.strip()) < 50:
            return (
                "skipped", cache_key, "", file_path, label, "",
                display_title, response_etag,
            )

        # Build full page content with header. The DITA article usually repeats
        # its title as an h1, which would otherwise duplicate our stable header.
        display_title = title or page_title or filename.replace(".md", "")
        markdown_lines = markdown.lstrip().splitlines()
        if (
            markdown_lines
            and markdown_lines[0].strip().casefold()
            == f"# {display_title}".casefold()
        ):
            markdown = "\n".join(markdown_lines[1:]).lstrip()
        content = f"# {display_title}\n\n"
        content += f"**Source:** {HELP_BASE_URL}{url_path}\n\n"
        content += "---\n\n"
        content += markdown

        content_hash = sha256(content)
        return (
            "ok", cache_key, content_hash, file_path, label, content,
            display_title, response_etag,
        )

    # Process pages concurrently
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {}
        for url_path, title in pages_list:
            future = executor.submit(process_page, url_path, title)
            futures[future] = (url_path, title)

        for future in as_completed(futures):
            url_path, title = futures[future]
            try:
                (
                    status, cache_key, content_hash, file_path, label, content,
                    display_title, response_etag,
                ) = future.result()
            except Exception as e:
                print(f"    ERROR processing {url_path}: {e}", file=sys.stderr)
                failed += 1
                completed[0] += 1
                continue

            clean_path = sanitize_help_path(url_path)
            parts = clean_path.split("/")
            filename = parts[-1] + ".md"
            subdir = "/".join(parts[:-1]) if len(parts) > 1 else ""
            section = parts[0] if len(parts) > 1 else "root"

            preserve_existing = (
                cache_key in cache and os.path.exists(file_path)
            )
            if status in ("ok", "unchanged") or preserve_existing:
                sections.setdefault(section, []).append(
                    (filename, display_title, url_path))

            if status == "failed":
                failed += 1
                if preserve_existing:
                    new_cache[cache_key] = cache[cache_key]
            elif status == "skipped":
                if preserve_existing:
                    new_cache[cache_key] = cache[cache_key]
            elif status == "unchanged":
                unchanged += 1
                new_cache[cache_key] = {
                    **cache[cache_key],
                    "etag": response_etag,
                    "title": display_title,
                    "url": url_path,
                }
            elif status == "ok":
                # Cache check
                cache_status = cache_check(cache, new_cache, cache_key, content_hash, file_path)
                new_cache[cache_key] = {
                    **new_cache[cache_key],
                    "etag": response_etag,
                    "title": display_title,
                    "url": url_path,
                }
                if cache_status == "unchanged":
                    unchanged += 1
                else:
                    is_new = cache_status == "new"
                    os.makedirs(os.path.dirname(file_path), exist_ok=True)
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    if args.verbose:
                        with lock_print:
                            print(f"  {'ADD' if is_new else 'UPDATE'} {label}")
                    if is_new:
                        added += 1
                    else:
                        updated += 1

            completed[0] += 1
            if completed[0] % 50 == 0 or completed[0] == total:
                with lock_print:
                    print(
                        f"    [{completed[0]}/{total}] +{added} ~{updated} "
                        f"={unchanged} failed:{failed}"
                    )

    # Build section README indexes
    for section, pages in sorted(sections.items()):
        if section == "root":
            continue
        section_title = section.replace("-", " ").replace("_", " ").title()
        readme_lines = [f"# {section_title}\n"]
        for fn, t, _ in sorted(pages, key=lambda x: x[1].lower()):
            readme_lines.append(f"- [{t}](./{fn})")
        readme_lines.append("")
        readme_content = "\n".join(readme_lines)

        readme_path = os.path.join(HELP_DOCS_DIR, section, "README.md")
        cache_key = f"help:{section}:README"
        content_hash = sha256(readme_content)

        status = cache_check(cache, new_cache, cache_key, content_hash, readme_path)
        if status == "unchanged":
            unchanged += 1
        else:
            is_new = status == "new"
            os.makedirs(os.path.dirname(readme_path), exist_ok=True)
            with open(readme_path, "w", encoding="utf-8") as f:
                f.write(readme_content)
            if is_new:
                added += 1
            else:
                updated += 1

    # Top-level help/ README
    help_readme_lines = ["# Okta Help Documentation\n"]
    help_readme_lines.append(
        "Complete Okta admin documentation scraped from help.okta.com.\n"
    )
    total_pages = sum(len(p) for p in sections.values())
    help_readme_lines.append(f"**Total pages:** {total_pages}\n")
    help_readme_lines.append("## Sections\n")
    for section in sorted(sections.keys()):
        if section == "root":
            continue
        section_title = section.replace("-", " ").replace("_", " ").title()
        count = len(sections[section])
        help_readme_lines.append(f"- [{section_title}](./{section}/) ({count} pages)")
    # Root pages
    if "root" in sections:
        help_readme_lines.append("")
        help_readme_lines.append("## Other Pages\n")
        for fn, t, _ in sorted(sections["root"], key=lambda x: x[1].lower()):
            help_readme_lines.append(f"- [{t}](./{fn})")
    help_readme_lines.append("")
    help_readme = "\n".join(help_readme_lines)

    if not args.dry_run:
        os.makedirs(HELP_DOCS_DIR, exist_ok=True)
        with open(os.path.join(HELP_DOCS_DIR, "README.md"), "w", encoding="utf-8") as f:
            f.write(help_readme)

    # Detect removals
    removed = 0
    for old_key in sorted(cache):
        if old_key.startswith("help:") and old_key not in new_cache:
            # Reconstruct path from cache key
            rest = old_key[5:]  # strip "help:" prefix
            old_path = os.path.join(HELP_DOCS_DIR, rest)
            if os.path.exists(old_path):
                if args.dry_run:
                    print(f"  REMOVE {old_key}")
                else:
                    os.remove(old_path)
                    if args.verbose:
                        print(f"  REMOVE {old_key}")
                removed += 1

    # Clean empty dirs
    if not args.dry_run and os.path.isdir(HELP_DOCS_DIR):
        for root, dirs, files in os.walk(HELP_DOCS_DIR, topdown=False):
            for d in dirs:
                dir_path = os.path.join(root, d)
                try:
                    if not os.listdir(dir_path):
                        os.rmdir(dir_path)
                except OSError:
                    pass

    return added, updated, unchanged, removed


# ---------------------------------------------------------------------------
# Main sync orchestration
# ---------------------------------------------------------------------------


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()
    new_cache: dict = {}

    # --- Part 1: API docs from OpenAPI specs ---
    print("Syncing API docs (3 OpenAPI specs)...")
    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    api_added, api_updated, api_unchanged, api_removed = sync_api(cache, new_cache, args)
    print(f"  API total: +{api_added} ~{api_updated} ={api_unchanged} -{api_removed}")

    # --- Part 2: Help docs from help.okta.com ---
    print("\nSyncing help docs (help.okta.com)...")
    help_added, help_updated, help_unchanged, help_removed = sync_help(cache, new_cache, args)
    print(f"  Help total: +{help_added} ~{help_updated} ={help_unchanged} -{help_removed}")

    # Top-level docs/ README
    top_lines = ["# Okta Documentation\n"]
    top_lines.append("- [API Reference](./api/) -- REST API endpoints generated from OpenAPI specs")
    top_lines.append("- [Help Documentation](./help/) -- Admin documentation from help.okta.com")
    top_lines.append("")
    if not args.dry_run:
        with open(os.path.join(DOCS_DIR, "README.md"), "w", encoding="utf-8") as f:
            f.write("\n".join(top_lines))

    # Save cache
    if not args.dry_run:
        save_cache(new_cache)

    total_added = api_added + help_added
    total_updated = api_updated + help_updated
    total_unchanged = api_unchanged + help_unchanged
    total_removed = api_removed + help_removed

    print(f"\nSync complete:")
    print(f"  Added:     {total_added}")
    print(f"  Updated:   {total_updated}")
    print(f"  Unchanged: {total_unchanged}")
    print(f"  Removed:   {total_removed}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Okta API and help docs, convert to markdown"
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
