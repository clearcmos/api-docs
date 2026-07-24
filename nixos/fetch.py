#!/usr/bin/env python3

"""
NixOS Manual Documentation Fetcher

Mirrors the NixOS stable manual as local markdown, split per section so that
incremental syncs report exactly which chapters / options / release notes
changed between runs.

Sources (all stable channel):
  1. https://nixos.org/manual/nixos/stable/             -- main manual
  2. https://nixos.org/manual/nixos/stable/options      -- configuration options
  3. https://nixos.org/manual/nixos/stable/release-notes -- release notes appendix

The pages are DocBook-rendered XHTML emitted by `nixos-render-docs`. There is
no machine-readable spec, so we slice each page by structural divs (chapter /
section / appendix / preface) and convert each chunk to markdown using a
custom html.parser-based renderer.

Directory layout produced under docs/:
  manual/
    preface.md
    {part}/{chapter}.md          # 4 parts: installation, configuration, administration, development
    contributing.md
  release-notes/
    {release}.md                 # one file per release version
  options/
    {namespace}.md               # services / programs split into subfiles by 2nd segment
    services/{service}.md
    programs/{program}.md

Each file is hashed; .cache.json maps relative path -> sha256. The end-of-run
report names every chapter / option group / release note that was added,
updated, or removed.
"""

import argparse
import gzip
import hashlib
import html as html_module
import html.parser
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://nixos.org/manual/nixos/stable"
PAGES = {
    "manual": f"{BASE}/",
    "options": f"{BASE}/options",
    "release-notes": f"{BASE}/release-notes",
}

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SOURCE_CACHE_PREFIX = "__source__:"

# Map h1 id of a part to its directory name under manual/.
PART_DIRS = {
    "ch-installation": "installation",
    "ch-configuration": "configuration",
    "ch-running": "administration",
    "ch-development": "development",
}

# Option namespaces split into per-name files (e.g. services/nginx.md).
# Everything else is grouped into one file per top-level namespace.
NESTED_OPTION_NAMESPACES = {"services", "programs"}


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(
    url: str, timeout: int = 180, etag: str | None = None
) -> tuple[str | None, str | None, bool]:
    headers = {
        "User-Agent": "nixos-docs-fetcher/1.0",
        "Accept": "text/html,application/xhtml+xml,*/*",
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
                data.decode("utf-8", errors="replace"),
                resp.headers.get("ETag"),
                False,
            )
    except HTTPError as e:
        if e.code == 304:
            return None, e.headers.get("ETag") or etag, True
        print(f"  ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
        return None, None, False
    except (URLError, TimeoutError, OSError) as e:
        print(f"  ERROR: Failed to fetch {url}: {e}", file=sys.stderr)
        return None, None, False


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
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


def slugify(name: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9._-]+", "-", name).strip("-.")
    return s or "unnamed"


def absolutize(url: str) -> str:
    """Make a relative manual link absolute so it works in the rendered file."""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("/"):
        return "https://nixos.org" + url
    if url.startswith("#"):
        # Anchor-only links: rewrite to point at the canonical manual page.
        return f"{BASE}/{url}"
    return url


# ---------------------------------------------------------------------------
# HTML to markdown converter
# ---------------------------------------------------------------------------

class NixOSHTMLRenderer(html.parser.HTMLParser):
    """Render a DocBook-rendered XHTML fragment to markdown.

    Handles the elements emitted by nixos-render-docs: structural divs
    (chapter / section / part / titlepage / toc), heading tags, lists,
    tables, programlistings, admonition blocks (note / warning / tip),
    `variablelist` definition lists, and inline code / emphasis.

    Headings inside a fragment start at level `base_heading_level` (default
    1) regardless of the source h1/h2/h3 -- the slicer normalizes by
    measuring the depth of the outermost heading and adjusting.
    """

    ADMONITIONS = {"note": "Note", "warning": "Warning", "tip": "Tip", "caution": "Caution", "important": "Important"}

    def __init__(self, base_heading_level: int = 1):
        super().__init__(convert_charrefs=True)
        self._base_level = base_heading_level

        # Number of `<div class="section">` (or chapter/preface/appendix)
        # ancestors. Used to compute heading levels: an h2 inside a section
        # that's inside a chapter renders at level base + section_depth.
        self._section_depth = 0
        # Stack of div_depths at which section_depth was incremented, so we
        # know exactly which `</div>` closes a structural div.
        self._struct_stack: list[int] = []
        # First h tag encountered: rendered at base level.
        self._first_heading_seen = False

        self._output: list[str] = []
        self._line = ""

        # Skip-stack: when > 0, ignore everything until matching close.
        self._skip = 0
        self._skip_tag: str | None = None

        # Code-block state.
        self._pre_depth = 0
        self._code_buf = ""
        self._code_lang = ""

        # Inline code (<code> outside <pre>).
        self._inline_code = 0
        # `<code class="filename">` doesn't emit backticks (it wraps a link).
        self._code_filename_depth = 0

        # Lists.
        self._list_types: list[str] = []   # "ul" / "ol"
        self._list_counters: list[int] = []

        # Definition list (<dl class="variablelist">) state.
        self._dl_depth = 0
        self._in_dt = False
        self._in_dd = False

        # Table.
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._row: list[str] = []
        self._cell = ""
        self._in_thead = False

        # Admonition (note/warning/tip): list of (kind, depth_at_open).
        self._adm: list[tuple[str, int]] = []

        # Track div nesting so we can correctly pop admonitions and skips.
        self._div_depth = 0

        # `<table class="simplelist">` is a list-of-files wrapper; render its
        # cells as plain paragraphs rather than a markdown table.
        self._simplelist_depth = 0

        # Link state.
        self._link_stack: list[dict] = []

        # Emphasis state (just to track for symmetry; markdown is symmetric).
        self._strong_depth = 0
        self._em_depth = 0

        # Suppress the empty trailing whitespace in headings.
        self._in_heading: int | None = None

    # ------------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        tid = a.get("id") or ""
        cls_set = set(cls.split())

        if tag == "div":
            self._div_depth += 1

        if self._skip > 0:
            if tag == self._skip_tag:
                self._skip += 1
            return

        # Skip TOC blocks and nav chrome. titlepage is NOT skipped because
        # nested section headings live inside it.
        if tag == "div" and ("toc" in cls_set or "navheader" in cls_set
                             or "navfooter" in cls_set or "list-of-examples" in cls_set):
            self._skip = 1
            self._skip_tag = "div"
            return

        # Track section nesting so heading levels can be computed correctly.
        if tag == "div" and ("section" in cls_set or "chapter" in cls_set
                             or "appendix" in cls_set or "preface" in cls_set
                             or "part" in cls_set):
            self._section_depth += 1
            self._struct_stack.append(self._div_depth)

        # Admonition open: <div class="note"> / "warning" / "tip" / "caution" / "important".
        if tag == "div":
            for kind in self.ADMONITIONS:
                if kind in cls_set:
                    self._flush_line()
                    if self._output and self._output[-1].strip():
                        self._output.append("")
                    self._output.append(f"> **{self.ADMONITIONS[kind]}**")
                    self._adm.append((kind, self._div_depth))
                    return

        # An h3 inside an admonition is just the admonition title which we
        # already emitted -- skip it.
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._adm:
            self._skip = 1
            self._skip_tag = tag
            return

        # Programlisting (code block). Class is "programlisting" or "programlisting LANG".
        if tag == "pre" and "programlisting" in cls_set:
            self._flush_line()
            self._pre_depth = 1
            self._code_buf = ""
            self._code_lang = ""
            for c in cls_set:
                if c != "programlisting":
                    self._code_lang = c.strip()
                    break
            return

        # Some pre tags don't have programlisting class but appear inside
        # <div class="screen"> etc. Treat all <pre> as code blocks.
        if tag == "pre":
            self._flush_line()
            self._pre_depth = 1
            self._code_buf = ""
            self._code_lang = a.get("data-lang") or ""
            return

        if tag == "code":
            if self._pre_depth > 0:
                # Pick up language hint if present.
                for c in cls_set:
                    if c.startswith("language-"):
                        self._code_lang = c[9:]
                return
            # `<code class="filename">` wraps an `<a class="filename">` link;
            # backticks around `[text](url)` would prevent the link from
            # rendering. Track it but suppress the backticks.
            if "filename" in cls_set:
                self._code_filename_depth += 1
                return
            self._inline_code += 1
            self._append("`")
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

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_line()
            if self._output and self._output[-1].strip():
                self._output.append("")
            # All DocBook section headings use h2, with hierarchy implied by
            # nesting. Use section_depth (1-based) to derive a markdown level:
            # the outermost chapter/section renders at base_level, each level
            # of nesting adds one.
            depth = max(1, self._section_depth)
            adjusted = max(1, min(6, self._base_level + depth - 1))
            self._line = "#" * adjusted + " "
            self._in_heading = adjusted
            self._first_heading_seen = True
            return

        if tag == "p":
            # Avoid double-flushing inside list-item markers.
            if self._line and not self._line_is_marker_only():
                self._flush_line()
            return

        if tag in ("strong", "b"):
            self._strong_depth += 1
            self._append("**")
            return
        if tag in ("em", "i"):
            self._em_depth += 1
            self._append("*")
            return
        # `<span class="emphasis">` always wraps an inner `<em>` in
        # nixos-render-docs output, so the inner em already produces the
        # italic markers -- the span itself is a no-op.

        if tag == "a":
            href = a.get("href", "")
            # Anchor-only "deep link" shortcuts (e.g. <a class="anchor">).
            if "anchor" in cls_set and (not href or href.startswith("#")):
                self._link_stack.append({"href": "", "suppress": True})
                return
            href = absolutize(href)
            self._link_stack.append({"href": href, "suppress": False})
            self._append("[")
            return

        if tag == "img":
            alt = a.get("alt", "").strip()
            src = absolutize(a.get("src", "").strip())
            if src:
                self._append(f"![{alt}]({src})")
            return

        if tag in ("ul", "ol"):
            self._flush_line()
            self._list_types.append(tag)
            self._list_counters.append(0)
            return

        if tag == "li":
            self._flush_line()
            if not self._list_types:
                self._list_types.append("ul")
                self._list_counters.append(0)
            self._list_counters[-1] += 1
            indent = "  " * (len(self._list_types) - 1)
            marker = f"{self._list_counters[-1]}. " if self._list_types[-1] == "ol" else "- "
            self._line = indent + marker
            return

        if tag == "blockquote":
            self._flush_line()
            return

        if tag == "dl":
            self._dl_depth += 1
            self._flush_line()
            if self._output and self._output[-1].strip():
                self._output.append("")
            return
        if tag == "dt":
            self._flush_line()
            self._in_dt = True
            self._line = "**"
            return
        if tag == "dd":
            self._flush_line()
            self._in_dd = True
            return

        if tag == "table":
            if "simplelist" in cls_set:
                self._simplelist_depth = 1
                self._flush_line()
                return
            self._flush_line()
            self._in_table = True
            self._table_rows = []
            return
        if tag == "thead":
            self._in_thead = True
            return
        if tag == "tr":
            if self._simplelist_depth > 0:
                self._flush_line()
                return
            if self._in_table:
                self._row = []
            return
        if tag in ("td", "th"):
            if self._simplelist_depth > 0:
                return
            if self._in_table:
                self._cell = ""
            return

    # ------------------------------------------------------------------

    def handle_endtag(self, tag):
        if self._skip > 0:
            if tag == self._skip_tag:
                self._skip -= 1
                if self._skip == 0:
                    self._skip_tag = None
            if tag == "div":
                self._div_depth -= 1
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._flush_line()
            self._output.append("")
            self._in_heading = None
            return

        if tag == "p":
            self._flush_line()
            if self._output and self._output[-1].strip():
                self._output.append("")
            return

        if tag == "pre":
            if self._pre_depth > 0:
                self._pre_depth = 0
                if self._output and self._output[-1].strip():
                    self._output.append("")
                lang = self._code_lang.strip()
                # Map nixos-render-docs language hints to fenced-block hints.
                lang_map = {"nix": "nix", "bash": "bash", "sh": "sh", "shell": "shell",
                            "ShellSession": "shell", "json": "json", "py": "python",
                            "yaml": "yaml", "sql": "sql", "plain": ""}
                lang = lang_map.get(lang, lang)
                self._output.append(f"```{lang}".rstrip())
                body = self._code_buf.rstrip("\n")
                if body:
                    self._output.extend(body.split("\n"))
                self._output.append("```")
                self._output.append("")
                self._code_buf = ""
                self._code_lang = ""
            return

        if tag == "code":
            if self._pre_depth > 0:
                return
            if self._code_filename_depth > 0:
                self._code_filename_depth -= 1
                return
            if self._inline_code > 0:
                self._inline_code -= 1
                self._append("`")
            return

        if tag in ("strong", "b"):
            if self._strong_depth > 0:
                self._strong_depth -= 1
                self._append("**")
            return
        if tag in ("em", "i"):
            if self._em_depth > 0:
                self._em_depth -= 1
                self._append("*")
            return
        if tag == "span":
            return

        if tag == "a":
            if self._link_stack:
                ctx = self._link_stack.pop()
                if not ctx["suppress"]:
                    href = ctx["href"]
                    if href:
                        self._append(f"]({href})")
                    else:
                        self._append("]")
            return

        if tag in ("ul", "ol"):
            if self._list_types:
                self._list_types.pop()
                self._list_counters.pop()
            self._flush_line()
            if not self._list_types and self._output and self._output[-1].strip():
                self._output.append("")
            return

        if tag == "li":
            self._flush_line()
            return

        if tag == "dt":
            if self._in_dt:
                # Close the bold marker.
                self._line = self._line.rstrip()
                if self._line.endswith("**"):
                    self._line = self._line[:-2]
                self._line += "**"
                self._flush_line()
                self._in_dt = False
            return
        if tag == "dd":
            if self._in_dd:
                self._flush_line()
                self._output.append("")
                self._in_dd = False
            return
        if tag == "dl":
            if self._dl_depth > 0:
                self._dl_depth -= 1
            return

        if tag == "table":
            if self._simplelist_depth > 0:
                self._simplelist_depth = 0
                self._flush_line()
                if self._output and self._output[-1].strip():
                    self._output.append("")
                return
            if self._in_table:
                self._flush_table()
                self._in_table = False
            return
        if tag == "thead":
            self._in_thead = False
            return
        if tag == "tr":
            if self._simplelist_depth > 0:
                self._flush_line()
                return
            if self._in_table and self._row:
                self._table_rows.append(self._row)
                if self._in_thead:
                    self._table_rows.append(["---"] * len(self._row))
            return
        if tag in ("td", "th"):
            if self._simplelist_depth > 0:
                return
            if self._in_table:
                self._row.append(self._cell.strip().replace("\n", " "))
            return

        if tag == "div":
            # Close any admonition that opened at this depth.
            while self._adm and self._adm[-1][1] == self._div_depth:
                self._flush_line()
                self._adm.pop()
                if self._output and self._output[-1].strip():
                    self._output.append("")
            # Decrement section_depth on structural-div close. Best-effort:
            # we don't track which class each open belonged to, so we rely on
            # symmetric increments. Stop at zero to avoid going negative.
            if self._section_depth > 0:
                # Only decrement when this </div> closes a structural div we
                # incremented for. We can't fully verify without a stack, but
                # since increments and div_depth are 1:1 paired, mirroring on
                # close is safe as long as we only decrement on the matching
                # structural close. To keep this simple we maintain a parallel
                # bool stack (_struct_stack).
                if self._struct_stack and self._struct_stack[-1] == self._div_depth:
                    self._struct_stack.pop()
                    self._section_depth -= 1
            self._div_depth -= 1
            return

    # ------------------------------------------------------------------

    def handle_data(self, data: str):
        if self._skip > 0:
            return

        if self._pre_depth > 0:
            self._code_buf += data
            return

        if self._link_stack and self._link_stack[-1]["suppress"]:
            return

        if self._in_table:
            self._cell += data
            return

        if data.strip() == "":
            if self._line and not self._line.endswith(" "):
                self._line += " "
            return

        collapsed = re.sub(r"\s+", " ", data)
        if self._line.endswith(" ") and collapsed.startswith(" "):
            collapsed = collapsed.lstrip()
        self._line += collapsed

    # ------------------------------------------------------------------

    def _append(self, text: str):
        if self._in_table:
            self._cell += text
        else:
            self._line += text

    def _line_is_marker_only(self) -> bool:
        return bool(re.match(r"^\s*(?:-|\d+\.)\s*$", self._line))

    def _flush_line(self):
        line = self._line.rstrip()
        if line:
            if self._adm:
                line = "> " + line
            self._output.append(line)
        elif self._adm and self._output and self._output[-1].startswith(">"):
            self._output.append(">")
        self._line = ""

    def _flush_table(self):
        if not self._table_rows:
            return
        width = max(len(r) for r in self._table_rows)
        rows = [r + [""] * (width - len(r)) for r in self._table_rows]
        has_sep = any(r and r[0] == "---" for r in rows)
        if not has_sep and rows:
            rows = [[f"Col {i + 1}" for i in range(width)], ["---"] * width] + rows
        if self._output and self._output[-1].strip():
            self._output.append("")
        for r in rows:
            cells = [c.replace("|", "\\|") for c in r]
            self._output.append("| " + " | ".join(cells) + " |")
        self._output.append("")

    # ------------------------------------------------------------------

    def render(self) -> str:
        self._flush_line()
        cleaned: list[str] = []
        prev_blank = True
        for ln in self._output:
            blank = not ln.strip()
            if blank and prev_blank:
                continue
            cleaned.append(ln.rstrip())
            prev_blank = blank
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        return ("\n".join(cleaned) + "\n") if cleaned else ""


def html_to_markdown(html_fragment: str, base_heading_level: int = 1) -> str:
    p = NixOSHTMLRenderer(base_heading_level=base_heading_level)
    p.feed(html_fragment)
    return p.render()


# ---------------------------------------------------------------------------
# HTML slicing
# ---------------------------------------------------------------------------

class StructureSlicer(html.parser.HTMLParser):
    """First-pass parser that extracts the byte ranges of every top-level
    structural chunk on a manual page.

    A "chunk" is one of:
      * <div class="preface">
      * <div class="part">
      * <div class="chapter">     -- a chapter belongs to the most recently
                                    opened part (DocBook puts chapters as
                                    siblings of <div class="part">, not
                                    descendants)
      * <div class="section">     -- top-level (non-nested) section
      * <div class="appendix">

    For each chunk we record (kind, id, title, raw_html, parent_part_id).
    """

    INTERESTING = {"preface", "part", "chapter", "section", "appendix"}

    def __init__(self, source: str):
        super().__init__(convert_charrefs=False)
        self._src = source
        self._chunks: list[dict] = []

        # Stack of currently open structural divs:
        # [{"kind": "part"|"chapter"|..., "start": offset, "id": "...", "title": "..."}].
        self._stack: list[dict] = []
        # General div nesting depth, used to align _stack entries with raw close tags.
        self._div_depth = 0
        # Heading capture state: when True, accumulate text into _heading_buf.
        self._capture_heading: dict | None = None
        self._heading_buf = ""
        # Most recently opened part (chapters following it belong to it).
        self._current_part_id = ""

    def handle_starttag(self, tag, attrs):
        if tag == "div":
            self._div_depth += 1
            a = dict(attrs)
            cls = (a.get("class") or "").split()
            for kind in self.INTERESTING:
                if kind in cls:
                    self._stack.append({
                        "kind": kind,
                        "start": self.getpos_offset(),  # offset of "<div"
                        "div_depth": self._div_depth,
                        "id": "",
                        "title": "",
                        "heading_level": 0,
                        "parent_part_id": self._current_part_id,
                    })
                    break
            return
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._stack:
            top = self._stack[-1]
            if not top["id"]:
                a = dict(attrs)
                if a.get("id"):
                    top["id"] = a["id"]
                    top["heading_level"] = int(tag[1])
                    # If this is a part, update current_part_id immediately
                    # so following chapters can attach to it.
                    if top["kind"] == "part":
                        self._current_part_id = a["id"]
                    self._capture_heading = top
                    self._heading_buf = ""
            return

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6") and self._capture_heading is not None:
            self._capture_heading["title"] = re.sub(r"\s+", " ", self._heading_buf).strip()
            self._capture_heading = None
            return
        if tag == "div":
            if self._stack and self._stack[-1]["div_depth"] == self._div_depth:
                top = self._stack.pop()
                # Ending offset is just after the </div>.
                end = self.getpos_offset_after_close()
                self._chunks.append({
                    "kind": top["kind"],
                    "id": top["id"],
                    "title": top["title"],
                    "html": self._src[top["start"]:end],
                    "depth": top["div_depth"],
                    "heading_level": top["heading_level"],
                    "parent_part_id": top["parent_part_id"],
                })
            self._div_depth -= 1

    def handle_data(self, data):
        if self._capture_heading is not None:
            self._heading_buf += data

    # html.parser exposes line/column via self.getpos(); we need byte offsets.
    # The simplest approach is to maintain our own offset by re-locating each
    # event in the source. We approximate using the parser's internal index.
    def getpos_offset(self) -> int:
        # html.parser stores rawdata + index in self; we stored source as _src.
        # The parser exposes self.getpos() (line, col). Convert to byte offset.
        line, col = self.getpos()
        return self._line_col_to_offset(line, col)

    def getpos_offset_after_close(self) -> int:
        # End of the current </div> tag.
        line, col = self.getpos()
        off = self._line_col_to_offset(line, col)
        # Find the "</...>" that contains this offset and return offset just
        # after the closing ">".
        gt = self._src.find(">", off)
        if gt == -1:
            return len(self._src)
        return gt + 1

    def _line_col_to_offset(self, line: int, col: int) -> int:
        # Cache line offsets lazily.
        if not hasattr(self, "_line_offsets"):
            offs = [0]
            for i, ch in enumerate(self._src):
                if ch == "\n":
                    offs.append(i + 1)
            offs.append(len(self._src))
            self._line_offsets = offs
        if line - 1 < 0 or line - 1 >= len(self._line_offsets):
            return len(self._src)
        return self._line_offsets[line - 1] + col

    def chunks(self) -> list[dict]:
        return self._chunks


def slice_manual(html_text: str) -> dict:
    """Slice the main manual HTML into preface, parts, chapters, contributing.

    Returns:
      {
        "preface": {id, title, html},
        "parts": {part_id: {"id", "title", "html", "chapters": [...]}},
        "extras": [{id, title, html}],   # contributing etc. (top-level chapters not in any part)
      }
    """
    sl = StructureSlicer(html_text)
    sl.feed(html_text)
    chunks = sl.chunks()

    # Collect parts first (chunks are emitted in close order, so a chapter
    # closes before its enclosing part -- pass 1 establishes the part set).
    preface = None
    parts: dict[str, dict] = {}
    for c in chunks:
        if c["kind"] == "preface":
            preface = {"id": c["id"], "title": c["title"], "html": c["html"]}
        elif c["kind"] == "part":
            parts[c["id"]] = {"id": c["id"], "title": c["title"],
                              "html": c["html"], "chapters": []}

    extras: list[dict] = []
    chapters_in_parts: dict[str, list[dict]] = {}

    for c in chunks:
        if c["kind"] != "chapter":
            continue
        entry = {"id": c["id"], "title": c["title"], "html": c["html"]}
        # Top-level chapters use h1 (e.g. "Contributing"). Chapters that
        # belong to a part use h2 -- the part already used h1.
        is_top_level = c.get("heading_level", 2) == 1
        if not is_top_level and c["parent_part_id"] and c["parent_part_id"] in parts:
            chapters_in_parts.setdefault(c["parent_part_id"], []).append(entry)
        else:
            extras.append(entry)

    for pid, ch_list in chapters_in_parts.items():
        parts[pid]["chapters"] = ch_list

    return {"preface": preface, "parts": parts, "extras": extras}


def slice_release_notes(html_text: str) -> list[dict]:
    """Each top-level release-notes section (h2 id="sec-release-X.Y") becomes
    one entry. Subsections (h3 highlights / new-modules / incompatibilities /
    notable-changes) stay nested inside their release file rather than getting
    their own files.
    """
    sl = StructureSlicer(html_text)
    sl.feed(html_text)
    out = []
    for c in sl.chunks():
        if c["kind"] != "section":
            continue
        if not c["id"].startswith("sec-release-"):
            continue
        if c.get("heading_level", 0) != 2:
            continue
        out.append({"id": c["id"], "title": c["title"], "html": c["html"]})
    return out


# ---------------------------------------------------------------------------
# Options page parsing
# ---------------------------------------------------------------------------

class OptionsParser(html.parser.HTMLParser):
    """Streaming parser for the /options page.

    Extracts every <dt><dd> pair for an option and groups them. Each option
    yields (name, html_fragment).

    The page structure is:
        <dl class="variablelist">
          <dt>...<a id="opt-NAME"></a>...<code class="option">NAME</code>...</dt>
          <dd>...body...</dd>
          <dt>...</dt>
          <dd>...</dd>
          ...
        </dl>
    """

    def __init__(self):
        super().__init__(convert_charrefs=False)
        self._in_dl = False
        self._dl_depth = 0          # variablelist nesting depth
        self._inner_dl_depth = 0    # depth of plain <dl> nested inside dt/dd
        self._dt_html = ""
        self._dd_html = ""
        self._mode: str | None = None   # "dt" or "dd" or None
        self._options: list[dict] = []
        # Best-effort name pickup from the anchor id (always present and reliable).
        self._anchor_name: str | None = None

    def options(self) -> list[dict]:
        return self._options

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = (a.get("class") or "").split()

        if tag == "dl":
            if "variablelist" in cls and not self._in_dl:
                # Enter top-level variablelist (the outer dl on the options page).
                self._in_dl = True
                self._dl_depth = 1
                return
            if self._in_dl:
                # Nested <dl> inside a dt/dd; track its depth so we don't
                # mistakenly exit variablelist on its close.
                self._inner_dl_depth += 1
                # Fall through to re-emit it into the current buffer.

        if not self._in_dl:
            return

        if self._inner_dl_depth == 0 and tag == "dt":
            self._flush_pair()
            self._mode = "dt"
            self._dt_html = ""
            self._dd_html = ""
            self._anchor_name = None
            return
        if self._inner_dl_depth == 0 and tag == "dd":
            self._mode = "dd"
            return

        if self._mode is None:
            return

        # Pick up option name from the first <a id="opt-...">.
        if tag == "a" and self._mode == "dt" and self._anchor_name is None:
            aid = a.get("id", "")
            if aid.startswith("opt-"):
                self._anchor_name = aid[4:]

        # Re-emit start tag into the right buffer.
        attr_str = "".join(f' {k}="{html_module.escape(v or "", quote=True)}"' for k, v in attrs)
        chunk = f"<{tag}{attr_str}>"
        if tag in ("br", "hr", "img"):
            chunk = f"<{tag}{attr_str}/>"
        self._emit(chunk)

    def handle_endtag(self, tag):
        if not self._in_dl:
            return

        if tag == "dl":
            if self._inner_dl_depth > 0:
                # Inner dl close -- emit and stay in variablelist mode.
                self._inner_dl_depth -= 1
                self._emit("</dl>")
                return
            # Outer variablelist close.
            self._flush_pair()
            self._in_dl = False
            self._dl_depth = 0
            self._mode = None
            return

        if self._inner_dl_depth == 0 and self._mode == "dt" and tag == "dt":
            self._mode = None
            return
        if self._inner_dl_depth == 0 and self._mode == "dd" and tag == "dd":
            self._flush_pair()
            self._mode = None
            return

        if self._mode is None:
            return

        self._emit(f"</{tag}>")

    def handle_data(self, data):
        if self._mode is None:
            return
        self._emit(data)

    def handle_entityref(self, name):
        if self._mode is None:
            return
        self._emit(f"&{name};")

    def handle_charref(self, name):
        if self._mode is None:
            return
        self._emit(f"&#{name};")

    def _emit(self, chunk: str):
        if self._mode == "dt":
            self._dt_html += chunk
        elif self._mode == "dd":
            self._dd_html += chunk

    def _flush_pair(self):
        if self._anchor_name and self._dd_html:
            self._options.append({
                "name": html_module.unescape(self._anchor_name),
                "dt_html": self._dt_html,
                "dd_html": self._dd_html,
            })
        self._anchor_name = None
        self._dt_html = ""
        self._dd_html = ""


def parse_options(html_text: str) -> list[dict]:
    p = OptionsParser()
    p.feed(html_text)
    return p.options()


def option_group_path(name: str) -> tuple[str, str]:
    """Map an option name to (group_dir, filename).

    services.nginx.foo  -> ("services",  "nginx.md")
    programs.git.signing -> ("programs", "git.md")
    boot.loader.grub.x   -> ("",         "boot.md")
    networking.x         -> ("",         "networking.md")
    """
    parts = name.split(".")
    top = parts[0] if parts else "misc"
    # Anchor-only weird names like "_imports_=___pkgs.ghostunnel..." -> bucket
    # them under "_imports".
    if top.startswith("_"):
        top = "_imports"
    if top in NESTED_OPTION_NAMESPACES and len(parts) >= 2:
        sub = slugify(parts[1])
        return top, f"{sub}.md"
    return "", f"{slugify(top)}.md"


def render_option_md(opt: dict) -> str:
    """Convert one option's <dt>+<dd> HTML to a markdown chunk."""
    # Render heading from the option name; the <dt> block has the formatted
    # name with code styling but the anchor name is canonical.
    name = opt["name"]
    body = html_to_markdown(opt["dd_html"], base_heading_level=3)
    lines = [f"## `{name}`", ""]
    if body.strip():
        lines.append(body.rstrip())
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# README builders
# ---------------------------------------------------------------------------

def build_top_readme(manual_index: dict, release_count: int, option_groups: list[str]) -> str:
    lines = ["# NixOS Manual (stable)", ""]
    lines.append(f"Mirror of [{BASE}]({BASE}). Each chapter / option group / "
                 "release note is stored as its own markdown file so that "
                 "incremental syncs report exactly what changed.")
    lines.append("")
    lines.append("## Sections")
    lines.append("")
    lines.append("- [Manual](./manual/) -- preface, installation, configuration, administration, development")
    lines.append(f"- [Release Notes](./release-notes/) -- {release_count} releases")
    lines.append(f"- [Options](./options/) -- {len(option_groups)} groups, including services/* and programs/*")
    lines.append("")
    return "\n".join(lines)


def build_manual_readme(manual_index: dict) -> str:
    lines = ["# NixOS Manual", ""]
    if manual_index.get("preface"):
        lines.append("- [Preface](./preface.md)")
    for pid, dirname in PART_DIRS.items():
        part = manual_index["parts"].get(pid)
        if part:
            n = len(part["chapters"])
            lines.append(f"- [{part['title']}](./{dirname}/) ({n} chapters)")
    for extra in manual_index.get("extras", []):
        slug = slugify(extra["id"])
        lines.append(f"- [{extra['title']}](./{slug}.md)")
    lines.append("")
    return "\n".join(lines)


def build_part_readme(part: dict, dirname: str) -> str:
    lines = [f"# {part['title']}", ""]
    for ch in part["chapters"]:
        lines.append(f"- [{ch['title']}](./{slugify(ch['id'])}.md)")
    lines.append("")
    return "\n".join(lines)


def build_release_readme(releases: list[dict]) -> str:
    lines = ["# NixOS Release Notes", ""]
    for r in releases:
        lines.append(f"- [{r['title']}](./{slugify(r['id'])}.md)")
    lines.append("")
    return "\n".join(lines)


def build_options_readme(groups: dict[str, dict[str, int]]) -> str:
    """groups: {dir_or_empty: {filename: option_count}}."""
    lines = ["# NixOS Configuration Options", ""]
    flat = sorted(groups.get("", {}).keys())
    if flat:
        lines.append("## Top-level namespaces")
        lines.append("")
        for fn in flat:
            n = groups[""][fn]
            lines.append(f"- [{fn[:-3]}](./{fn}) ({n} options)")
        lines.append("")
    for dirname in sorted(k for k in groups if k):
        lines.append(f"## {dirname}")
        lines.append("")
        for fn in sorted(groups[dirname]):
            n = groups[dirname][fn]
            lines.append(f"- [{dirname}.{fn[:-3]}](./{dirname}/{fn}) ({n} options)")
        lines.append("")
    return "\n".join(lines)


def build_options_subdir_readme(dirname: str, files: dict[str, int]) -> str:
    lines = [f"# {dirname.title()} options", ""]
    for fn in sorted(files):
        lines.append(f"- [{dirname}.{fn[:-3]}](./{fn}) ({files[fn]} options)")
    lines.append("")
    return "\n".join(lines)


def build_chapter_md(chunk: dict, source_url: str) -> str:
    body = html_to_markdown(chunk["html"], base_heading_level=1).rstrip()
    # If the renderer didn't emit any heading (e.g. the source structure was
    # unusual), fall back to a synthesized one. Otherwise prepend the source
    # link below the rendered top-level heading.
    src_line = f"*Source: [{source_url}#{chunk['id']}]({source_url}#{chunk['id']})*"
    if body.startswith("# "):
        first_nl = body.find("\n")
        if first_nl == -1:
            first_nl = len(body)
        return f"{body[:first_nl]}\n\n{src_line}\n\n{body[first_nl:].lstrip()}\n"
    return f"# {chunk['title']}\n\n{src_line}\n\n{body}\n"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()
    new_cache: dict[str, dict] = {}

    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    unchanged = 0

    def commit(rel_path: str, content: str, label_for_log: str) -> None:
        nonlocal unchanged
        path = os.path.join(DOCS_DIR, rel_path)
        digest = sha256(content)
        prev = cache.get(rel_path, {})
        if prev.get("sha256") == digest and os.path.exists(path):
            unchanged += 1
            new_cache[rel_path] = prev
            return
        is_new = rel_path not in cache or not os.path.exists(path)
        write_file(path, content, dry_run=args.dry_run, verbose=args.verbose,
                   label="ADD" if is_new else "UPDATE")
        new_cache[rel_path] = {
            "sha256": digest,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "label": label_for_log,
        }
        (added if is_new else updated).append(f"{label_for_log} ({rel_path})")

    # --- Fetch all three pages -----------------------------------------
    print("Fetching pages concurrently...")
    source_results: dict[str, tuple[str | None, str | None, bool]] = {}
    with ThreadPoolExecutor(max_workers=len(PAGES)) as pool:
        futures = {}
        for slug, url in PAGES.items():
            source_key = f"{SOURCE_CACHE_PREFIX}{slug}"
            etag = cache.get(source_key, {}).get("etag")
            print(f"  GET {url}" + (" (conditional)" if etag else ""))
            futures[pool.submit(fetch_url, url, 180, etag)] = slug
        for future in as_completed(futures):
            slug = futures[future]
            source_results[slug] = future.result()

    if any(text is None and not unchanged_source
           for text, _, unchanged_source in source_results.values()):
        print("ERROR: one or more source pages could not be fetched",
              file=sys.stderr)
        sys.exit(1)

    output_entries = {
        key: value for key, value in cache.items()
        if not key.startswith(SOURCE_CACHE_PREFIX)
    }
    outputs_complete = (
        bool(output_entries)
        and all(os.path.exists(os.path.join(DOCS_DIR, key))
                for key in output_entries)
    )
    if (
        len(source_results) == len(PAGES)
        and all(result[2] for result in source_results.values())
        and outputs_complete
    ):
        print("  All source ETags unchanged; skipping download and conversion")
        print("\nSync complete:")
        print("  Added:     0")
        print("  Updated:   0")
        print(f"  Unchanged: {len(output_entries)}")
        print("  Removed:   0")
        return

    # If one source changed, fetch bodies for the 304 sources too so the
    # cross-source indexes can be rebuilt consistently. Also repairs a
    # missing generated file when all sources returned 304.
    missing_bodies = [
        slug for slug, (text, _, _) in source_results.items() if text is None
    ]
    if missing_bodies:
        print(f"  Refetching {len(missing_bodies)} unchanged source bodies "
              "for a complete rebuild...")
        with ThreadPoolExecutor(max_workers=len(missing_bodies)) as pool:
            futures = {
                pool.submit(fetch_url, PAGES[slug]): slug
                for slug in missing_bodies
            }
            for future in as_completed(futures):
                slug = futures[future]
                source_results[slug] = future.result()

    pages: dict[str, str] = {}
    for slug, url in PAGES.items():
        text, etag, _ = source_results[slug]
        if text is None:
            print(f"ERROR: could not fetch {url}", file=sys.stderr)
            sys.exit(1)
        pages[slug] = text
        new_cache[f"{SOURCE_CACHE_PREFIX}{slug}"] = {
            "etag": etag or "",
            "url": url,
        }
        print(f"    {slug}: {len(text):,} bytes")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    # --- Slice the main manual -----------------------------------------
    print("\nSlicing main manual...")
    manual = slice_manual(pages["manual"])
    n_chapters = sum(len(p["chapters"]) for p in manual["parts"].values())
    print(f"  preface: {1 if manual['preface'] else 0}")
    print(f"  parts:   {len(manual['parts'])}")
    print(f"  chapters: {n_chapters}")
    print(f"  extras (top-level chapters outside parts): {len(manual['extras'])}")

    # Preface.
    if manual["preface"]:
        md = build_chapter_md(manual["preface"], PAGES["manual"])
        commit("manual/preface.md", md, manual["preface"]["title"] or "Preface")

    # Parts and chapters.
    for pid, dirname in PART_DIRS.items():
        part = manual["parts"].get(pid)
        if not part:
            continue
        for ch in part["chapters"]:
            slug = slugify(ch["id"])
            rel = f"manual/{dirname}/{slug}.md"
            md = build_chapter_md(ch, PAGES["manual"])
            commit(rel, md, f"{part['title']} / {ch['title']}")
        commit(f"manual/{dirname}/README.md", build_part_readme(part, dirname),
               f"{part['title']} index")

    # Extras (Contributing chapter, etc).
    for ex in manual["extras"]:
        slug = slugify(ex["id"])
        md = build_chapter_md(ex, PAGES["manual"])
        commit(f"manual/{slug}.md", md, ex["title"])

    commit("manual/README.md", build_manual_readme(manual), "Manual index")

    # --- Release notes -------------------------------------------------
    print("\nSlicing release notes...")
    releases = slice_release_notes(pages["release-notes"])
    print(f"  releases: {len(releases)}")
    for r in releases:
        slug = slugify(r["id"])
        md = build_chapter_md(r, PAGES["release-notes"])
        commit(f"release-notes/{slug}.md", md, r["title"])
    commit("release-notes/README.md", build_release_readme(releases),
           "Release notes index")

    # --- Options -------------------------------------------------------
    print("\nParsing options page...")
    opts = parse_options(pages["options"])
    print(f"  options: {len(opts)}")

    # Group by (dir, filename).
    grouped: dict[tuple[str, str], list[dict]] = {}
    for o in opts:
        key = option_group_path(o["name"])
        grouped.setdefault(key, []).append(o)

    print(f"  groups: {len(grouped)}")

    # Render one markdown file per group.
    group_summary: dict[str, dict[str, int]] = {}
    for (dirname, fname), members in sorted(grouped.items()):
        rel = f"options/{fname}" if not dirname else f"options/{dirname}/{fname}"
        # Sort options inside a group lexicographically by name for stable diffs.
        members.sort(key=lambda m: m["name"])
        title_parts = [dirname, fname[:-3]] if dirname else [fname[:-3]]
        title = ".".join(p for p in title_parts if p)
        body_chunks: list[str] = []
        for o in members:
            body_chunks.append(render_option_md(o))
        body = "\n".join(body_chunks)
        md = f"# {title} options\n\n{body}".rstrip() + "\n"
        commit(rel, md, f"options.{title} ({len(members)})")
        group_summary.setdefault(dirname, {})[fname] = len(members)

    # Per-subdir READMEs.
    for dirname, files in group_summary.items():
        if dirname:
            commit(f"options/{dirname}/README.md",
                   build_options_subdir_readme(dirname, files),
                   f"options.{dirname} index")

    commit("options/README.md", build_options_readme(group_summary),
           "Options index")

    # --- Top-level README ---------------------------------------------
    n_groups = sum(len(g) for g in group_summary.values())
    commit("README.md", build_top_readme(manual, len(releases), [str(i) for i in range(n_groups)]),
           "Top-level index")

    # --- Detect removals -----------------------------------------------
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        if old_key.startswith(SOURCE_CACHE_PREFIX):
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
        label = cache.get(old_key, {}).get("label", "")
        removed.append(f"{label} ({old_key})" if label else old_key)

    # Prune empty directories.
    if not args.dry_run and os.path.isdir(DOCS_DIR):
        for root, _, _ in os.walk(DOCS_DIR, topdown=False):
            if root == DOCS_DIR:
                continue
            if not os.listdir(root):
                os.rmdir(root)
                if args.verbose:
                    print(f"  RMDIR {os.path.relpath(root, DOCS_DIR)}/")

    if not args.dry_run:
        save_cache(new_cache)

    # --- Report --------------------------------------------------------
    print("\nSync complete:")
    print(f"  Added:     {len(added)}")
    print(f"  Updated:   {len(updated)}")
    print(f"  Unchanged: {unchanged}")
    print(f"  Removed:   {len(removed)}")

    def _print(label: str, items: list[str]):
        if not items:
            return
        print(f"\n{label}:")
        for it in sorted(items, key=str.lower):
            print(f"  - {it}")

    _print("Added", added)
    _print("Updated", updated)
    _print("Removed", removed)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch the NixOS stable manual and mirror it as local markdown"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate everything, ignoring cache")
    parser.add_argument("--verbose", action="store_true",
                        help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
