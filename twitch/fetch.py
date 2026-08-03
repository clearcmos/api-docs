#!/usr/bin/env python3

"""
Twitch API Documentation Fetcher

dev.twitch.tv is a Jekyll static site with no machine-readable spec, no
llms.txt, and an access-denied sitemap.xml. The official twitchdev GitHub org
publishes no OpenAPI spec either, so this fetcher scrapes the rendered HTML,
which is clean Jekyll/Rouge output.

Two page shapes exist under /docs/api/:

  Guide pages   -- content in <section class="text-content">. Discovered by
                   crawling /docs/api/* links breadth-first starting from the
                   landing page (the sidebar misses /docs/api/moderation, so
                   crawling is required; the sidebar is still parsed to order
                   the guide list in the README).
  Reference     -- /docs/api/reference is one ~1.4 MB page holding every
                   endpoint as a <section class="doc-content"> block with a
                   <section class="left-docs"> (docs: h2 title with the stable
                   anchor id, h3 subsections, parameter/response tables) and a
                   <section class="right-code"> (example requests/responses).
                   The first doc-content block is an index table mapping each
                   endpoint anchor to its Resource group (Ads, Bits, Chat,
                   ...); that mapping drives the per-resource directories.

Conversion notes (MarkdownConverter):
  - Rouge code blocks come in two flavors: <div class="language-X
    highlighter-rouge"><div class="highlight"><pre> (lang on the outer div)
    and <figure class="highlight"><pre><code class="language-X"
    data-lang="X"> (lang on the code tag). Syntax-highlight spans are
    stripped by taking text only inside <pre>.
  - Response-body tables encode field nesting with leading non-breaking
    spaces in the first cell; the leading \xa0 run is preserved so the
    hierarchy survives in markdown, while all other \xa0 become spaces.
  - Table cells may contain <ul>, <br>, and <p>; they are flattened onto one
    physical line with <br> separators (markdown cells cannot span lines).
  - CloudCannon editor links (<a class="editor-link" href="cloudcannon:...">)
    carry a pencil character and are skipped entirely.
  - <span class="pill ...">NEW</span> badges render as **NEW**.
  - <details>/<summary> (moderation page) render as a bold summary line
    followed by the unfolded content.
  - Zero-width spaces (which pollute a few headings) are stripped globally.
  - Links are rewritten: /docs/api/reference#anchor and bare #anchor go to
    the local per-endpoint file, /docs/api/* to the local page file, and
    everything else becomes an absolute https://dev.twitch.tv URL.

Output layout:
  docs/
    README.md             (catalogue: guides + reference summary)
    index.md              (the /docs/api/ landing page)
    get-started.md  guide.md  moderation.md  ...
    reference/
      README.md           (full endpoint index grouped by resource)
      ads/
        README.md         (resource index)
        start-commercial.md   (named by the endpoint's stable anchor)
        ...
      bits/  channels/  chat/  ...
"""

import argparse
import gzip
import hashlib
import html.parser
import json
import os
import posixpath
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin
from urllib.request import Request, urlopen

BASE_URL = "https://dev.twitch.tv"
API_ROOT = "/docs/api"  # crawl prefix, normalized (no trailing /)
REFERENCE_PATH = "/docs/api/reference"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
USER_AGENT = "twitch-api-docs-fetcher/1.0"


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Encoding": "gzip",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            data: bytes = resp.read()
            if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                data = gzip.decompress(data)
            return data.decode("utf-8", errors="replace")
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
            data = json.load(f)
            return data if isinstance(data, dict) else {}
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def clean_text(text: str) -> str:
    text = text.replace("\u200b", "").replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def strip_tags(fragment: str) -> str:
    """Plain text from an HTML fragment; pill badges become **bold**."""
    fragment = re.sub(r'<span class="pill[^"]*">([^<]*)</span>', r"**\1**", fragment)
    fragment = re.sub(r"<[^>]+>", " ", fragment)
    return clean_text(html.unescape(fragment))


def quote_url(url: str) -> str:
    """Percent-encode characters that would break a markdown link target
    (the site has one malformed href containing a literal space)."""
    return quote(url, safe=":/?#[]@!$&'*+,;=%~._-")


def normalize_doc_path(href: str) -> str:
    path = href.split("#", 1)[0].split("?", 1)[0]
    return path.rstrip("/") or "/"


def page_source_url(path: str) -> str:
    return f"{BASE_URL}{path}/"


# ---------------------------------------------------------------------------
# HTML -> markdown converter
# ---------------------------------------------------------------------------

BLOCK_TAGS = {
    "p",
    "pre",
    "figure",
    "table",
    "ul",
    "ol",
    "hr",
    "blockquote",
    "div",
    "details",
    "summary",
    "h1",
    "h2",
    "h3",
    "h4",
    "h5",
    "h6",
}
SKIP_TAGS = {"script", "style", "noscript", "svg", "button", "iframe", "nav"}
INLINE_END_TAGS = {"strong", "b", "em", "i", "small", "a", "span"}


class MarkdownConverter(html.parser.HTMLParser):
    """Convert a Jekyll/Rouge HTML fragment to markdown block lines.

    rewrite_href maps every <a href> / <img src> to its final link target.
    heading_offset shifts heading levels (-1 turns the reference page's h2
    endpoint titles into per-file h1s and its h3 subsections into h2s).
    """

    def __init__(self, rewrite_href, heading_offset: int = 0):
        super().__init__(convert_charrefs=True)
        self.rewrite_href = rewrite_href
        self.heading_offset = heading_offset
        self.lines: list[str] = []

        self._line = ""  # pending inline text for current block
        self._skip_depth = 0  # >0 while inside SKIP_TAGS / editor links
        self._skip_tag: str | None = None
        self._bq_depth = 0  # blockquote nesting

        # Code-block state.
        self._pre_depth = 0
        self._code_buf = ""
        self._code_lang = ""
        self._pending_lang = ""  # from <div class="language-X highlighter-rouge">

        # List state: stack of [kind, counter]; kind is "ul" or "ol".
        self._list_stack: list[list] = []

        # Heading state.
        self._heading_level = 0
        self._heading_buf = ""

        # Inline emphasis: stack of closing markers, popped on end tags.
        self._inline_stack: list[str] = []
        self._code_inline_depth = 0  # inside inline <code>

        # Table state.
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._table_header: list[str] | None = None
        self._row: list[str] = []
        self._cell = ""

    # -- block emission -------------------------------------------------------

    def _emit(self, text: str):
        if self._bq_depth:
            prefix = "> " * self._bq_depth
            text = "\n".join((prefix + line).rstrip() for line in text.split("\n"))
        self.lines.append(text)
        self.lines.append("")

    def _flush_line(self):
        parts = [re.sub(r"[ \t]+", " ", p).strip() for p in self._line.split("\n")]
        self._line = ""
        while parts and parts[0] == "":
            parts.pop(0)
        while parts and parts[-1] == "":
            parts.pop()
        if parts:
            self._emit(re.sub(r"\n{3,}", "\n\n", "\n".join(parts)))

    # -- start tags -------------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if self._skip_depth > 0:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "")

        if tag in SKIP_TAGS or (
            tag == "a" and ("editor-link" in cls or a.get("href", "").startswith("cloudcannon:"))
        ):
            self._skip_depth = 1
            self._skip_tag = tag
            return

        if self._pre_depth > 0:
            # Inside a code block only text matters, except nested structure
            # bookkeeping and the language hint on <figure>-style blocks.
            if tag == "pre":
                self._pre_depth += 1
            elif tag == "code":
                lang = a.get("data-lang", "")
                if not lang:
                    m = re.search(r"language-([\w+.-]+)", cls)
                    lang = m.group(1) if m else ""
                if lang:
                    self._code_lang = lang
            elif tag == "br":
                self._code_buf += "\n"
            return

        if tag == "div":
            m = re.search(r"language-([\w+.-]+)", cls)
            if m:
                self._pending_lang = m.group(1)

        if tag in BLOCK_TAGS and not self._in_table and not self._heading_level:
            self._flush_line()

        if tag == "blockquote":
            self._bq_depth += 1
            return
        if tag == "hr":
            self._emit("---")
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_level = int(tag[1])
            self._heading_buf = ""
            return
        if self._heading_level:
            return  # ignore markup inside headings (titles are plain text)

        if tag == "pre":
            self._pre_depth = 1
            self._code_buf = ""
            self._code_lang = self._pending_lang
            return

        if tag == "code":
            self._code_inline_depth += 1
            self._append("`")
            return

        if tag in ("strong", "b", "em", "i", "span"):
            if tag == "span":
                marker = "**" if "pill" in cls.split() else ""
            else:
                marker = "**" if tag in ("strong", "b") else "*"
            if self._code_inline_depth > 0:
                marker = ""  # no emphasis markers inside inline code
            self._append(marker)
            self._inline_stack.append(marker)
            return
        if tag == "small":
            self._append("")
            self._inline_stack.append("")
            return

        if tag == "a":
            href = a.get("href", "")
            target = self.rewrite_href(href) if href else ""
            if target and self._code_inline_depth == 0:
                self._append("[")
                self._inline_stack.append(f"]({target})")
            else:
                self._inline_stack.append("")
            return

        if tag == "br":
            if self._in_table:
                self._cell += "<br>"
            elif self._list_stack:
                self._append(" ")
            else:
                self._append("\n")
            return

        if tag == "p" and self._in_table:
            if self._cell.strip():
                self._cell += "<br>"
            return

        if tag in ("ul", "ol"):
            if not self._in_table:
                self._list_stack.append([tag, 0])
            return

        if tag == "li":
            if self._in_table:
                self._cell += "<br>- "
            else:
                self._start_list_item()
            return

        if tag == "table":
            self._in_table = True
            self._table_rows = []
            self._table_header = None
            self._row = []
            return
        if tag == "tr" and self._in_table:
            self._commit_row()
            return
        if tag in ("td", "th") and self._in_table:
            self._cell = ""
            return

        if tag == "img":
            src = a.get("src", "")
            if not src:
                return
            target = self.rewrite_href(src)
            text = f"![{clean_text(a.get('alt', ''))}]({target})"
            if self._in_table:
                self._cell += text
            else:
                self._flush_line()
                self._emit(text)
            return

    # -- end tags ---------------------------------------------------------------

    def handle_endtag(self, tag):
        if self._skip_depth > 0:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return

        if self._pre_depth > 0:
            if tag == "pre":
                self._pre_depth -= 1
                if self._pre_depth == 0:
                    self._emit_code_block()
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._finish_heading()
            return
        if self._heading_level:
            return

        if tag == "code":
            if self._code_inline_depth > 0:
                self._append("`")
                self._code_inline_depth -= 1
            return

        if tag in INLINE_END_TAGS:
            if self._inline_stack:
                self._append(self._inline_stack.pop())
            return

        if tag in ("ul", "ol"):
            if not self._in_table and self._list_stack:
                self._list_stack.pop()
                if not self._list_stack:
                    self.lines.append("")
            return

        if tag == "li" and not self._in_table:
            self._flush_list_item()
            return

        if tag in ("td", "th") and self._in_table:
            self._row.append(self._render_cell(self._cell))
            self._cell = ""
            return
        if tag == "tr" and self._in_table:
            self._commit_row()
            return
        if tag == "table" and self._in_table:
            self._commit_row()
            self._emit_table()
            self._in_table = False
            return

        if tag == "blockquote":
            self._flush_line()
            self._bq_depth = max(0, self._bq_depth - 1)
            return

        if tag in ("p", "summary", "figure", "div", "details") and not self._in_table:
            self._flush_line()

    def handle_data(self, data):
        if self._skip_depth > 0:
            return
        data = data.replace("\u200b", "")
        if self._pre_depth > 0:
            self._code_buf += data
            return
        if self._heading_level:
            self._heading_buf += data
            return
        if self._in_table:
            self._cell += data.replace("\n", " ")  # keep \xa0 (cell nesting)
            return
        self._append(data.replace("\xa0", " ").replace("\n", " "))

    def _append(self, text: str):
        if self._in_table:
            self._cell += text
        else:
            self._line += text

    # -- headings -----------------------------------------------------------------

    def _finish_heading(self):
        level = self._heading_level
        self._heading_level = 0
        title = clean_text(self._heading_buf)
        self._heading_buf = ""
        if not title:
            return
        out = max(1, min(6, level + self.heading_offset))
        self._emit("#" * out + " " + title)

    # -- lists ----------------------------------------------------------------------

    def _start_list_item(self):
        self._flush_line()
        depth = max(0, len(self._list_stack) - 1)
        kind = str(self._list_stack[-1][0]) if self._list_stack else "ul"
        counter = int(self._list_stack[-1][1]) if self._list_stack else 0
        if kind == "ol":
            counter += 1
            self._list_stack[-1][1] = counter
            self._line = f"{'  ' * depth}{counter}. "
        else:
            self._line = f"{'  ' * depth}- "

    def _flush_list_item(self):
        line = re.sub(r"[ \t]+", " ", self._line.replace("\n", " ")).rstrip()
        self._line = ""
        if line.strip(" -0123456789."):
            if self._bq_depth:
                line = "> " * self._bq_depth + line
            self.lines.append(line)

    # -- code blocks -------------------------------------------------------------------

    def _emit_code_block(self):
        code = self._code_buf.strip("\n")
        self._code_buf = ""
        lang = self._code_lang
        self._code_lang = ""
        self._pending_lang = ""
        if not code.strip():
            return
        fence = "```"
        while fence in code:
            fence += "`"
        self._emit("\n".join([fence + lang, *code.split("\n"), fence]))

    # -- tables ------------------------------------------------------------------------

    def _commit_row(self):
        if self._row:
            if self._table_header is None:
                self._table_header = self._row
            else:
                self._table_rows.append(self._row)
        self._row = []

    def _render_cell(self, cell: str) -> str:
        # Leading non-breaking spaces encode response-field nesting; keep them.
        indent = ""
        lead = re.match(r"^[\xa0 ]+", cell)
        if lead:
            indent = "\xa0" * lead.group(0).count("\xa0")
            cell = cell[lead.end() :]
        cell = cell.replace("\xa0", " ").replace("\n", " ")
        cell = re.sub(r"(\s*<br>\s*)+", "<br>", cell)
        cell = re.sub(r"[ \t]+", " ", cell).strip()
        cell = re.sub(r"^(<br>)+|(<br>)+$", "", cell)
        return (indent + cell).replace("|", "\\|")

    def _emit_table(self):
        header = self._table_header or []
        rows = self._table_rows
        if not header and rows:
            header, rows = rows[0], rows[1:]
        if not header:
            return
        ncol = max(len(header), *(len(r) for r in rows)) if rows else len(header)
        header = header + [""] * (ncol - len(header))
        out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * ncol) + "|"]
        for r in rows:
            r = r + [""] * (ncol - len(r))
            out.append("| " + " | ".join(r) + " |")
        self._emit("\n".join(out))

    # -- result -------------------------------------------------------------------------

    def markdown(self) -> str:
        self._flush_line()
        if self._in_table:
            self._commit_row()
            self._emit_table()
            self._in_table = False
        out: list[str] = []
        for line in self.lines:
            if line == "" and (not out or out[-1] == ""):
                continue
            out.append(line)
        return "\n".join(out).strip()


# ---------------------------------------------------------------------------
# Link rewriting
# ---------------------------------------------------------------------------


def make_rewriter(
    cur_rel: str, cur_page_path: str, page_files: dict, endpoint_files: dict, on_reference: bool
):
    """Build the href-rewriting callable for one output file.

    cur_rel        -- output path relative to docs/ (posix), e.g. "guide.md"
    cur_page_path  -- normalized source page path, e.g. "/docs/api/guide"
    page_files     -- {normalized page path: output rel path}
    endpoint_files -- {endpoint anchor: output rel path}
    on_reference   -- True while converting reference-page content, where bare
                      #anchors refer to endpoint sections
    """
    cur_dir = posixpath.dirname(cur_rel)

    def rel(target: str, frag: str = "") -> str:
        path = posixpath.relpath(target, cur_dir) if cur_dir else target
        return f"{path}#{frag}" if frag else path

    def rewrite(href: str) -> str:
        href = href.strip()
        if not href:
            return ""
        if href.startswith("#"):
            frag = href[1:]
            if frag in endpoint_files:
                return rel(endpoint_files[frag])
            if on_reference:
                # Anchor on the reference page that is not a known endpoint.
                return quote_url(f"{BASE_URL}{REFERENCE_PATH}#{frag}")
            return href  # same-page heading anchor
        if href.startswith("//"):
            return quote_url("https:" + href)
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", href):
            return quote_url(href)  # http(s), mailto, ...
        if href.startswith("/"):
            path = normalize_doc_path(href)
            frag = href.partition("#")[2]
            if path in page_files:
                if frag and frag in endpoint_files:
                    return rel(endpoint_files[frag])
                if path == REFERENCE_PATH and frag:
                    return rel(page_files[path])  # unknown endpoint anchor
                return rel(page_files[path], frag)
            return quote_url(BASE_URL + href)
        # Rare relative href: resolve against the current page URL.
        return quote_url(urljoin(page_source_url(cur_page_path), href))

    return rewrite


# ---------------------------------------------------------------------------
# Page discovery and extraction
# ---------------------------------------------------------------------------


def main_content(page_html: str) -> str:
    start = page_html.find('<div class="main"')
    if start < 0:
        return page_html
    end = page_html.find("<footer", start)
    return page_html[start : end if end > start else len(page_html)]


def extract_api_links(fragment: str) -> set[str]:
    links = set()
    for href in re.findall(r'href="(/docs/api[^"]*)"', fragment):
        path = normalize_doc_path(href)
        if path == API_ROOT or path.startswith(API_ROOT + "/"):
            links.add(path)
    return links


def crawl_pages(verbose: bool) -> dict[str, str]:
    """BFS within the /docs/api/ prefix; returns {normalized path: html}."""
    pages: dict[str, str] = {}
    frontier = [API_ROOT]
    while frontier:
        batch = [p for p in dict.fromkeys(frontier) if p not in pages]
        frontier = []
        if not batch:
            break
        with ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(lambda p: (p, fetch_url(page_source_url(p))), batch))
        for path, text in results:
            if text is None:
                print(f"ERROR: aborting, page failed to fetch: {path}", file=sys.stderr)
                sys.exit(1)
            if verbose:
                print(f"  fetched {path} ({len(text)} bytes)")
            pages[path] = text
            frontier.extend(extract_api_links(main_content(text)) - pages.keys())
    return pages


def parse_sidebar(landing_html: str) -> list[tuple[str, str]]:
    """Ordered (path, label) for the Twitch API sidebar sub-pages."""
    m = re.search(r'<dt>\s*<a href="/docs/api/"[^>]*>Twitch API</a>\s*</dt>(.*?)</dl>', landing_html, re.S)
    if not m:
        return []
    out: list[tuple[str, str]] = []
    seen = set()
    for href, label in re.findall(r'<a class="sub-page" href="([^"#]+)"[^>]*>([^<]+)</a>', m.group(1)):
        path = normalize_doc_path(href)
        if (path == API_ROOT or path.startswith(API_ROOT + "/")) and path not in seen:
            seen.add(path)
            out.append((path, clean_text(label)))
    return out


def extract_text_content(page_html: str) -> str | None:
    start = page_html.find('<section class="text-content')
    if start < 0:
        return None
    start = page_html.find(">", start) + 1
    end = page_html.find("</section>", start)
    return page_html[start:end]


def page_title(fragment: str, page_html: str) -> str:
    m = re.search(r"<h1[^>]*>(.*?)</h1>", fragment, re.S)
    if m:
        return strip_tags(m.group(1))
    m = re.search(r"<title>([^<]*)</title>", page_html)
    return clean_text((m.group(1) if m else "").split("|")[0]) or "Untitled"


def page_slug(path: str) -> str:
    relative = path[len(API_ROOT) :].strip("/")
    if not relative:
        return "index"
    return "/".join(sanitize_filename(seg) or "page" for seg in relative.split("/"))


# ---------------------------------------------------------------------------
# Reference page parsing
# ---------------------------------------------------------------------------


def parse_reference(page_html: str):
    """Returns (index_rows, endpoints).

    index_rows -- ordered [(resource, anchor, title, description)] from the
                  index table at the top of the reference page
    endpoints  -- ordered [{anchor, title, left_html, right_html}]
    """
    chunks = page_html.split('<section class="doc-content">')
    if len(chunks) < 3:
        print("ERROR: reference page structure changed (no doc-content sections)", file=sys.stderr)
        sys.exit(1)

    index_rows = []
    for res, anchor, title, desc in re.findall(
        r'<td>([^<]*)</td>\s*<td><a href="#([^"]+)">([^<]+)</a></td>\s*<td>(.*?)</td>', chunks[1], re.S
    ):
        index_rows.append((clean_text(res), anchor, clean_text(title), strip_tags(desc)))

    endpoints = []
    for chunk in chunks[2:]:
        m = re.search(r'<h2 id="([^"]+)"[^>]*>(.*?)</h2>', chunk, re.S)
        if not m:
            continue
        left_start = chunk.find('<section class="left-docs">')
        left_end = chunk.find("</section>", left_start)
        left_html = chunk[left_start:left_end] if left_start >= 0 else ""
        h2_end = left_html.find("</h2>")
        if h2_end >= 0:
            left_html = left_html[h2_end + len("</h2>") :]
        right_start = chunk.find('<section class="right-code">')
        right_html = ""
        if right_start >= 0:
            right_end = chunk.find("</section>", right_start)
            right_html = chunk[chunk.find(">", right_start) + 1 : right_end]
        endpoints.append(
            {
                "anchor": m.group(1),
                "title": strip_tags(m.group(2)),
                "left_html": left_html,
                "right_html": right_html,
            }
        )
    return index_rows, endpoints


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------


def convert_fragment(fragment: str, rewriter, heading_offset: int = 0) -> str:
    conv = MarkdownConverter(rewriter, heading_offset)
    conv.feed(fragment)
    conv.close()
    return conv.markdown()


def build_guide_page(
    path: str, fragment: str, title: str, cur_rel: str, page_files: dict, endpoint_files: dict
) -> str:
    # Drop the page's own <h1>; it becomes the file title below.
    m = re.search(r"<h1[^>]*>.*?</h1>", fragment, re.S)
    if m:
        fragment = fragment[m.end() :]
    rewriter = make_rewriter(cur_rel, path, page_files, endpoint_files, False)
    body = convert_fragment(fragment, rewriter)
    src = page_source_url(path)
    parts = [f"# {title}\n", f"Source: [{src}]({src})\n"]
    if body:
        parts.append(body)
    return "\n".join(parts).rstrip() + "\n"


def build_endpoint_page(ep: dict, resource: str, cur_rel: str, page_files: dict, endpoint_files: dict) -> str:
    rewriter = make_rewriter(cur_rel, REFERENCE_PATH, page_files, endpoint_files, True)
    body = convert_fragment(ep["left_html"], rewriter, heading_offset=-1)
    if ep["right_html"]:
        examples = convert_fragment(ep["right_html"], rewriter, heading_offset=-1)
        if examples:
            body = f"{body}\n\n{examples}" if body else examples
    src = f"{BASE_URL}{REFERENCE_PATH}#{ep['anchor']}"
    parts = [f"# {ep['title']}\n", f"**Resource:** {resource}\n", f"Source: [{src}]({src})\n"]
    if body:
        parts.append(body)
    return "\n".join(parts).rstrip() + "\n"


def build_resource_readme(resource: str, entries: list[dict]) -> str:
    src = f"{BASE_URL}{REFERENCE_PATH}/"
    lines = [
        f"# {resource}\n",
        f"Twitch API Reference: {resource} endpoints.\n",
        f"Source: [{src}]({src})\n",
        "## Endpoints\n",
    ]
    for e in entries:
        desc = f": {e['description']}" if e["description"] else ""
        lines.append(f"- [{e['title']}](./{e['filename']}){desc}")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_reference_readme(resources: list[tuple[str, str, list[dict]]]) -> str:
    src = f"{BASE_URL}{REFERENCE_PATH}/"
    total = sum(len(entries) for _, _, entries in resources)
    lines = [
        "# Twitch API Reference\n",
        "Helix endpoints, served under `https://api.twitch.tv/helix/`. "
        "Each endpoint's page lists its required OAuth authorization, URL, "
        "parameters, response body, response codes, and examples.\n",
        f"Source: [{src}]({src})\n",
        f"**Endpoints documented:** {total}\n",
        "## Resources\n",
    ]
    for resource, slug, entries in resources:
        lines.append(f"- [{resource}](./{slug}/README.md) ({len(entries)} endpoints)")
    lines.append("")
    for resource, slug, entries in resources:
        lines.append(f"### {resource}\n")
        for e in entries:
            desc = f": {e['description']}" if e["description"] else ""
            lines.append(f"- [{e['title']}](./{slug}/{e['filename']}){desc}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_top_readme(guides: list[dict], resources: list[tuple[str, str, list[dict]]]) -> str:
    src = page_source_url(API_ROOT)
    total = sum(len(entries) for _, _, entries in resources)
    lines = [
        "# Twitch API Documentation\n",
        "Documentation for the Twitch API (Helix): guides plus the full "
        "endpoint reference, scraped from dev.twitch.tv.\n",
        f"Source: [{src}]({src})\n",
        "## Guides\n",
    ]
    for g in guides:
        lines.append(f"- [{g['title']}](./{g['filename']})")
    lines.append("")
    lines.append("## Reference\n")
    lines.append(
        f"[Full endpoint index](./reference/README.md): {total} endpoints "
        f"across {len(resources)} resources.\n"
    )
    for resource, slug, entries in resources:
        lines.append(f"- [{resource}](./reference/{slug}/README.md) ({len(entries)} endpoints)")
    lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print(f"Crawling {page_source_url(API_ROOT)} ...")
    pages = crawl_pages(args.verbose)
    if REFERENCE_PATH not in pages:
        print("ERROR: reference page not discovered", file=sys.stderr)
        sys.exit(1)

    index_rows, endpoints = parse_reference(pages[REFERENCE_PATH])
    print(f"  Pages: {len(pages)} (guides: {len(pages) - 1})")
    print(
        f"  Endpoints: {len(endpoints)} across {len(dict.fromkeys(r for r, _, _, _ in index_rows))} resources"
    )
    if len(endpoints) < 100:
        print("ERROR: suspiciously few endpoints parsed -- page structure may have changed", file=sys.stderr)
        sys.exit(1)

    # Resource grouping, in index-table order.
    resources: list[tuple[str, str, list[dict]]] = []
    by_resource: dict[str, list[dict]] = {}
    res_slugs: dict[str, str] = {}
    for res, _anchor, _title, _desc in index_rows:
        if res not in by_resource:
            by_resource[res] = []
            res_slugs[res] = sanitize_filename(res) or "other"
            resources.append((res, res_slugs[res], by_resource[res]))
    endpoint_files: dict[str, str] = {}
    ep_by_anchor = {ep["anchor"]: ep for ep in endpoints}
    for res, anchor, title, desc in index_rows:
        ep = ep_by_anchor.get(anchor)
        if ep is None:
            continue
        filename = f"{sanitize_filename(anchor)}.md"
        rel = f"reference/{res_slugs[res]}/{filename}"
        endpoint_files[anchor] = rel
        by_resource[res].append(
            {
                "anchor": anchor,
                "title": ep["title"] or title,
                "description": desc,
                "filename": filename,
                "rel": rel,
            }
        )
    for ep in endpoints:  # endpoints missing from the index table, if any
        if ep["anchor"] not in endpoint_files:
            filename = f"{sanitize_filename(ep['anchor'])}.md"
            if "Other" not in by_resource:
                by_resource["Other"] = []
                res_slugs["Other"] = "other"
                resources.append(("Other", "other", by_resource["Other"]))
            rel = f"reference/other/{filename}"
            endpoint_files[ep["anchor"]] = rel
            by_resource["Other"].append(
                {
                    "anchor": ep["anchor"],
                    "title": ep["title"],
                    "description": "",
                    "filename": filename,
                    "rel": rel,
                }
            )

    # Guide pages: sidebar order first, crawl-only extras after.
    sidebar = parse_sidebar(pages[API_ROOT])
    guide_paths = [p for p, _ in sidebar if p != REFERENCE_PATH and p in pages]
    for path in sorted(pages):
        if path != REFERENCE_PATH and path not in guide_paths:
            guide_paths.append(path)

    page_files = {
        path: (f"{page_slug(path)}.md" if path != REFERENCE_PATH else "reference/README.md") for path in pages
    }

    guides: list[dict] = []
    for path in guide_paths:
        fragment = extract_text_content(pages[path])
        if fragment is None:
            print(f"WARNING: no text-content section on {path}, skipping", file=sys.stderr)
            continue
        guides.append(
            {
                "path": path,
                "filename": page_files[path],
                "title": page_title(fragment, pages[path]),
                "fragment": fragment,
            }
        )

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}

    def write_file(rel_path: str, content: str):
        nonlocal added, updated, unchanged
        content_hash = sha256(content)
        full_path = os.path.join(DOCS_DIR, rel_path)
        if cache.get(rel_path, {}).get("sha256") == content_hash and os.path.exists(full_path):
            unchanged += 1
            new_cache[rel_path] = cache[rel_path]
            return
        is_new = rel_path not in cache or not os.path.exists(full_path)
        action = "ADD" if is_new else "UPDATE"
        if args.dry_run:
            print(f"  {action} {rel_path}")
        else:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            if args.verbose:
                print(f"  {action} {rel_path}")
        new_cache[rel_path] = {
            "sha256": content_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    for g in guides:
        write_file(
            g["filename"],
            build_guide_page(g["path"], g["fragment"], g["title"], g["filename"], page_files, endpoint_files),
        )

    for resource, slug, entries in resources:
        write_file(f"reference/{slug}/README.md", build_resource_readme(resource, entries))
        for e in entries:
            ep = ep_by_anchor[e["anchor"]]
            write_file(e["rel"], build_endpoint_page(ep, resource, e["rel"], page_files, endpoint_files))

    write_file("reference/README.md", build_reference_readme(resources))
    write_file("README.md", build_top_readme(guides, resources))

    # Removal detection.
    removed = 0
    for old_key in sorted(cache):
        if old_key not in new_cache:
            old_path = os.path.join(DOCS_DIR, old_key)
            if os.path.exists(old_path):
                if args.dry_run:
                    print(f"  REMOVE {old_key}")
                else:
                    os.remove(old_path)
                    if args.verbose:
                        print(f"  REMOVE {old_key}")
                removed += 1

    # Prune empty directories (bottom-up) and persist the cache.
    if not args.dry_run:
        for root, dirs, files in os.walk(DOCS_DIR, topdown=False):
            if root != DOCS_DIR and not dirs and not files:
                os.rmdir(root)
                if args.verbose:
                    print(f"  RMDIR {os.path.relpath(root, DOCS_DIR)}/")
        save_cache(new_cache)

    print("\nSync complete:")
    print(f"  Added:      {added}")
    print(f"  Updated:    {updated}")
    print(f"  Unchanged:  {unchanged}")
    print(f"  Removed:    {removed}")
    print(f"  Total files: {added + updated + unchanged}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Twitch API docs from dev.twitch.tv and convert to markdown"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
