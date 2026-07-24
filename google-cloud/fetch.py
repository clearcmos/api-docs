#!/usr/bin/env python3

"""
Google Cloud documentation fetcher (docs.cloud.google.com).

Mirrors the entire Google Cloud documentation site to local markdown.

Discovery:
  * https://docs.cloud.google.com/sitemap.xml is an index of ~60 shard
    sitemaps. The shards return HTTP 500 unless the request advertises
    "Accept-Encoding: gzip" -- every request this fetcher makes sends it.
  * Shards list ~1.1M URLs including translations (?hl=xx). English-only
    is ~520k URLs. Each URL carries <lastmod>, which drives incremental
    sync: a page is refetched only if its lastmod changed, it is new, or
    its output file is missing.

Scale notes (this fetcher deviates from the smaller ones on purpose):
  * Fetching uses a process pool (--procs) where each process runs a
    thread pool (--threads). Each thread keeps a persistent HTTPS
    connection (keep-alive) to avoid per-request TLS handshakes.
  * --dry-run does not fetch page content at all; it prints the plan
    (how many pages would be fetched/removed and why). Fetching 500k
    pages to show a diff would defeat the point.
  * Only one top-level docs/README.md catalogue is generated (grouped
    by product with page counts). Per-directory READMEs would add tens
    of thousands of files for no benefit.
  * The cache stores {sha256, lastmod} per file, compact JSON (~50 MB).
    It is saved periodically during long runs so an interrupted run
    resumes where it left off.

Conversion: each page's <div class="devsite-article-body"> is converted
to markdown by an html.parser subclass (adapted from youtube/fetch.py).
The HTML is sliced to the article body before parsing, which skips ~85%
of each page (Devsite chrome). The page title comes from the JSON-LD
"headline" or the <title> tag, since the <h1> sits outside the body.

Default scope is every English page EXCEPT the per-class client-library
reference trees ({lang}/docs/reference/* for java/nodejs/ruby/python/
dotnet/php/cpp/go, ~410k machine-generated pages that mirror the same
API surface the REST reference already covers). That leaves ~120k pages:
all product/feature docs, the REST API reference, and the gcloud CLI
reference. Flags:
  --include-sdk-reference  also mirror the per-class SDK trees
  --include-translations   also mirror ?hl=xx pages (doubles the corpus)
  --only PREFIXES          comma-separated path prefixes, e.g.
                           --only compute,bigquery,run
  --limit N                cap the fetch list (testing)

Removal detection is scope-aware: it only deletes files whose URL left
the sitemap and that are inside the currently configured scope, so
out-of-scope files from a wider earlier run are left alone.

To stop a run early, use Ctrl-C (SIGINT reaches the whole process group
and the cache is saved for resume) or kill the process GROUP
(kill -- -PGID). Killing only the parent PID leaves the pool's worker
processes running -- they keep fetching and writing for minutes.
"""

import argparse
import gzip
import hashlib
import html as html_lib
import html.parser
import http.client
import json
import os
import re
import ssl
import sys
import threading
import time
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from urllib.parse import urlsplit

HOST = "docs.cloud.google.com"
SITE = f"https://{HOST}"
SITEMAP_INDEX = f"{SITE}/sitemap.xml"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

USER_AGENT = "google-cloud-docs-fetcher/1.0"

SDK_REFERENCE_RE = re.compile(
    r"^(?:java|nodejs|ruby|python|dotnet|php|cpp|go)/docs/reference(?:/|$)"
)

# Retry schedule (seconds) for 429/5xx and transport errors.
RETRY_DELAYS = (1, 3, 8, 20)


# ---------------------------------------------------------------------------
# HTTP with keep-alive
# ---------------------------------------------------------------------------

_tls = threading.local()
_SSL_CTX = ssl.create_default_context()


def _connection(fresh: bool = False):
    conn = getattr(_tls, "conn", None)
    if fresh and conn is not None:
        try:
            conn.close()
        except OSError:
            pass
        conn = None
    if conn is None:
        conn = http.client.HTTPSConnection(HOST, 443, timeout=90, context=_SSL_CTX)
        _tls.conn = conn
    return conn


def _read_body(resp) -> bytes:
    body = resp.read()
    if resp.getheader("Content-Encoding", "").lower() == "gzip":
        body = gzip.decompress(body)
    return body


def http_get(path: str, max_redirects: int = 4) -> tuple[int, bytes | None]:
    """GET a path from docs.cloud.google.com with keep-alive and gzip.

    Returns (status, body). Retries transport errors and 429/5xx with
    backoff. Follows same-host redirects. Returns (status, None) on
    permanent failure.
    """
    redirects = 0
    attempt = 0
    while True:
        conn = _connection(fresh=attempt > 0)
        try:
            conn.request("GET", path, headers={
                "User-Agent": USER_AGENT,
                "Accept-Encoding": "gzip",
                "Accept": "*/*",
            })
            resp = conn.getresponse()
            status = resp.status
            if status in (301, 302, 303, 307, 308):
                resp.read()  # drain for keep-alive
                loc = resp.getheader("Location", "")
                parts = urlsplit(loc)
                if parts.netloc and parts.netloc != HOST:
                    return status, None  # left the docs host; not our page
                target = parts.path or "/"
                if parts.query:
                    target += "?" + parts.query
                redirects += 1
                if redirects > max_redirects:
                    return status, None
                path = target
                continue
            body = _read_body(resp)
            if status == 200:
                return 200, body
            if status in (429, 500, 502, 503, 504) and attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                attempt += 1
                continue
            return status, None
        except (OSError, http.client.HTTPException, TimeoutError) as e:
            if attempt < len(RETRY_DELAYS):
                time.sleep(RETRY_DELAYS[attempt])
                attempt += 1
                continue
            print(f"ERROR: {path}: {e}", file=sys.stderr)
            return 0, None


# ---------------------------------------------------------------------------
# Sitemap discovery
# ---------------------------------------------------------------------------

URL_ENTRY_RE = re.compile(
    r"<loc>([^<]+)</loc>\s*(?:<lastmod>([^<]+)</lastmod>)?", re.S
)


def fetch_sitemap_urls(verbose: bool) -> tuple[dict[str, str | None], bool]:
    """Return ({path: lastmod}, complete) for every English page.

    `complete` is False if any shard failed, in which case removal
    detection is skipped.
    """
    status, body = http_get("/sitemap.xml")
    if status != 200 or body is None:
        print("ERROR: failed to fetch sitemap index", file=sys.stderr)
        sys.exit(1)
    index = body.decode("utf-8", errors="replace")
    shard_urls = re.findall(r"<loc>([^<]+)</loc>", index)
    print(f"Sitemap index lists {len(shard_urls)} shards")

    pages: dict[str, str | None] = {}
    complete = True

    def load_shard(shard_url: str) -> tuple[str, str | None]:
        path = urlsplit(shard_url).path
        st, data = http_get(path)
        if st != 200 or data is None:
            return shard_url, None
        return shard_url, data.decode("utf-8", errors="replace")

    with ThreadPoolExecutor(max_workers=8) as pool:
        for shard_url, xml in pool.map(load_shard, shard_urls):
            if xml is None or "<urlset" not in xml:
                print(f"WARNING: shard failed: {shard_url}", file=sys.stderr)
                complete = False
                continue
            n = 0
            for m in URL_ENTRY_RE.finditer(xml):
                loc, lastmod = m.group(1), m.group(2)
                parts = urlsplit(loc.strip())
                if parts.netloc != HOST:
                    continue
                pages[parts.path + (f"?{parts.query}" if parts.query else "")] = lastmod
                n += 1
            if verbose:
                print(f"  {shard_url}: {n} URLs")
    return pages, complete


def path_to_rel(path: str) -> str:
    """Map a URL path to an output file path relative to docs/."""
    p = path.lstrip("/").rstrip("/")
    if not p:
        p = "index"
    return p + ".md"


SITEMAP_SNAPSHOT = os.path.join(SCRIPT_DIR, ".sitemap-pages.json")


def plan_pages(args) -> tuple[list[tuple[str, str, str | None]], bool]:
    """Return ([(path, rel, lastmod)], sitemap_complete)."""
    if args.sitemap_cache and os.path.exists(SITEMAP_SNAPSHOT):
        with open(SITEMAP_SNAPSHOT) as f:
            pages = json.load(f)
        complete = True
        print(f"Loaded sitemap snapshot ({len(pages)} URLs)")
    else:
        pages, complete = fetch_sitemap_urls(args.verbose)
        if args.sitemap_cache and complete:
            with open(SITEMAP_SNAPSHOT, "w") as f:
                json.dump(pages, f, separators=(",", ":"))
    print(f"Sitemap URLs: {len(pages)}")

    only = None
    if args.only:
        only = tuple(p.strip().strip("/") for p in args.only.split(",") if p.strip())

    plan = []
    seen_rels = set()
    for path in sorted(pages):
        if "?" in path:
            if "hl=" in path and not args.include_translations:
                continue
            # Translations keep their query as part of the filename.
            base, query = path.split("?", 1)
            rel = path_to_rel(base)[:-3] + "." + query.replace("=", "-") + ".md"
        else:
            rel = path_to_rel(path)
        stripped = path.lstrip("/")
        if not args.include_sdk_reference and SDK_REFERENCE_RE.match(stripped):
            continue
        if only and not any(
            stripped == o or stripped.startswith(o + "/") for o in only
        ):
            continue
        if rel in seen_rels:
            continue
        seen_rels.add(rel)
        plan.append((path, rel, pages[path]))

    if args.limit:
        plan = plan[: args.limit]
    return plan, complete


# ---------------------------------------------------------------------------
# HTML to Markdown
# ---------------------------------------------------------------------------

BODY_START_RE = re.compile(r'<div class="devsite-article-body[^"]*"[^>]*>')
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)
HEADLINE_RE = re.compile(r'"headline":\s*"((?:[^"\\]|\\.)*)"')


class DevsiteExtractor(html.parser.HTMLParser):
    """Converts a Google Devsite article body to markdown.

    Must be fed HTML starting at the article-body <div> (see
    html_to_markdown, which slices the page first).
    """

    SKIP_TAGS = {
        "style", "script", "noscript", "devsite-iframe", "iframe",
        "devsite-toc", "devsite-content-footer", "devsite-thumb-rating",
        "devsite-feedback", "devsite-llm-tools", "devsite-actions",
        "button", "svg", "form", "devsite-feature-tooltip",
        "devsite-page-rating", "devsite-animation", "devsite-dialog",
        "video", "audio", "devsite-hats-survey", "devsite-nav-buttons",
        "label", "select", "devsite-select", "devsite-language-selector",
        "devsite-lightbox", "devsite-modal-dialog",
    }

    CHROME_DIV_CLASSES = {
        "devsite-article-meta",
        "devsite-banner",
        "devsite-collection-link",
        "devsite-key-takeaways-panel",
        "devsite-thumb-rating",
        "devsite-content-footer",
        "devsite-floating-action-buttons",
        "devsite-recommendations",
    }

    NOTE_CLASSES = {
        "note", "caution", "warning", "tip", "key-point", "key-term",
        "objective", "success", "beta", "special", "important",
        "deprecated", "dogfood", "preview", "experimental",
    }

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self._in_body = True
        self._body_depth = 0
        self._started = False
        self._skip_depth = 0
        self._skip_tag: str | None = None

        self._output: list[str] = []
        self._current_line = ""

        self._pre_depth = 0
        self._code_content = ""
        self._code_lang = ""

        self._list_stack: list[list] = []

        self._in_table = False
        self._table_rows: list[list[dict]] = []
        self._current_row: list[dict] = []
        self._current_cell: list[str] = []
        self._cell_is_header = False
        self._cell_colspan = 1
        self._in_thead = False
        self._cell_buffer_target: str | None = None

        self._notes: list[dict] = []
        self._tag_stack: list[dict] = []
        self._in_heading = 0
        self.title = ""

    # -- helpers ---------------------------------------------------------

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

    # -- handlers --------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        if not self._in_body:
            return
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "")

        if not self._started:
            # First tag is the article-body div itself.
            if tag == "div":
                self._started = True
                self._body_depth = 1
            return

        if tag == "div":
            self._body_depth += 1

        if self._skip_depth > 0:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        if tag == "div":
            classes = cls.split()
            if any(c in self.CHROME_DIV_CLASSES for c in classes):
                self._skip_depth = 1
                self._skip_tag = "div"
                return

        if tag == "span" and "material-icons" in cls.split():
            self._skip_depth = 1
            self._skip_tag = "span"
            return

        # In-page mini-TOC lists (REST reference pages).
        if tag == "ul" and "toc" in cls.split():
            self._skip_depth = 1
            self._skip_tag = "ul"
            return

        if tag in self.SKIP_TAGS:
            self._skip_depth = 1
            self._skip_tag = tag
            return

        if tag in ("aside", "div"):
            classes = set(cls.split())
            if classes & self.NOTE_CLASSES:
                self._flush_line()
                self._ensure_blank()
                self._notes.append({"body_depth": self._body_depth, "tag": tag})
                return

        if tag in ("devsite-code", "devsite-selector", "devsite-expandable",
                   "section", "devsite-tabs", "nav"):
            return

        if tag == "pre":
            self._flush_line()
            self._pre_depth += 1
            if self._pre_depth == 1:
                self._code_content = ""
                self._code_lang = (a.get("syntax") or a.get("data-lang") or "").lower()
                for c in cls.split():
                    if c.startswith("lang-"):
                        self._code_lang = c[5:].lower()
            return

        if tag == "code":
            if self._pre_depth > 0:
                for c in cls.split():
                    if c.startswith("language-"):
                        self._code_lang = c[9:].lower()
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
            if not self._is_bare_marker(self._current_line):
                self._flush_line()
            self._tag_stack.append({"tag": "p"})
            return

        if tag == "br":
            if self._in_table and self._cell_buffer_target == "cell":
                self._current_cell.append(" <br> ")
                return
            self._flush_line()
            return

        if tag == "hr":
            self._flush_line()
            self._ensure_blank()
            self._output.append("---")
            self._output.append("")
            return

        if tag in ("strong", "b"):
            inside_code = (self._pre_depth > 0
                           or any(t.get("tag") == "code" for t in self._tag_stack))
            if not inside_code:
                self._append_inline("**")
            self._tag_stack.append({"tag": "strong", "inside_code": inside_code})
            return

        if tag in ("em", "i"):
            inside_code = (self._pre_depth > 0
                           or any(t.get("tag") == "code" for t in self._tag_stack))
            if not inside_code:
                self._append_inline("*")
            self._tag_stack.append({"tag": "em", "inside_code": inside_code})
            return

        if tag == "a":
            href = a.get("href", "")
            # Inside <pre>, links must stay plain text (BigQuery SQL syntax
            # diagrams hyperlink every keyword).
            if not href or self._pre_depth > 0:
                self._tag_stack.append({"tag": "a", "href": "", "suppress": True})
                return
            href = self._normalize_href(href)
            self._append_inline("[")
            self._tag_stack.append({"tag": "a", "href": href, "suppress": False})
            return

        if tag == "img":
            if self._pre_depth > 0:
                return
            alt = a.get("alt", "").strip()
            src = a.get("src", "").strip()
            if src:
                if src.startswith("/"):
                    src = SITE + src
                self._append_inline(f"![{alt}]({src})")
            return

        if tag in ("ul", "ol"):
            if self._in_table and self._cell_buffer_target == "cell":
                self._current_cell.append(" ")
                return
            self._flush_line()
            self._list_stack.append([tag, 0])
            return

        if tag == "li":
            if self._in_table and self._cell_buffer_target == "cell":
                if any(s.strip() for s in self._current_cell):
                    self._current_cell.append(" <br> ")
                return
            self._flush_line()
            if not self._list_stack:
                self._list_stack.append(["ul", 0])
            self._list_stack[-1][1] += 1
            indent = "  " * (len(self._list_stack) - 1)
            if self._list_stack[-1][0] == "ol":
                marker = f"{self._list_stack[-1][1]}. "
            else:
                marker = "- "
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
        if tag == "tr":
            if self._in_table:
                self._current_row = []
            return
        if tag in ("td", "th"):
            if self._in_table:
                self._current_cell = []
                self._cell_is_header = (tag == "th" or self._in_thead)
                self._cell_buffer_target = "cell"
                try:
                    self._cell_colspan = int(a.get("colspan") or "1")
                except ValueError:
                    self._cell_colspan = 1
            return

    def handle_endtag(self, tag):
        if not self._in_body or not self._started:
            return

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

        if self._notes and self._notes[-1]["tag"] == tag:
            target = self._notes[-1]["body_depth"] - (1 if tag == "div" else 0)
            if self._body_depth == target:
                self._flush_line()
                self._notes.pop()
                self._ensure_blank()
                return

        if tag in ("devsite-code", "devsite-selector", "devsite-expandable",
                   "section", "devsite-tabs", "nav"):
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
            if tag == "h1" and not self.title:
                self.title = self._current_line.lstrip("#").strip()
            self._flush_line()
            self._output.append("")
            self._in_heading = 0
            if self._tag_stack and self._tag_stack[-1].get("tag") == tag:
                self._tag_stack.pop()
            return

        if tag == "p":
            if self._tag_stack and self._tag_stack[-1].get("tag") == "p":
                self._tag_stack.pop()
            if self._in_table and self._cell_buffer_target == "cell":
                self._current_cell.append(" <br> ")
                return
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
                self._append_inline(f"]({meta.get('href', '')})")
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
                text = re.sub(r"\s+", " ", text)
                text = re.sub(r"(?: ?<br> ?)+", " <br> ", text)
                text = re.sub(r"^<br> | <br>$", "", text).strip()
                self._current_row.append({
                    "text": text,
                    "is_header": self._cell_is_header,
                    "colspan": self._cell_colspan,
                })
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
        if tag == "table":
            if self._in_table:
                self._render_table()
                self._in_table = False
                self._table_rows = []
            return

    def handle_data(self, data):
        if not self._in_body or not self._started or self._skip_depth > 0:
            return
        if self._pre_depth > 0:
            self._code_content += data
            return
        data = data.replace("\u200b", "")
        if self._in_table and self._cell_buffer_target == "cell":
            self._current_cell.append(data.replace("\xa0", " "))
            return
        if data.strip() == "" and self._current_line.endswith(" "):
            return
        normalized = re.sub(r"[\t\n]+", " ", data.replace("\xa0", " "))
        normalized = re.sub(r"  +", " ", normalized)
        if not self._current_line and not self._in_heading:
            normalized = normalized.lstrip()
        self._current_line += normalized

    # -- tables ----------------------------------------------------------

    def _render_table(self):
        rows = self._table_rows
        if not rows:
            return
        self._ensure_blank()

        # Tables using full-width colspan rows as section dividers (REST
        # "Fields" tables and friends) read better as definition lists;
        # their description cells hold lists and code blocks.
        has_divider = any(len(r) == 1 and r[0].get("colspan", 1) >= 2 for r in rows)

        if has_divider:
            for row in rows:
                if len(row) == 1 and row[0].get("colspan", 1) >= 2:
                    label = row[0]["text"].strip()
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
                desc = " ".join(c["text"].strip() for c in row[1:] if c["text"].strip())
                if not name and not desc:
                    continue
                if name:
                    self._output.append(f"- **{name}**")
                if desc:
                    self._output.append(f"  {desc}")
            self._output.append("")
            return

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
        stripped = line.rstrip()
        if not stripped:
            return False
        return bool(re.match(r"^\s*(-|\d+\.)$", stripped))

    @staticmethod
    def _normalize_href(href: str) -> str:
        if href.startswith(("http://", "https://", "#", "mailto:")):
            return href
        if href.startswith("//"):
            return "https:" + href
        if href.startswith("/"):
            return SITE + href
        return href

    def finalize(self) -> str:
        self._flush_line()
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
        while out and not out[0].strip():
            out.pop(0)
        while out and not out[-1].strip():
            out.pop()
        text = "\n".join(out) + "\n"
        text = re.sub(r"`\[([^\]\n`]+)\]\(([^)\n]+)\)`", r"[`\1`](\2)", text)
        return text


def extract_title(head_html: str) -> str:
    m = HEADLINE_RE.search(head_html)
    if m:
        try:
            return json.loads(f'"{m.group(1)}"').strip()
        except ValueError:
            pass
    m = TITLE_RE.search(head_html)
    if m:
        t = html_lib.unescape(m.group(1)).replace("\xa0", " ")
        t = re.sub(r"\s+", " ", t).strip()
        if " | " in t:
            t = t.split(" | ")[0].strip()
        return t
    return ""


def html_to_markdown(page_html: str, source_url: str) -> str | None:
    m = BODY_START_RE.search(page_html)
    if not m:
        return None
    title = extract_title(page_html[: m.start()])
    parser = DevsiteExtractor()
    try:
        parser.feed(page_html[m.start():])
        parser.close()
    except Exception:
        return None
    body = parser.finalize()
    if not body.strip():
        return None
    header_title = parser.title or title or source_url.rsplit("/", 1)[-1]
    header = f"# {header_title}\n\nSource: {source_url}\n\n"
    lines = body.split("\n")
    if lines and lines[0].startswith("# ") and lines[0][2:].strip() == header_title:
        lines = lines[1:]
        while lines and not lines[0].strip():
            lines.pop(0)
        body = "\n".join(lines) + "\n"
    return header + body


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------

_W_THREADS = 12


def _pool_init(threads: int):
    global _W_THREADS
    _W_THREADS = threads


def _process_one(job) -> tuple:
    """job: (path, rel, lastmod, old_sha) -> (status, rel, sha, lastmod, path)

    status: added | updated | unchanged | empty | failed | http404 ...
    """
    path, rel, lastmod, old_sha = job
    status, body = http_get(path)
    if body is None:
        return (f"http{status}" if status else "failed", rel, None, lastmod, path)
    text = body.decode("utf-8", errors="replace")
    md = html_to_markdown(text, SITE + path)
    if md is None:
        return ("empty", rel, "", lastmod, path)
    sha = hashlib.sha256(md.encode("utf-8")).hexdigest()
    out_path = os.path.join(DOCS_DIR, rel)
    if sha == old_sha and os.path.exists(out_path):
        return ("unchanged", rel, sha, lastmod, path)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(md)
    return ("added" if old_sha is None else "updated", rel, sha, lastmod, path)


_worker_pool: ThreadPoolExecutor | None = None


def _process_batch(jobs: list) -> list:
    # One persistent thread pool per worker process, so each thread's
    # keep-alive connection survives across batches.
    global _worker_pool
    if _worker_pool is None:
        _worker_pool = ThreadPoolExecutor(max_workers=_W_THREADS)
    return list(_worker_pool.map(_process_one, jobs))


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, separators=(",", ":"), sort_keys=True)
    os.replace(tmp, CACHE_FILE)


# ---------------------------------------------------------------------------
# README
# ---------------------------------------------------------------------------

def build_top_readme(cache: dict, total: int) -> str:
    groups: dict[str, int] = {}
    for rel in cache:
        if rel == "README.md":
            continue
        top = rel.split("/", 1)[0]
        if top.endswith(".md"):
            top = top[:-3]
        groups[top] = groups.get(top, 0) + 1
    lines = [
        "# Google Cloud Documentation",
        "",
        f"Source: {SITE}/docs",
        "",
        f"Full mirror of docs.cloud.google.com ({total} pages, English).",
        "Files mirror the site's URL paths: the page at",
        "`https://docs.cloud.google.com/compute/docs/instances` lives at",
        "`compute/docs/instances.md`.",
        "",
        "## Products",
        "",
    ]
    for top in sorted(groups):
        lines.append(f"- `{top}/` - {groups[top]} pages")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args) -> None:
    os.makedirs(DOCS_DIR, exist_ok=True)
    cache = load_cache()

    plan, sitemap_complete = plan_pages(args)
    print(f"In scope: {len(plan)} pages")

    plan_rels = {rel for _, rel, _ in plan}

    jobs = []
    for path, rel, lastmod in plan:
        entry = cache.get(rel)
        if args.force or entry is None:
            jobs.append((path, rel, lastmod, entry["sha256"] if entry else None))
            continue
        out_path = os.path.join(DOCS_DIR, rel)
        file_ok = entry.get("sha256") == "" or os.path.exists(out_path)
        if entry.get("lastmod") != lastmod or not file_ok:
            jobs.append((path, rel, lastmod, entry.get("sha256") or None))

    # Removals: cached files whose URL left the sitemap, restricted to
    # the current scope so files excluded by flags (not gone from the
    # site) are never deleted. Needs every shard and no ad-hoc filters.
    removals = []
    translation_re = re.compile(r"\.hl-[A-Za-z-]+\.md$")

    def in_scope(rel: str) -> bool:
        if not args.include_sdk_reference and SDK_REFERENCE_RE.match(rel):
            return False
        if not args.include_translations and translation_re.search(rel):
            return False
        return True

    if sitemap_complete and not (args.only or args.limit):
        removals = [rel for rel in cache
                    if rel != "README.md" and rel not in plan_rels
                    and in_scope(rel)]

    print(f"To fetch: {len(jobs)} (new or changed)  |  unchanged: "
          f"{len(plan) - len(jobs)}  |  removals: {len(removals)}")

    if args.dry_run:
        for rel in removals[:20]:
            print(f"  REMOVE {rel}")
        if len(removals) > 20:
            print(f"  ... and {len(removals) - 20} more removals")
        for job in jobs[:20]:
            print(f"  FETCH {job[0]}")
        if len(jobs) > 20:
            print(f"  ... and {len(jobs) - 20} more fetches")
        return

    counts = {"added": 0, "updated": 0, "unchanged": 0, "empty": 0}
    failures: list[tuple] = []
    done = 0
    started = time.time()
    last_report = started
    last_save = started

    def handle_results(results):
        nonlocal done, last_report, last_save
        for status, rel, sha, lastmod, path in results:
            done += 1
            if status in ("added", "updated", "unchanged", "empty"):
                counts[status] += 1
                cache[rel] = {"sha256": sha, "lastmod": lastmod}
                if args.verbose and status in ("added", "updated"):
                    print(f"  {status.upper()} {rel}")
            else:
                failures.append((status, path, rel, lastmod))
                if args.verbose:
                    print(f"  FAIL({status}) {path}", file=sys.stderr)
        now = time.time()
        if now - last_report >= 10:
            rate = done / max(now - started, 1)
            eta = (len(jobs) - done) / max(rate, 0.1)
            print(f"  {done}/{len(jobs)} pages  {rate:.0f}/s  "
                  f"ETA {eta/60:.0f}m  (+{counts['added']} ~{counts['updated']} "
                  f"={counts['unchanged']} !{len(failures)})", flush=True)
            last_report = now
        if now - last_save >= 120:
            save_cache(cache)
            last_save = now

    if jobs:
        batch_size = 200
        batches = [jobs[i:i + batch_size] for i in range(0, len(jobs), batch_size)]
        print(f"Fetching with {args.procs} processes x {args.threads} threads "
              f"({len(batches)} batches)")
        try:
            with ProcessPoolExecutor(
                max_workers=args.procs,
                initializer=_pool_init, initargs=(args.threads,),
            ) as pool:
                futures = [pool.submit(_process_batch, b) for b in batches]
                for fut in as_completed(futures):
                    handle_results(fut.result())

                # One retry round for transient failures.
                retry = [(p, r, lm, cache.get(r, {}).get("sha256"))
                         for (st, p, r, lm) in failures if st != "http404"]
                if retry:
                    print(f"Retrying {len(retry)} failed pages")
                    failures.clear()
                    retry_batches = [retry[i:i + 50] for i in range(0, len(retry), 50)]
                    for fut in as_completed(
                        [pool.submit(_process_batch, b) for b in retry_batches]
                    ):
                        handle_results(fut.result())
        except KeyboardInterrupt:
            print("\nInterrupted; saving cache (rerun to resume)")
            save_cache(cache)
            raise

    removed = 0
    for rel in removals:
        cache.pop(rel, None)
        out_path = os.path.join(DOCS_DIR, rel)
        if os.path.exists(out_path):
            os.remove(out_path)
            removed += 1
            d = os.path.dirname(out_path)
            while d != DOCS_DIR and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
                d = os.path.dirname(d)
            if args.verbose:
                print(f"  REMOVE {rel}")

    readme = build_top_readme(cache, len(plan_rels))
    with open(os.path.join(DOCS_DIR, "README.md"), "w") as f:
        f.write(readme)
    cache["README.md"] = {
        "sha256": hashlib.sha256(readme.encode()).hexdigest(), "lastmod": None,
    }

    save_cache(cache)

    elapsed = time.time() - started
    print()
    print(f"Sync complete in {elapsed/60:.1f}m:")
    print(f"  Added:     {counts['added']}")
    print(f"  Updated:   {counts['updated']}")
    print(f"  Unchanged: {len(plan) - len(jobs) + counts['unchanged']}")
    print(f"  Empty:     {counts['empty']}")
    print(f"  Removed:   {removed}")
    print(f"  Failed:    {len(failures)}")
    if failures:
        for st, path, _, _ in failures[:10]:
            print(f"    {st}: {path}")
        if len(failures) > 10:
            print(f"    ... and {len(failures) - 10} more")


def main():
    parser = argparse.ArgumentParser(
        description="Mirror docs.cloud.google.com to local markdown")
    parser.add_argument("--dry-run", action="store_true",
                        help="Show the fetch/removal plan without fetching pages")
    parser.add_argument("--force", action="store_true",
                        help="Refetch everything ignoring lastmod cache")
    parser.add_argument("--verbose", action="store_true",
                        help="Per-file logging")
    parser.add_argument("--procs", type=int,
                        default=max(4, min(12, (os.cpu_count() or 8) // 2)),
                        help="Worker processes (default: half the cores, max 12)")
    parser.add_argument("--threads", type=int, default=12,
                        help="Threads per worker process (default 12)")
    parser.add_argument("--only", type=str, default="",
                        help="Comma-separated URL path prefixes to include "
                             "(e.g. compute,bigquery,sdk)")
    parser.add_argument("--limit", type=int, default=0,
                        help="Cap the number of pages (testing)")
    parser.add_argument("--include-sdk-reference", action="store_true",
                        help="Also mirror {lang}/docs/reference/* per-class "
                             "client-library pages (~410k extra pages)")
    parser.add_argument("--include-translations", action="store_true",
                        help="Also mirror ?hl=xx translated pages")
    parser.add_argument("--sitemap-cache", action="store_true",
                        help="Reuse .sitemap-pages.json instead of re-downloading "
                             "the 60 sitemap shards (testing)")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
