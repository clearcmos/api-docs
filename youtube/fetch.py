#!/usr/bin/env python3

"""
YouTube Data API v3 documentation fetcher.

Scrapes the human-readable API reference under
https://developers.google.com/youtube/v3/docs and converts each page to
markdown. The raw OpenAPI-style discovery doc for YouTube v3 already lives
in google/docs/youtube-v3.md; this fetcher captures the narrative reference
(parameter descriptions, request/response examples, errors) that the
discovery doc does not include.

Source layout (Google Devsite):
  * Landing page /youtube/v3/docs lists all endpoint URLs as <a href> links
    inside <div class="devsite-article-body">. Method URLs look like
    /youtube/v3/docs/{resource}/{method}; a handful of resource overview
    pages (/youtube/v3/docs/{resource}) are not linked but exist - we derive
    them from the method URLs.
  * Each page's body is a self-contained <div class="devsite-article-body">.
    The body has <section id="...">, headings, paragraphs, parameter
    tables, devsite-code blocks, and div.note admonitions.
  * The auxiliary /youtube/v3/docs/errors page is included.

Output layout:
  docs/
    README.md                       (auto-generated catalogue)
    index.md                        (landing page content)
    errors.md                       (errors page)
    {resource}/
      README.md                     (auto-generated catalogue for resource)
      index.md                      (resource overview page)
      {method}.md                   (one per endpoint, e.g. list.md)
"""

import argparse
import gzip
import hashlib
import html.parser
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SITE = "https://developers.google.com"
INDEX_URL = f"{SITE}/youtube/v3/docs"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

MAX_WORKERS = 24

# Resource overview pages that exist on the site but are not linked from the
# /youtube/v3/docs index. Discovered by probing /youtube/v3/docs/{name}.
# Anything else is derived from the method URLs themselves.
EXTRA_RESOURCE_PAGES = (
    "activities",
    "captions",
    "channelBanners",
    "channelSections",
    "commentThreads",
    "i18nLanguages",
    "i18nRegions",
    "members",
    "membershipsLevels",
    "playlistItems",
    "search",
    "subscriptions",
    "thumbnails",
    "videoAbuseReportReasons",
    "videoCategories",
)

# Auxiliary pages directly under /youtube/v3/docs/.
EXTRA_PATHS = ("errors",)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": "youtube-docs-fetcher/1.0",
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
            data = json.load(f)
            return data if isinstance(data, dict) else {}
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
# URL discovery
# ---------------------------------------------------------------------------


def extract_doc_urls(index_html: str) -> set[str]:
    """Pull all /youtube/v3/docs/* link targets out of the article body."""
    m = re.search(r'<div class="devsite-article-body[^"]*"[^>]*>', index_html)
    body = index_html[m.start() :] if m else index_html
    paths = set(re.findall(r'href="(/youtube/v3/docs/[^"#?]+)"', body))
    # Normalize trailing slashes.
    return {p.rstrip("/") for p in paths}


def derive_resource_paths(method_paths: set[str]) -> set[str]:
    """Given /youtube/v3/docs/{resource}/{method} URLs, return the set of
    /youtube/v3/docs/{resource} parents."""
    resources = set()
    for p in method_paths:
        tail = p[len("/youtube/v3/docs/") :]
        if "/" in tail:
            resource = tail.split("/", 1)[0]
            resources.add(f"/youtube/v3/docs/{resource}")
    return resources


def plan_pages() -> list[tuple[str, str]]:
    """Return list of (url, output_relpath) pairs to fetch.

    output_relpath is relative to DOCS_DIR and has a .md extension.
    """
    print(f"Fetching index page {INDEX_URL}")
    index_html = fetch_url(INDEX_URL)
    if not index_html:
        print("ERROR: failed to fetch index page", file=sys.stderr)
        sys.exit(1)

    method_paths = extract_doc_urls(index_html)
    resource_paths = derive_resource_paths(method_paths)
    # Augment with extra resource pages that are not linked.
    for name in EXTRA_RESOURCE_PAGES:
        resource_paths.add(f"/youtube/v3/docs/{name}")
    extra_paths = {f"/youtube/v3/docs/{p}" for p in EXTRA_PATHS}

    plan: list[tuple[str, str]] = []

    # Landing page itself.
    plan.append((INDEX_URL, "index.md"))

    # Auxiliary pages (errors).
    for path in sorted(extra_paths):
        name = path[len("/youtube/v3/docs/") :]
        plan.append((SITE + path, f"{name}.md"))

    # Resource overview pages.
    for path in sorted(resource_paths):
        resource = path[len("/youtube/v3/docs/") :]
        plan.append((SITE + path, f"{resource}/index.md"))

    # Method pages.
    for path in sorted(method_paths):
        tail = path[len("/youtube/v3/docs/") :]
        if "/" not in tail:
            # Already covered by resource overview pages above.
            continue
        resource, method = tail.split("/", 1)
        # Some methods themselves contain slashes? On YouTube v3 they do not.
        method_file = method.replace("/", "_")
        plan.append((SITE + path, f"{resource}/{method_file}.md"))

    return plan


# ---------------------------------------------------------------------------
# HTML to Markdown converter
# ---------------------------------------------------------------------------

INLINE_TAGS = {
    "a",
    "code",
    "strong",
    "b",
    "em",
    "i",
    "span",
    "sup",
    "sub",
    "s",
    "tt",
    "var",
    "kbd",
    "mark",
    "small",
}


class DevsiteExtractor(html.parser.HTMLParser):
    """Extracts and converts a Google Devsite article body to markdown.

    The parser walks the entire HTML page but only emits output once it
    enters <div class="devsite-article-body">. Inside the body it strips
    nav chrome, the "Stay organized with collections" banner, <devsite-iframe>
    interactive panels, and <style>/<script>, then renders the remaining
    block structure as markdown.
    """

    SKIP_TAGS = {
        "style",
        "script",
        "noscript",
        "devsite-iframe",
        "iframe",
        "devsite-toc",
        "devsite-content-footer",
        "devsite-thumb-rating",
    }

    # Classes that turn a <div>/<aside> into a blockquote-style callout.
    # The original HTML already contains its own label (e.g. "<b>Note:</b>"),
    # so we only need to wrap subsequent flushed lines in "> " quoting.
    NOTE_CLASSES = {
        "note",
        "caution",
        "warning",
        "tip",
        "key-point",
        "key-term",
        "objective",
        "success",
        "beta",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_body = False
        self._body_depth = 0  # nesting depth of divs while inside body
        self._skip_depth = 0
        self._skip_tag: str | None = None  # tag name that opened the skip

        self._output: list[str] = []
        self._current_line = ""

        # Code-block state.
        self._pre_depth = 0
        self._code_content = ""
        self._code_lang = ""

        # List state: stack of ("ul"|"ol", counter).
        self._list_stack: list[list] = []

        # Table state.
        self._in_table = False
        self._table_rows: list[list[dict]] = []
        self._current_row: list[dict] = []
        self._current_cell: list[str] = []
        self._cell_is_header = False
        self._cell_colspan = 1
        self._in_thead = False
        self._cell_buffer_target: str | None = None  # "line" or "cell"

        # Admonition state: stack of dicts {body_depth, tag} recording the
        # body_depth value at the time the note opened, so we can pop on its
        # matching close. While the stack is non-empty, every flushed line is
        # prefixed with "> ".
        self._notes: list[dict] = []

        # Link/inline emphasis stack.
        self._tag_stack: list[dict] = []

        # Heading state.
        self._in_heading = 0  # heading level if inside h1-h6

        # Skip the meta breadcrumb/header block at top of article (.devsite-article-meta).
        self._meta_skip_depth = 0

        # Skip the "Stay organized with collections" block (.devsite-collection-link).
        # That widget has its own container; detect via class.
        self._collection_skip_depth = 0

        # Captured title (page <h1>, or document <title> as fallback).
        self.title = ""
        self.doc_title = ""

        # Track depths at which interesting state opened so we can pop them
        # symmetrically on </div>.

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _flush_line(self):
        line = self._current_line.rstrip()
        self._current_line = ""
        if not line:
            return
        if self._notes:
            line = "> " + line
        self._output.append(line)

    def _append_inline(self, text: str):
        if self._pre_depth > 0:
            self._code_content += text
            return
        if self._in_table and self._cell_buffer_target == "cell":
            self._current_cell.append(text)
            return
        self._current_line += text

    def _ensure_blank(self):
        if self._output and self._output[-1].strip():
            self._output.append("")

    def _is_skipped(self) -> bool:
        return self._skip_depth > 0

    # ------------------------------------------------------------------
    # Parser handlers
    # ------------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "")

        # Capture <title> globally so we can fall back to it for the page header.
        if tag == "title" and not self._in_body:
            self._tag_stack.append({"tag": "doctitle"})
            return

        # Skip subtree blocks even before we enter the body — we don't care,
        # but we do need to be ready to skip inside the body.
        if not self._in_body:
            if tag == "div" and "devsite-article-body" in cls:
                self._in_body = True
                self._body_depth = 1
                return
            return

        # Inside the body now.

        # Track div depth for body exit detection.
        if tag == "div":
            self._body_depth += 1

        # Honor existing skip context.
        if self._skip_depth > 0:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        # Skip blocks: meta nav, collection banner, devsite chrome, AI panels.
        if tag == "div":
            classes = cls.split()
            chrome = {
                "devsite-article-meta",
                "devsite-banner",
                "devsite-collection-link",
                "devsite-key-takeaways-panel",
                "devsite-thumb-rating",
                "devsite-content-footer",
                "devsite-floating-action-buttons",
            }
            if any(c in chrome for c in classes):
                self._skip_depth = 1
                self._skip_tag = "div"
                return

        # Drop decorative material-icons spans (rendered as icon ligatures on
        # the website, leaking as words like "outlined_flag" in text).
        if tag == "span" and "material-icons" in cls.split():
            self._skip_depth = 1
            self._skip_tag = "span"
            return

        if tag in self.SKIP_TAGS:
            self._skip_depth = 1
            self._skip_tag = tag
            return

        # Note / admonition blocks. Devsite uses <aside class="note"> and
        # <div class="note"> styles. The HTML already contains its own label
        # (e.g. "<b>Note:</b>"), so we just wrap subsequent content in a
        # blockquote and let the existing inline label flow through.
        if tag in ("aside", "div"):
            classes = set(cls.split())
            if classes & self.NOTE_CLASSES:
                self._flush_line()
                self._ensure_blank()
                # body_depth was already incremented above for div; record the
                # current value so we can match on close.
                self._notes.append({"body_depth": self._body_depth, "tag": tag})
                return

        # devsite-code wraps a <pre>. We just let the <pre> below kick in.
        if tag == "devsite-code":
            return

        if tag == "pre":
            self._flush_line()
            self._pre_depth += 1
            if self._pre_depth == 1:
                self._code_content = ""
                self._code_lang = a.get("data-lang", "") or ""
            return

        if tag == "code":
            if self._pre_depth > 0:
                for c in cls.split():
                    if c.startswith("language-"):
                        self._code_lang = c[9:]
                        break
                return
            self._append_inline("`")
            self._tag_stack.append({"tag": "code"})
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_line()
            self._ensure_blank()
            level = int(tag[1])
            self._in_heading = level
            self._current_line = "#" * level + " "
            self._tag_stack.append({"tag": tag})
            return

        if tag == "p":
            # Don't flush if we're sitting on a bare list-item marker -- AsciiDoc
            # and devsite both wrap list-item text in <p>, and we want the
            # marker and the paragraph text on the same line.
            if not self._is_bare_marker(self._current_line):
                self._flush_line()
            # Drop the special quota-impact paragraph into a single blockquote line.
            if "special" in cls.split():
                # Treat as a note-like callout for one paragraph.
                self._tag_stack.append({"tag": "p", "special": True})
                self._ensure_blank()
                self._current_line = "> "
                return
            self._tag_stack.append({"tag": "p"})
            return

        if tag == "br":
            self._flush_line()
            return

        if tag == "hr":
            self._flush_line()
            self._ensure_blank()
            self._output.append("---")
            self._output.append("")
            return

        if tag in ("strong", "b"):
            # Inside <code>, decorative <strong> would render as `**foo**`
            # which is ugly; skip the markers and let the text through.
            inside_code = any(t.get("tag") == "code" for t in self._tag_stack)
            if not inside_code:
                self._append_inline("**")
            self._tag_stack.append({"tag": "strong", "inside_code": inside_code})
            return

        if tag in ("em", "i"):
            inside_code = any(t.get("tag") == "code" for t in self._tag_stack)
            if not inside_code:
                self._append_inline("*")
            self._tag_stack.append({"tag": "em", "inside_code": inside_code})
            return

        if tag == "a":
            href = a.get("href", "")
            # Strip pure anchor links to nothing useful (e.g. "#").
            if not href:
                self._tag_stack.append({"tag": "a", "href": "", "suppress": True})
                return
            href = self._normalize_href(href)
            self._append_inline("[")
            self._tag_stack.append({"tag": "a", "href": href, "suppress": False})
            return

        if tag == "img":
            alt = a.get("alt", "").strip()
            src = a.get("src", "").strip()
            if src:
                if src.startswith("/"):
                    src = SITE + src
                self._append_inline(f"![{alt}]({src})")
            return

        if tag in ("ul", "ol"):
            if self._in_table and self._cell_buffer_target == "cell":
                # Inside a table cell, list structure is flattened into the
                # cell text so it doesn't leak orphan markers into the output.
                self._current_cell.append(" ")
                return
            self._flush_line()
            self._list_stack.append([tag, 0])
            return

        if tag == "li":
            if self._in_table and self._cell_buffer_target == "cell":
                self._current_cell.append(" ")
                return
            self._flush_line()
            if not self._list_stack:
                self._list_stack.append(["ul", 0])
            self._list_stack[-1][1] += 1
            indent = "  " * (len(self._list_stack) - 1)
            marker = f"{self._list_stack[-1][1]}. " if self._list_stack[-1][0] == "ol" else "- "
            self._current_line = indent + marker
            return

        if tag == "dl":
            self._flush_line()
            return
        if tag == "dt":
            self._flush_line()
            self._ensure_blank()
            self._current_line = "**"
            self._tag_stack.append({"tag": "dt"})
            return
        if tag == "dd":
            self._flush_line()
            self._current_line = ": "
            self._tag_stack.append({"tag": "dd"})
            return

        if tag == "blockquote":
            self._flush_line()
            self._ensure_blank()
            self._tag_stack.append({"tag": "blockquote"})
            return

        if tag == "table":
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
                self._current_cell = []
                self._cell_is_header = tag == "th" or self._in_thead
                self._cell_buffer_target = "cell"
                try:
                    self._cell_colspan = int(a.get("colspan") or "1")
                except ValueError:
                    self._cell_colspan = 1
            return

        # Sections / details / summary / nav / aside default-through.

    def handle_endtag(self, tag):
        if tag == "title" and self._tag_stack and self._tag_stack[-1].get("tag") == "doctitle":
            self._tag_stack.pop()
            return

        if not self._in_body:
            return

        # Bookkeep div depth for body exit.
        if tag == "div":
            self._body_depth -= 1
            if self._body_depth <= 0:
                self._flush_line()
                self._in_body = False
                self._skip_depth = 0
                return

        if self._skip_depth > 0:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return

        # Closing of an admonition block (aside or div with note class).
        if self._notes and self._notes[-1]["tag"] == tag:
            # For div, body_depth was already decremented above. The recorded
            # body_depth is the depth *while inside* the note, so when we exit
            # it the live depth is one less. For aside, no decrement happened.
            live_depth = self._body_depth if tag == "div" else self._body_depth
            target = self._notes[-1]["body_depth"] - (1 if tag == "div" else 0)
            if live_depth == target:
                self._flush_line()
                self._notes.pop()
                self._ensure_blank()
                return

        if tag == "devsite-code":
            return

        if tag == "pre":
            self._pre_depth -= 1
            if self._pre_depth == 0:
                self._ensure_blank()
                self._output.append(f"```{self._code_lang}".rstrip())
                body = self._code_content.rstrip("\n")
                for ln in body.split("\n"):
                    self._output.append(ln)
                self._output.append("```")
                self._output.append("")
            return

        if tag == "code":
            if self._pre_depth > 0:
                return
            if self._tag_stack and self._tag_stack[-1].get("tag") == "code":
                self._tag_stack.pop()
                self._append_inline("`")
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            # Capture title from first h1.
            if tag == "h1" and not self.title:
                self.title = self._current_line.lstrip("#").strip()
            self._flush_line()
            self._output.append("")
            self._in_heading = 0
            if self._tag_stack and self._tag_stack[-1].get("tag") == tag:
                self._tag_stack.pop()
            return

        if tag == "p":
            top = self._tag_stack[-1] if self._tag_stack else None
            self._tag_stack.pop() if top and top.get("tag") == "p" else None
            self._flush_line()
            self._ensure_blank()
            return

        if tag in ("strong", "b"):
            if self._tag_stack and self._tag_stack[-1].get("tag") == "strong":
                meta = self._tag_stack.pop()
                if not meta.get("inside_code"):
                    self._append_inline("**")
            return
        if tag in ("em", "i"):
            if self._tag_stack and self._tag_stack[-1].get("tag") == "em":
                meta = self._tag_stack.pop()
                if not meta.get("inside_code"):
                    self._append_inline("*")
            return

        if tag == "a":
            if self._tag_stack and self._tag_stack[-1].get("tag") == "a":
                meta = self._tag_stack.pop()
                if meta.get("suppress"):
                    return
                href = meta.get("href", "")
                self._append_inline(f"]({href})")
            return

        if tag in ("ul", "ol"):
            if self._in_table and self._cell_buffer_target == "cell":
                return
            if self._list_stack:
                self._list_stack.pop()
            self._flush_line()
            if not self._list_stack:
                self._ensure_blank()
            return

        if tag == "li":
            if self._in_table and self._cell_buffer_target == "cell":
                return
            self._flush_line()
            return

        if tag in ("dt", "dd"):
            if tag == "dt":
                self._current_line += "**"
            self._flush_line()
            if self._tag_stack and self._tag_stack[-1].get("tag") == tag:
                self._tag_stack.pop()
            return

        if tag == "blockquote":
            if self._tag_stack and self._tag_stack[-1].get("tag") == "blockquote":
                self._tag_stack.pop()
            self._flush_line()
            self._ensure_blank()
            return

        if tag in ("td", "th"):
            if self._in_table:
                text = "".join(self._current_cell).strip()
                # Collapse whitespace inside cells.
                text = re.sub(r"\s+", " ", text)
                self._current_row.append(
                    {
                        "text": text,
                        "is_header": self._cell_is_header,
                        "colspan": self._cell_colspan,
                    }
                )
                self._current_cell = []
                self._cell_buffer_target = None
                self._cell_colspan = 1
            return
        if tag == "tr":
            if self._in_table and self._current_row:
                self._table_rows.append(self._current_row)
                self._current_row = []
            return
        if tag == "thead":
            self._in_thead = False
            return
        if tag == "tbody":
            return
        if tag == "table":
            if self._in_table:
                self._render_table()
                self._in_table = False
                self._table_rows = []
            return

    def handle_data(self, data):
        if self._tag_stack and self._tag_stack[-1].get("tag") == "doctitle":
            self.doc_title += data
            return
        if not self._in_body or self._is_skipped():
            return
        if self._pre_depth > 0:
            self._code_content += data
            return
        if self._in_table and self._cell_buffer_target == "cell":
            self._current_cell.append(data)
            return
        # Collapse runs of whitespace, but keep at least one space.
        if data.strip() == "" and self._current_line.endswith(" "):
            return
        normalized = re.sub(r"[\t\n]+", " ", data)
        normalized = re.sub(r"  +", " ", normalized)
        if not self._current_line and not self._in_heading:
            # Strip leading space at the start of a fresh line.
            normalized = normalized.lstrip()
        self._current_line += normalized

    # ------------------------------------------------------------------
    # Tables
    # ------------------------------------------------------------------

    def _render_table(self):
        rows = self._table_rows
        if not rows:
            return
        self._ensure_blank()

        # Heuristic: YouTube parameter tables use colspan="2"/"3" rows as
        # section dividers (Required parameters / Filters / Optional
        # parameters / Properties). Two-column data rows are
        # parameter-name + description. Render dividers as bold headings
        # and data rows as a definition-list block, since the description
        # cell often contains lists/code which break markdown tables.
        is_definition_style = any(len(r) == 1 and r[0].get("colspan", 1) >= 2 for r in rows) or all(
            len(r) <= 2 for r in rows
        )

        if is_definition_style:
            for row in rows:
                if len(row) == 1 and row[0].get("colspan", 1) >= 2:
                    label = row[0]["text"].strip()
                    # Devsite divider cells already use <b>; our inline pass
                    # has wrapped them in ** ** -- avoid the **** doubling.
                    label = re.sub(r"^\*\*(.*?)\*\*$", r"\1", label)
                    if label:
                        self._output.append("")
                        self._output.append(f"**{label}**")
                        self._output.append("")
                    continue
                if len(row) == 1:
                    self._output.append(row[0]["text"].strip())
                    continue
                name = row[0]["text"].strip()
                desc = row[1]["text"].strip() if len(row) > 1 else ""
                if not name and not desc:
                    continue
                if name:
                    self._output.append(f"- **{name}**")
                if desc:
                    # Soft-wrap-ish: keep as a single indented line.
                    for line in desc.split("\n"):
                        if line.strip():
                            self._output.append(f"  {line.strip()}")
            self._output.append("")
            return

        # Otherwise, render a real markdown table.
        # Find header row (first row containing header cells) or default to first row.
        header_idx = 0
        for i, row in enumerate(rows):
            if any(c["is_header"] for c in row):
                header_idx = i
                break
        header = rows[header_idx]
        body_rows = [r for i, r in enumerate(rows) if i != header_idx]
        ncols = max(len(header), max((len(r) for r in body_rows), default=0))
        if ncols == 0:
            return
        header_cells = [c["text"] for c in header] + [""] * (ncols - len(header))
        self._output.append("| " + " | ".join(self._escape_cell(c) for c in header_cells) + " |")
        self._output.append("| " + " | ".join(["---"] * ncols) + " |")
        for r in body_rows:
            cells = [c["text"] for c in r] + [""] * (ncols - len(r))
            self._output.append("| " + " | ".join(self._escape_cell(c) for c in cells) + " |")
        self._output.append("")

    @staticmethod
    def _escape_cell(text: str) -> str:
        return text.replace("|", "\\|").replace("\n", " ").strip()

    @staticmethod
    def _is_bare_marker(line: str) -> bool:
        """True if `line` is just leading whitespace + a list marker."""
        if not line:
            return False
        stripped = line.rstrip()
        if not stripped:
            return False
        # Bullet marker: optional indent + "- "
        if re.match(r"^\s*-\s*$", stripped + " "):
            return True
        # Numeric marker: optional indent + "N. "
        return bool(re.match(r"^\s*\d+\.\s*$", stripped + " "))

    # ------------------------------------------------------------------
    # Link normalization
    # ------------------------------------------------------------------

    def _normalize_href(self, href: str) -> str:
        # Absolute URLs and anchors pass through.
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return href
        if href.startswith("/"):
            return SITE + href
        return href

    # ------------------------------------------------------------------
    # Finalization
    # ------------------------------------------------------------------

    def finalize(self) -> str:
        self._flush_line()
        # Collapse 3+ consecutive blank lines to 2.
        out: list[str] = []
        blank_run = 0
        for line in self._output:
            if line.strip() == "":
                blank_run += 1
                if blank_run > 1:
                    continue
            else:
                blank_run = 0
            out.append(line)
        # Strip leading/trailing blanks.
        while out and not out[0].strip():
            out.pop(0)
        while out and not out[-1].strip():
            out.pop()
        text = "\n".join(out) + "\n"
        # Rewrite `[text](url)` (a Markdown link wrapped in backticks, which
        # the renderer treats as literal text) to [`text`](url).
        text = re.sub(r"`\[([^\]\n`]+)\]\(([^)\n]+)\)`", r"[`\1`](\2)", text)
        return text


def html_to_markdown(html_text: str, source_url: str) -> str | None:
    parser = DevsiteExtractor()
    parser.feed(html_text)
    body = parser.finalize()
    if not body.strip():
        return None
    # Prefer the article body's h1. Fall back to the <title> minus the
    # Devsite " | Google for Developers" tail; last resort is the URL slug.
    if parser.title:
        header_title = parser.title
    elif parser.doc_title:
        t = parser.doc_title.replace("\xa0", " ")
        # Devsite separator is "&nbsp;|&nbsp;" — split on the pipe variants
        # and take the first segment. Don't split on " - " because hyphens
        # often appear inside actual titles (e.g. "API - Errors").
        if " | " in t:
            t = t.split(" | ")[0]
        header_title = re.sub(r"\s+", " ", t).strip()
    else:
        header_title = source_url.rsplit("/", 1)[-1]
    header = f"# {header_title}\n\nSource: {source_url}\n\n"
    # If the body already starts with the same h1, strip it to avoid duplication.
    lines = body.split("\n")
    if lines and lines[0].startswith("# ") and lines[0][2:].strip() == header_title:
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        body = "\n".join(lines) + "\n"
    return header + body


# ---------------------------------------------------------------------------
# README generation
# ---------------------------------------------------------------------------


def build_resource_readme(resource: str, methods: list[str]) -> str:
    lines = [f"# {resource}", ""]
    lines.append(f"Source: {SITE}/youtube/v3/docs/{resource}")
    lines.append("")
    lines.append("## Methods")
    lines.append("")
    lines.append("- [Resource overview](./index.md)")
    for m in sorted(methods):
        lines.append(f"- [{m}](./{m}.md)")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(resources: dict[str, list[str]], extras: list[str]) -> str:
    lines = [
        "# YouTube Data API v3",
        "",
        f"Source: {INDEX_URL}",
        "",
        "Scraped reference documentation. Pair this with the raw discovery",
        "document at `google/docs/youtube-v3.md` for the machine-readable",
        "schema.",
        "",
        "## Pages",
        "",
        "- [API Reference (landing page)](./index.md)",
    ]
    for name in sorted(extras):
        lines.append(f"- [{name}](./{name}.md)")
    lines.append("")
    lines.append("## Resources")
    lines.append("")
    for resource in sorted(resources):
        methods = resources[resource]
        method_list = ", ".join(sorted(methods)) if methods else "overview only"
        lines.append(f"- [{resource}](./{resource}/README.md) - {method_list}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def fetch_page(url: str, out_rel: str, verbose: bool) -> tuple[str, str, str | None]:
    html = fetch_url(url)
    if html is None:
        return url, out_rel, None
    md = html_to_markdown(html, url)
    if md is None and verbose:
        print(f"  SKIP {out_rel}: empty body", file=sys.stderr)
    return url, out_rel, md


def sync(args: argparse.Namespace) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    cache = {} if args.force else load_cache()

    plan = plan_pages()
    print(f"Planning {len(plan)} pages")

    results: dict[str, tuple[str, str | None]] = {}  # out_rel -> (url, content)

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_page, url, rel, args.verbose) for url, rel in plan]
        for future in as_completed(futures):
            url, rel, content = future.result()
            results[rel] = (url, content)

    new_cache: dict[str, dict] = {}
    added = updated = unchanged = failed = 0

    # Track which pages belong to each resource for README generation.
    resources: dict[str, list[str]] = {}
    extras: list[str] = []

    for rel, (url, content) in sorted(results.items()):
        out_path = os.path.join(DOCS_DIR, rel)
        cached = cache.get(rel, {})
        if content is None:
            failed += 1
            if cached and os.path.exists(out_path):
                unchanged += 1
                new_cache[rel] = cached
            else:
                continue
        else:
            h = sha256(content)
            if cached.get("sha256") == h and os.path.exists(out_path):
                unchanged += 1
                new_cache[rel] = cached
            else:
                is_new = rel not in cache or not os.path.exists(out_path)
                label = "ADD" if is_new else "UPDATE"
                write_file(out_path, content, dry_run=args.dry_run, verbose=args.verbose, label=label)
                new_cache[rel] = {
                    "sha256": h,
                    "url": url,
                    "last_updated": datetime.now(UTC).isoformat(),
                }
                if is_new:
                    added += 1
                else:
                    updated += 1
                    if not args.verbose:
                        print(f"  UPDATE {rel}")

        # Bookkeeping for catalogues.
        parts = rel.split("/")
        if len(parts) == 1:
            base = parts[0]
            if base not in ("index.md", "README.md"):
                extras.append(base[:-3])
        elif len(parts) == 2:
            resource = parts[0]
            fname = parts[1]
            resources.setdefault(resource, [])
            if fname not in ("index.md", "README.md"):
                resources[resource].append(fname[:-3])

    # Generate per-resource READMEs.
    for resource, methods in resources.items():
        readme = build_resource_readme(resource, methods)
        rel = f"{resource}/README.md"
        out_path = os.path.join(DOCS_DIR, rel)
        h = sha256(readme)
        cached = cache.get(rel, {})
        if cached.get("sha256") == h and os.path.exists(out_path):
            unchanged += 1
            new_cache[rel] = cached
            continue
        is_new = rel not in cache or not os.path.exists(out_path)
        write_file(
            out_path, readme, dry_run=args.dry_run, verbose=args.verbose, label="ADD" if is_new else "UPDATE"
        )
        new_cache[rel] = {
            "sha256": h,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Top-level README.
    top = build_top_readme(resources, extras)
    rel = "README.md"
    out_path = os.path.join(DOCS_DIR, rel)
    h = sha256(top)
    cached = cache.get(rel, {})
    if cached.get("sha256") == h and os.path.exists(out_path):
        unchanged += 1
        new_cache[rel] = cached
    else:
        is_new = rel not in cache or not os.path.exists(out_path)
        write_file(
            out_path, top, dry_run=args.dry_run, verbose=args.verbose, label="ADD" if is_new else "UPDATE"
        )
        new_cache[rel] = {
            "sha256": h,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Detect removals.
    removed = 0
    for old_rel in sorted(cache):
        if old_rel in new_cache:
            continue
        out_path = os.path.join(DOCS_DIR, old_rel)
        if args.dry_run:
            print(f"  REMOVE {old_rel}")
            removed += 1
            continue
        if os.path.exists(out_path):
            os.remove(out_path)
            removed += 1
            print(f"  REMOVE {old_rel}")

    if not args.dry_run:
        save_cache(new_cache)

    print()
    print("Sync complete:")
    print(f"  Added:     {added}")
    print(f"  Updated:   {updated}")
    print(f"  Unchanged: {unchanged}")
    print(f"  Removed:   {removed}")
    print(f"  Failed:    {failed}")
    print(f"  Total:     {added + updated + unchanged}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(
        description="Sync YouTube Data API v3 reference docs to markdown",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-download everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-page logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
