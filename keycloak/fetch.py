#!/usr/bin/env python3

"""
Keycloak Documentation Fetcher

Scrapes all human-readable documentation pages linked from
https://www.keycloak.org/documentation as markdown.

Sources:
  1. Sitemap-derived guide pages under /server/, /getting-started/,
     /high-availability/, /operator/, /observability/, /ui-customization/,
     /securing-apps/, /migration/ -- each is an AsciiDoc-rendered HTML
     page with content in <div class="kc-asciidoc" id="guide-body">.
  2. Monolithic reference manuals under /docs/latest/ (server_admin,
     server_development, release_notes, authorization_services,
     upgrading) -- each is a single large HTML page with content in
     <div id="content">.
  3. The Admin REST API reference at /docs-api/latest/rest-api/ -- same
     AsciiDoc single-page layout as the manuals.
  4. The /documentation and /guides index pages themselves.

Blog posts and JavaDoc are excluded (not in scope for this scraper).

All pages are converted from HTML to Markdown using stdlib html.parser.
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
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

SITE = "https://www.keycloak.org"
SITEMAP_URL = f"{SITE}/sitemap.xml"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

MAX_WORKERS = 24

# URL path prefixes that belong to the /documentation scope.
# Any sitemap URL whose path starts with one of these is included.
GUIDE_PREFIXES = (
    "/server/",
    "/getting-started/",
    "/high-availability/",
    "/operator/",
    "/observability/",
    "/ui-customization/",
    "/securing-apps/",
    "/migration/",
)

# Exact sitemap paths outside the prefixes above that belong in scope. The
# /documentation and /guides index pages are deliberately not included: they
# are link-only landing pages whose role is served by the generated
# top-level README.
GUIDE_EXACT: tuple[str, ...] = ()

# Monolithic reference manuals. Each is one large HTML page.
MANUALS = (
    ("server_admin", f"{SITE}/docs/latest/server_admin/index.html"),
    ("server_development", f"{SITE}/docs/latest/server_development/index.html"),
    ("release_notes", f"{SITE}/docs/latest/release_notes/index.html"),
    ("authorization_services", f"{SITE}/docs/latest/authorization_services/index.html"),
    ("upgrading", f"{SITE}/docs/latest/upgrading/index.html"),
    ("rest-api", f"{SITE}/docs-api/latest/rest-api/index.html"),
)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 120) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": "keycloak-docs-fetcher/1.0",
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data: bytes = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="replace")
    except HTTPError as e:
        if e.code == 404:
            return None
        print(f"ERROR: {url}: HTTP {e.code}", file=sys.stderr)
        return None
    except (URLError, TimeoutError, OSError) as e:
        print(f"ERROR: {url}: {e}", file=sys.stderr)
        return None


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            return cast(dict, json.load(f))
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def write_file(path: str, content: str, *, dry_run: bool, verbose: bool, label: str) -> None:
    rel = os.path.relpath(path, DOCS_DIR)
    if dry_run:
        print(f"  {label} {rel}")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(content)
    if verbose:
        print(f"  {label} {rel}")


# ---------------------------------------------------------------------------
# HTML to Markdown converter
# ---------------------------------------------------------------------------


class KeycloakHTMLExtractor(html.parser.HTMLParser):
    """Converts Keycloak AsciiDoc-rendered HTML pages to markdown.

    Two content container patterns are supported:
      * guide pages: <div class="kc-asciidoc" id="guide-body">
      * manual pages: <div id="content">

    Once inside the container, tags are translated to markdown. Sidebar
    TOCs, "edit this guide" links, navbars, footers, and other site
    chrome outside the container are ignored by construction.
    """

    ADMONITION_LABELS = {
        "note": "Note",
        "tip": "Tip",
        "important": "Important",
        "warning": "Warning",
        "caution": "Caution",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_content = False
        self._content_depth = 0
        self._skip_depth = 0
        self._output: list[str] = []
        self._current_line = ""

        # Tag context stack; each entry describes one open element that
        # affects markdown output (e.g. a link, a blockquote, a header).
        self._tag_stack: list[dict] = []

        # Code-block state.
        self._code_depth = 0  # nesting depth inside <pre>
        self._code_content = ""
        self._code_lang = ""

        # List state.
        self._list_types: list[str] = []  # "ul" / "ol"
        self._list_counters: list[int] = []

        # Table state.
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._current_row: list[str] = []
        self._current_cell = ""
        self._in_thead = False
        self._cell_is_header = False

        # Admonition state (stack of type names).
        self._admonitions: list[tuple[str, int]] = []
        self._admonition_skip_icon = 0  # skip depth inside <td class="icon">

        # Page title.
        self._title = ""
        self._in_title_tag = False

    # ------------------------------------------------------------------
    # Parser event handlers
    # ------------------------------------------------------------------

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]):
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "")
        tid = a.get("id", "")

        # Always capture the document <title>.
        if tag == "title":
            self._in_title_tag = True
            return

        # Enter main content container.
        if not self._in_content:
            is_guide_body = tag == "div" and tid == "guide-body"
            is_manual_content = tag == "div" and tid == "content"
            if is_guide_body or is_manual_content:
                self._in_content = True
                self._content_depth = 1
                return
            # Outside content: ignore everything except the title capture above.
            return

        # Already inside content: track nesting so we can detect exit.
        if tag == "div":
            self._content_depth += 1

        if self._skip_depth > 0:
            if tag == "div":
                self._skip_depth += 1
            return

        # Skip sidebars, page-link boxes, ToC blocks, figure captions' images.
        skip_classes = (
            "sidebarblock",  # <div class="sidebarblock page-links">
            "top-menu-guides",  # top-of-manual menu
            "top-menu-version",  # version picker
        )
        if tag == "div" and any(sc in cls for sc in skip_classes):
            self._skip_depth = 1
            return
        if tag == "div" and tid in ("toc", "toctitle", "footer"):
            self._skip_depth = 1
            return

        # Handle AsciiDoc admonition blocks: <div class="admonitionblock note">.
        # Structure is an admonition div containing a table whose content cell
        # holds the body.
        # We render as a blockquote prefixed with the admonition label, and
        # skip the icon cell entirely. We record the content_depth at which
        # the admonition opened so we can pop it when the matching </div> closes.
        if tag == "div":
            for kind in self.ADMONITION_LABELS:
                if f"admonitionblock {kind}" in cls or cls.startswith(f"admonitionblock {kind}"):
                    self._flush_line()
                    self._output.append("")
                    label = self.ADMONITION_LABELS[kind]
                    self._output.append(f"> **{label}:**")
                    self._admonitions.append((kind, self._content_depth))
                    return

        # Inside an admonition, skip the icon cell but keep the content cell.
        if self._admonitions and tag == "td" and "icon" in cls:
            self._admonition_skip_icon = 1
            return

        if self._admonition_skip_icon > 0:
            if tag == "td":
                self._admonition_skip_icon += 1
            return

        # Listing block <pre> may be nested inside <div class="listingblock">.
        # Language is on the inner <code class="language-X">.
        if tag == "pre":
            self._flush_line()
            self._code_depth += 1
            if self._code_depth == 1:
                self._code_content = ""
                self._code_lang = ""
                # Some AsciiDoc pre elements expose data-lang on pre itself.
                self._code_lang = a.get("data-lang", "") or self._code_lang
            return

        if tag == "code":
            if self._code_depth > 0:
                # Nested <code> inside <pre>: just absorb its language.
                for c in cls.split():
                    if c.startswith("language-"):
                        self._code_lang = c[9:]
                        break
                lang = a.get("data-lang", "")
                if lang:
                    self._code_lang = lang
                return
            if self._in_table:
                self._current_cell += "`"
            else:
                self._current_line += "`"
            self._tag_stack.append({"tag": "code"})
            return

        # Headings.
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_line()
            # Add a blank separator above the heading.
            if self._output and self._output[-1].strip():
                self._output.append("")
            level = int(tag[1])
            self._current_line = "#" * level + " "
            self._tag_stack.append({"tag": tag})
            return

        # Paragraph: flush the previous paragraph, but don't flush if the
        # current line is just a list-item marker (AsciiDoc always nests
        # <p> inside <li>, and we want marker and text on the same line).
        if tag == "p":
            if not self._is_marker_only(self._current_line):
                self._flush_line()
            return

        # Inline emphasis.
        if tag in ("strong", "b"):
            self._append_inline("**")
            self._tag_stack.append({"tag": "strong"})
            return
        if tag in ("em", "i"):
            self._append_inline("*")
            self._tag_stack.append({"tag": "em"})
            return

        if tag == "a":
            href = a.get("href", "")
            # Drop AsciiDoc's empty heading-anchor links entirely. Those look
            # like <a class="anchor" href="#..."></a> just before a heading.
            if "anchor" in cls.split() or (href.startswith("#") and "anchor" in cls):
                self._tag_stack.append({"tag": "a", "href": "", "suppress": True})
                return
            self._tag_stack.append({"tag": "a", "href": href, "suppress": False})
            self._append_inline("[")
            return

        if tag == "img":
            alt = a.get("alt", "").strip()
            src = a.get("src", "").strip()
            if src:
                self._append_inline(f"![{alt}]({src})")
            return

        if tag == "br":
            self._flush_line()
            return

        if tag == "hr":
            self._flush_line()
            self._output.append("")
            self._output.append("---")
            self._output.append("")
            return

        if tag in ("ul", "ol"):
            self._flush_line()
            self._list_types.append(tag)
            self._list_counters.append(0)
            return

        if tag == "li":
            self._flush_line()
            if not self._list_types:
                # Defensive: list item without list wrapper.
                self._list_types.append("ul")
                self._list_counters.append(0)
            self._list_counters[-1] += 1
            indent = "  " * (len(self._list_types) - 1)
            marker = f"{self._list_counters[-1]}. " if self._list_types[-1] == "ol" else "- "
            self._current_line = indent + marker
            return

        if tag == "blockquote":
            self._flush_line()
            self._tag_stack.append({"tag": "blockquote"})
            return

        if tag == "table":
            # Admonition blocks use tables internally; skip the wrapping
            # table so its structure doesn't leak into markdown.
            if self._admonitions and not self._in_table:
                return
            self._flush_line()
            self._in_table = True
            self._table_rows = []
            return

        if tag == "thead":
            self._in_thead = True
            return

        if tag == "tbody":
            return

        if tag == "tr":
            if self._in_table:
                self._current_row = []
            return

        if tag in ("td", "th"):
            if self._in_table:
                self._current_cell = ""
                self._cell_is_header = tag == "th" or self._in_thead
            return

        # Unknown/structural tags are a no-op: their text content still flows
        # through handle_data.

    def handle_endtag(self, tag: str):
        if tag == "title":
            self._in_title_tag = False
            return

        if not self._in_content:
            return

        if self._skip_depth > 0:
            if tag == "div":
                self._skip_depth -= 1
            if self._skip_depth <= 0:
                self._skip_depth = 0
            # Still track the outer content depth so we can detect exit.
            if tag == "div":
                self._content_depth -= 1
                if self._content_depth <= 0:
                    self._flush_line()
                    self._in_content = False
            return

        # Inside the admonition icon cell, drop every end-tag until the <td>
        # closes -- otherwise stray `*`/`**` from unmatched <i>/<strong> etc
        # leaks into the output.
        if self._admonition_skip_icon > 0 and tag != "td":
            return

        # Emphasis / headings / links / code close.
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_line()
            self._output.append("")
            if self._tag_stack and self._tag_stack[-1].get("tag") == tag:
                self._tag_stack.pop()
            # Fall through to div depth bookkeeping below if relevant (no-op).
        elif tag == "p":
            self._flush_line()
            if self._output and self._output[-1].strip():
                self._output.append("")
        elif tag == "pre":
            self._code_depth -= 1
            if self._code_depth == 0:
                if self._output and self._output[-1].strip():
                    self._output.append("")
                self._output.append(f"```{self._code_lang}".rstrip())
                body = self._code_content.rstrip("\n")
                if body:
                    self._output.extend(body.split("\n"))
                self._output.append("```")
                self._output.append("")
        elif tag == "code":
            if self._code_depth > 0:
                return
            if self._tag_stack and self._tag_stack[-1].get("tag") == "code":
                self._tag_stack.pop()
                if self._in_table:
                    self._current_cell += "`"
                else:
                    self._current_line += "`"
        elif tag in ("strong", "b"):
            if self._tag_stack and self._tag_stack[-1].get("tag") == "strong":
                self._tag_stack.pop()
            self._append_inline("**")
        elif tag in ("em", "i"):
            if self._tag_stack and self._tag_stack[-1].get("tag") == "em":
                self._tag_stack.pop()
            self._append_inline("*")
        elif tag == "a":
            ctx = {"href": "", "suppress": False}
            if self._tag_stack and self._tag_stack[-1].get("tag") == "a":
                ctx = self._tag_stack.pop()
            if not ctx.get("suppress"):
                self._append_inline(f"]({ctx.get('href', '')})")
        elif tag in ("ul", "ol"):
            if self._list_types:
                self._list_types.pop()
            if self._list_counters:
                self._list_counters.pop()
            self._flush_line()
            if not self._list_types and self._output and self._output[-1].strip():
                self._output.append("")
        elif tag == "li":
            self._flush_line()
        elif tag == "blockquote":
            self._flush_line()
            if self._tag_stack and self._tag_stack[-1].get("tag") == "blockquote":
                self._tag_stack.pop()
            self._output.append("")
        elif tag == "table":
            if self._admonitions:
                # Wrapping table of an admonition; don't flush.
                return
            self._flush_table()
            self._in_table = False
        elif tag == "thead":
            self._in_thead = False
        elif tag == "tr":
            if self._in_table:
                self._table_rows.append(self._current_row)
                # After the header row, insert a markdown separator.
                if self._in_thead and self._current_row:
                    self._table_rows.append(["---"] * len(self._current_row))
        elif tag in ("td", "th"):
            # Admonition icon cell close.
            if self._admonition_skip_icon > 0:
                if tag == "td":
                    self._admonition_skip_icon -= 1
                return
            if self._in_table:
                self._current_row.append(self._current_cell.strip().replace("\n", " "))

        # Div depth bookkeeping: handle admonition pop and content-container exit.
        if tag == "div":
            # Flush any trailing inline text that accumulated just before this
            # close, so admonition prefixing applies before we pop the context.
            if self._admonitions and self._admonitions[-1][1] == self._content_depth:
                self._flush_line()
                self._admonitions.pop()
                # Trailing separator blank after admonition.
                if self._output and self._output[-1].strip():
                    self._output.append("")
            self._content_depth -= 1
            if self._content_depth <= 0:
                self._flush_line()
                self._in_content = False

    def handle_data(self, data: str):
        if self._in_title_tag and not self._title:
            self._title = data.strip()
            return

        if not self._in_content:
            return

        if self._skip_depth > 0 or self._admonition_skip_icon > 0:
            return

        if self._is_suppressed():
            return

        if self._code_depth > 0:
            self._code_content += data
            return

        if self._in_table:
            self._current_cell += data
            return

        # Preserve text; collapse internal whitespace to single spaces but
        # keep one trailing/leading space if present so adjacent inline tags
        # don't glue their text together.
        if data.strip() == "":
            if self._current_line and not self._current_line.endswith(" "):
                self._current_line += " "
            return

        # Inside headers/paragraphs/list items, collapse all whitespace.
        collapsed = re.sub(r"\s+", " ", data)
        # If current line ends with a space and new fragment starts with one,
        # avoid doubling.
        if self._current_line.endswith(" ") and collapsed.startswith(" "):
            collapsed = collapsed.lstrip()
        self._current_line += collapsed

    # ------------------------------------------------------------------
    # Emission helpers
    # ------------------------------------------------------------------

    def _append_inline(self, text: str) -> None:
        """Append inline formatting to the right buffer (table cell vs line)."""
        if self._in_table:
            self._current_cell += text
        else:
            self._current_line += text

    _MARKER_RE = re.compile(r"^\s*(?:-|\d+\.)\s*$")

    def _is_marker_only(self, line: str) -> bool:
        return bool(self._MARKER_RE.match(line))

    def _is_suppressed(self) -> bool:
        """True when we're inside a suppressed (anchor-only) <a>."""
        return any(frame.get("tag") == "a" and frame.get("suppress") for frame in reversed(self._tag_stack))

    def _flush_line(self):
        line = self._current_line.rstrip()
        if line:
            # If we're inside a blockquote, prefix with "> ".
            if any(t.get("tag") == "blockquote" for t in self._tag_stack) or self._admonitions:
                line = "> " + line
            self._output.append(line)
        elif self._admonitions and self._output and self._output[-1].startswith("> "):
            # Keep admonition paragraphs visually separated.
            self._output.append(">")
        self._current_line = ""

    def _flush_table(self):
        if not self._table_rows:
            return
        # Ensure all rows have the same column count.
        width = max(len(r) for r in self._table_rows)
        normalized = [r + [""] * (width - len(r)) for r in self._table_rows]
        # If no header row was explicitly set (no thead), synthesize one so
        # the markdown table is valid.
        has_separator = any(row and row[0] == "---" for row in normalized)
        if not has_separator and normalized:
            header = [f"Col {i + 1}" for i in range(width)]
            sep = ["---"] * width
            normalized = [header, sep] + normalized
        if self._output and self._output[-1].strip():
            self._output.append("")
        for row in normalized:
            cells = [c.replace("|", "\\|") for c in row]
            self._output.append("| " + " | ".join(cells) + " |")
        self._output.append("")

    def get_markdown(self) -> str:
        self._flush_line()
        lines = self._output
        # Collapse runs of blank lines; strip leading/trailing blanks.
        cleaned: list[str] = []
        prev_blank = True
        for line in lines:
            blank = not line.strip()
            if blank and prev_blank:
                continue
            cleaned.append(line.rstrip())
            prev_blank = blank
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        return "\n".join(cleaned) + "\n" if cleaned else ""

    def get_title(self) -> str:
        return self._title


def html_to_markdown(html_content: str) -> tuple[str, str]:
    """Convert a Keycloak doc HTML page to markdown.

    Returns (title, markdown). If the page has no recognized content
    container, markdown is empty.
    """
    parser = KeycloakHTMLExtractor()
    parser.feed(html_content)
    return parser.get_title(), parser.get_markdown()


# ---------------------------------------------------------------------------
# URL discovery
# ---------------------------------------------------------------------------


def parse_sitemap(xml_content: str) -> list[str]:
    root = ET.fromstring(xml_content)
    ns = ""
    if root.tag.startswith("{"):
        ns = root.tag.split("}")[0] + "}"
    urls: list[str] = []
    for url_elem in root.findall(f".//{ns}url"):
        loc = url_elem.find(f"{ns}loc")
        if loc is not None and loc.text:
            urls.append(loc.text.strip())
    return urls


def is_guide_url(url: str) -> bool:
    p = urlparse(url)
    if p.netloc != urlparse(SITE).netloc:
        return False
    path = p.path
    if path in GUIDE_EXACT:
        return True
    return any(path.startswith(prefix) for prefix in GUIDE_PREFIXES)


def discover_urls() -> list[str]:
    print("Fetching sitemap...")
    xml_content = fetch_url(SITEMAP_URL)
    if not xml_content:
        print("ERROR: failed to fetch sitemap", file=sys.stderr)
        sys.exit(1)
    all_urls = parse_sitemap(xml_content)
    guide_urls = [u for u in all_urls if is_guide_url(u)]
    guide_urls = sorted(set(guide_urls))
    print(f"  sitemap entries:   {len(all_urls)}")
    print(f"  guide pages:       {len(guide_urls)}")
    return guide_urls


# ---------------------------------------------------------------------------
# Path mapping
# ---------------------------------------------------------------------------


def guide_path(url: str) -> str:
    """Map a keycloak.org guide URL to a local markdown file path."""
    p = urlparse(url)
    path = p.path.strip("/")
    if not path:
        path = "index"
    parts = path.split("/")
    return os.path.join(DOCS_DIR, *parts) + ".md"


def manual_path(slug: str) -> str:
    return os.path.join(DOCS_DIR, "manuals", f"{slug}.md")


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------


def build_page_markdown(title: str, body: str, source_url: str) -> str:
    parts: list[str] = []
    if title:
        parts.append(f"# {title}")
        parts.append("")
    parts.append(f"*Source: [{source_url}]({source_url})*")
    parts.append("")
    parts.append(body.rstrip() + "\n")
    return "\n".join(parts)


def build_manuals_readme(manuals: list[dict]) -> str:
    lines = ["# Keycloak Reference Manuals", ""]
    lines.append(
        f"Monolithic reference documentation fetched from [{SITE}/documentation]({SITE}/documentation)."
    )
    lines.append("")
    for m in manuals:
        lines.append(f"- [{m['title']}](./{m['slug']}.md)")
    lines.append("")
    return "\n".join(lines)


def build_section_readme(section: str, pages: list[dict]) -> str:
    title = section.replace("-", " ").title()
    lines = [f"# {title}", ""]
    n = len(pages)
    lines.append(f"{n} page{'s' if n != 1 else ''}.")
    lines.append("")
    for p in sorted(pages, key=lambda x: x["rel"]):
        lines.append(f"- [{p['title']}](./{p['rel']})")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(sections: dict[str, list[dict]], manuals: list[dict]) -> str:
    lines = ["# Keycloak Documentation", ""]
    lines.append(f"*Mirrored from [{SITE}/documentation]({SITE}/documentation).*")
    lines.append("")
    root_pages = sections.get("_root", [])
    if root_pages:
        lines.append("## Index Pages")
        lines.append("")
        for p in sorted(root_pages, key=lambda x: x["rel"]):
            lines.append(f"- [{p['title']}](./{p['rel']})")
        lines.append("")
    if manuals:
        lines.append("## Reference Manuals")
        lines.append("")
        for m in manuals:
            lines.append(f"- [{m['title']}](./manuals/{m['slug']}.md)")
        lines.append("")
    guide_sections = {k: v for k, v in sections.items() if k != "_root"}
    if guide_sections:
        lines.append("## Guides")
        lines.append("")
        for name in sorted(guide_sections):
            display = name.replace("-", " ").title()
            lines.append(f"- [{display}](./{name}/) ({len(guide_sections[name])} pages)")
        lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def fetch_one_page(url: str) -> tuple[str, str | None]:
    return url, fetch_url(url)


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    guide_urls = discover_urls()
    manual_jobs = list(MANUALS)

    print(
        f"Fetching {len(guide_urls)} guide pages and "
        f"{len(manual_jobs)} manuals (concurrency={MAX_WORKERS})..."
    )

    fetched: dict[str, dict] = {}  # url -> {"kind", "html", "slug"?}
    missing: list[str] = []

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures: dict[Future[tuple[str, str | None]], tuple[str, str, str | None]] = {}
        for u in guide_urls:
            futures[pool.submit(fetch_one_page, u)] = ("guide", u, None)
        for manual_job_slug, u in manual_jobs:
            futures[pool.submit(fetch_one_page, u)] = ("manual", u, manual_job_slug)
        for fut in as_completed(futures):
            kind, url, fetched_slug = futures[fut]
            _, html_content = fut.result()
            if html_content is None:
                missing.append(url)
                continue
            entry = {"kind": kind, "html": html_content}
            if fetched_slug is not None:
                entry["slug"] = fetched_slug
            fetched[url] = entry

    print(f"  fetched: {len(fetched)}")
    if missing:
        print(f"  unavailable: {len(missing)}")
        for u in sorted(missing):
            print(f"    SKIP {u}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict[str, dict] = {}
    sections: dict[str, list[dict]] = {}
    manual_entries: list[dict] = []

    # --- Guide pages ---------------------------------------------------
    for url in sorted(u for u in fetched if fetched[u]["kind"] == "guide"):
        html_content = fetched[url]["html"]
        title, body = html_to_markdown(html_content)
        if not body.strip():
            if args.verbose:
                print(f"  SKIP (empty body) {url}")
            continue
        if not title:
            title = urlparse(url).path.strip("/") or url
        # Title from <title> often has a " - Keycloak" suffix; strip it.
        title = re.sub(r"\s*-\s*Keycloak\s*$", "", title).strip()
        page_md = build_page_markdown(title, body, url)
        page_hash = sha256(page_md)
        out_path = guide_path(url)
        cache_key = os.path.relpath(out_path, DOCS_DIR)

        # Group under first path segment for section READMEs.
        path_parts = urlparse(url).path.strip("/").split("/")
        section = path_parts[0] if len(path_parts) > 1 else "_root"
        if section == "_root":
            rel_from_section = os.path.basename(out_path)
        else:
            rel_from_section = os.path.relpath(out_path, os.path.join(DOCS_DIR, section))
        sections.setdefault(section, []).append(
            {
                "title": title,
                "rel": rel_from_section.replace(os.sep, "/"),
                "url": url,
            }
        )

        prev = cache.get(cache_key, {})
        if prev.get("sha256") == page_hash and os.path.exists(out_path):
            unchanged += 1
            new_cache[cache_key] = prev
            continue
        is_new = cache_key not in cache or not os.path.exists(out_path)
        write_file(
            out_path, page_md, dry_run=args.dry_run, verbose=args.verbose, label="ADD" if is_new else "UPDATE"
        )
        new_cache[cache_key] = {
            "sha256": page_hash,
            "last_updated": datetime.now(UTC).isoformat(),
            "url": url,
            "title": title,
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # --- Manuals --------------------------------------------------------
    for url in sorted(u for u in fetched if fetched[u]["kind"] == "manual"):
        entry = fetched[url]
        html_content = entry["html"]
        slug = entry["slug"]
        title, body = html_to_markdown(html_content)
        if not body.strip():
            if args.verbose:
                print(f"  SKIP (empty body) {url}")
            continue
        if not title:
            title = slug
        title = re.sub(r"\s*-\s*Keycloak\s*$", "", title).strip()
        page_md = build_page_markdown(title, body, url)
        page_hash = sha256(page_md)
        out_path = manual_path(slug)
        cache_key = os.path.relpath(out_path, DOCS_DIR)
        manual_entries.append({"slug": slug, "title": title, "url": url})

        prev = cache.get(cache_key, {})
        if prev.get("sha256") == page_hash and os.path.exists(out_path):
            unchanged += 1
            new_cache[cache_key] = prev
            continue
        is_new = cache_key not in cache or not os.path.exists(out_path)
        write_file(
            out_path, page_md, dry_run=args.dry_run, verbose=args.verbose, label="ADD" if is_new else "UPDATE"
        )
        new_cache[cache_key] = {
            "sha256": page_hash,
            "last_updated": datetime.now(UTC).isoformat(),
            "url": url,
            "title": title,
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Preserve last known-good pages and catalogue entries after a transient
    # fetch failure. A URL must disappear from discovery to be removed.
    manual_by_url = {url: slug for slug, url in manual_jobs}
    guide_url_set = set(guide_urls)
    for url in missing:
        if url in guide_url_set:
            out_path = guide_path(url)
            cache_key = os.path.relpath(out_path, DOCS_DIR)
            prev = cache.get(cache_key, {})
            if not prev or not os.path.exists(out_path):
                continue
            title = prev.get("title")
            if not title:
                try:
                    with open(out_path, encoding="utf-8") as f:
                        title = f.readline().strip().removeprefix("# ").strip()
                except OSError:
                    title = ""
            title = title or urlparse(url).path.rstrip("/").split("/")[-1]
            path_parts = urlparse(url).path.strip("/").split("/")
            section = path_parts[0] if len(path_parts) > 1 else "_root"
            rel_from_section = (
                os.path.basename(out_path)
                if section == "_root"
                else os.path.relpath(out_path, os.path.join(DOCS_DIR, section))
            )
            sections.setdefault(section, []).append(
                {
                    "title": title,
                    "rel": rel_from_section.replace(os.sep, "/"),
                    "url": url,
                }
            )
            unchanged += 1
            new_cache[cache_key] = {
                **prev,
                "title": title,
                "url": url,
            }
            continue

        manual_slug = manual_by_url.get(url)
        if manual_slug is None:
            continue
        out_path = manual_path(manual_slug)
        cache_key = os.path.relpath(out_path, DOCS_DIR)
        prev = cache.get(cache_key, {})
        if not prev or not os.path.exists(out_path):
            continue
        title = prev.get("title")
        if not title:
            try:
                with open(out_path, encoding="utf-8") as f:
                    title = f.readline().strip().removeprefix("# ").strip()
            except OSError:
                title = ""
        title = title or manual_slug
        manual_entries.append({"slug": manual_slug, "title": title, "url": url})
        unchanged += 1
        new_cache[cache_key] = {
            **prev,
            "title": title,
            "url": url,
        }

    # --- Section READMEs -----------------------------------------------
    for section, pages in sections.items():
        if section == "_root":
            continue
        readme = build_section_readme(section, pages)
        readme_path = os.path.join(DOCS_DIR, section, "README.md")
        cache_key = os.path.relpath(readme_path, DOCS_DIR)
        page_hash = sha256(readme)
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == page_hash and os.path.exists(readme_path):
            unchanged += 1
            new_cache[cache_key] = prev
            continue
        is_new = cache_key not in cache or not os.path.exists(readme_path)
        write_file(
            readme_path,
            readme,
            dry_run=args.dry_run,
            verbose=args.verbose,
            label="ADD" if is_new else "UPDATE",
        )
        new_cache[cache_key] = {
            "sha256": page_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # --- Manuals README ------------------------------------------------
    if manual_entries:
        manuals_readme = build_manuals_readme(sorted(manual_entries, key=lambda m: m["slug"]))
        path = os.path.join(DOCS_DIR, "manuals", "README.md")
        cache_key = os.path.relpath(path, DOCS_DIR)
        page_hash = sha256(manuals_readme)
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == page_hash and os.path.exists(path):
            unchanged += 1
            new_cache[cache_key] = prev
        else:
            is_new = cache_key not in cache or not os.path.exists(path)
            write_file(
                path,
                manuals_readme,
                dry_run=args.dry_run,
                verbose=args.verbose,
                label="ADD" if is_new else "UPDATE",
            )
            new_cache[cache_key] = {
                "sha256": page_hash,
                "last_updated": datetime.now(UTC).isoformat(),
            }
            if is_new:
                added += 1
            else:
                updated += 1

    # --- Top-level README ----------------------------------------------
    top_readme = build_top_readme(sections, sorted(manual_entries, key=lambda m: m["slug"]))
    top_path = os.path.join(DOCS_DIR, "README.md")
    top_key = "README.md"
    top_hash = sha256(top_readme)
    prev = cache.get(top_key, {})
    if prev.get("sha256") == top_hash and os.path.exists(top_path):
        unchanged += 1
        new_cache[top_key] = prev
    else:
        is_new = top_key not in cache or not os.path.exists(top_path)
        write_file(
            top_path,
            top_readme,
            dry_run=args.dry_run,
            verbose=args.verbose,
            label="ADD" if is_new else "UPDATE",
        )
        new_cache[top_key] = {
            "sha256": top_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # --- Removals ------------------------------------------------------
    removed = 0
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        old_path = os.path.join(DOCS_DIR, old_key)
        if not os.path.exists(old_path):
            continue
        if args.dry_run:
            print(f"  REMOVE {old_key}")
        else:
            os.remove(old_path)
            if args.verbose:
                print(f"  REMOVE {old_key}")
        removed += 1

    # Prune empty directories.
    if not args.dry_run and os.path.isdir(DOCS_DIR):
        for root, _, _ in os.walk(DOCS_DIR, topdown=False):
            if root == DOCS_DIR:
                continue
            if not os.listdir(root):
                os.rmdir(root)
                if args.verbose:
                    rel = os.path.relpath(root, DOCS_DIR)
                    print(f"  RMDIR {rel}/")

    if not args.dry_run:
        save_cache(new_cache)

    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Unavailable: {len(missing)}")
    print(f"  Sections:    {len([s for s in sections if s != '_root'])}")
    for section in sorted(sections):
        if section == "_root":
            continue
        print(f"    {section}: {len(sections[section])}")
    print(f"  Manuals:     {len(manual_entries)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Keycloak documentation and mirror as local markdown")
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything, ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
