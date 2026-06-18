#!/usr/bin/env python3

"""
SABnzbd API Documentation Fetcher

The SABnzbd API reference is a single HTML wiki page on sabnzbd.org (no
machine-readable spec, no MediaWiki API -- it is a custom CMS). The whole
reference lives inside one <div class="wiki-content"> element, structured as:

  <h1 id="...">Group</h1>     -- top-level groups (Queue / History / Status /
                                 Other functions) plus an "Introduction" group
  <h2 id="...">Function</h2>  -- individual API functions (mode=...). The id is
                                 the stable anchor and the api 'mode' value.

This fetcher fetches that one page, converts the body to markdown with a
focused html.parser.HTMLParser subclass (handling <pre>/<figure class=highlight>
code blocks, parameter tables with nested lists, inline code/links, and the
<span class="label"> return-type badges on function headings), then slices it
into one markdown file per function grouped under per-group directories.

Because everything comes from a single page, .cache.json stores a per-section
SHA256 so the run summary still reports granular "what changed".

Output layout:
  docs/
    README.md                       (auto-generated catalogue)
    introduction/
      README.md                     (group intro + section index)
      request-types.md
      ...
    queue-functions/
      README.md
      queue.md                      (named by the function's anchor / mode)
      pause.md
      ...
    history-functions/  status-functions/  other-functions/
"""

import argparse
import hashlib
import html.parser
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

WIKI_URL = "https://sabnzbd.org/wiki/configuration/5.0/api"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={"User-Agent": "sabnzbd-api-docs-fetcher/1.0"})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
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
# HTML -> markdown parser
# ---------------------------------------------------------------------------

# Tags that introduce a new block; encountering one flushes any pending inline
# text. The page has many unclosed <p> tags, so we cannot rely on </p>.
BLOCK_TAGS = {"p", "pre", "figure", "table", "ul", "ol", "hr", "blockquote",
              "div", "h1", "h2", "h3", "h4", "h5", "h6"}


class WikiSection:
    """A heading and the markdown body that follows it, up to the next heading."""

    def __init__(self, level: int, title: str, anchor: str | None, label: str | None):
        self.level = level          # 0 = preamble, 1 = h1 group, 2 = h2 function
        self.title = title
        self.anchor = anchor        # the id= value (also the api 'mode')
        self.label = label          # the <span class="label"> badge, e.g. "True/False"
        self.lines: list[str] = []  # markdown block lines

    def emit(self, text: str):
        self.lines.append(text)
        self.lines.append("")

    def body_markdown(self) -> str:
        out: list[str] = []
        for line in self.lines:
            if line == "" and (not out or out[-1] == ""):
                continue
            out.append(line)
        return "\n".join(out).strip()


class WikiParser(html.parser.HTMLParser):
    """Convert the sabnzbd wiki-content <div> into a list of WikiSection objects."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.sections: list[WikiSection] = [WikiSection(0, "", None, None)]

        self._in_content = False
        self._content_depth = 0     # div nesting inside wiki-content
        self._skip_depth = 0        # >0 while inside script/style/footer
        self._skip_tag: str | None = None

        self._line = ""             # pending inline text for the current block

        # Code-block state.
        self._pre_depth = 0
        self._code_buf = ""
        self._code_lang = ""

        # List state: stack of [kind, counter]; kind is "ul" or "ol".
        self._list_stack: list[list] = []

        # Heading state.
        self._heading_level = 0
        self._heading_title = ""
        self._heading_anchor: str | None = None
        self._heading_label: str | None = None
        self._in_label = False      # inside a <span class="label"> within a heading

        # Inline emphasis: stack of opening markers to re-close on end tag.
        self._inline_stack: list[str] = []
        self._code_inline_depth = 0  # inside inline <code> (suppress link markup)

        # Table state.
        self._in_table = False
        self._table_rows: list[list[str]] = []
        self._table_header: list[str] | None = None
        self._row: list[str] = []
        self._cell = ""             # inline buffer for the current cell
        self._cell_list_depth = 0   # nested <ul>/<ol> depth inside a cell

    # -- helpers ------------------------------------------------------------

    @property
    def _cur(self) -> WikiSection:
        return self.sections[-1]

    def _append_text(self, text: str):
        if self._pre_depth > 0:
            self._code_buf += text
            return
        if self._heading_level:
            if self._in_label:
                self._heading_label = (self._heading_label or "") + text
            else:
                self._heading_title += text
            return
        if self._in_table:
            self._cell += text
            return
        self._line += text

    def _flush_line(self):
        line = re.sub(r"[ \t]+", " ", self._line).strip()
        self._line = ""
        if line:
            self._cur.emit(line)

    # -- start tags ---------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        a = {k: (v or "") for k, v in attrs}
        cls = a.get("class", "")

        if not self._in_content:
            if tag == "div" and "wiki-content" in cls:
                self._in_content = True
                self._content_depth = 1
            return

        if self._skip_depth > 0:
            if tag == self._skip_tag:
                self._skip_depth += 1
            return

        if tag in ("script", "style", "footer", "noscript"):
            self._skip_depth = 1
            self._skip_tag = tag
            return

        if tag == "div":
            self._content_depth += 1

        # Block boundary: commit any pending inline text first.
        if tag in BLOCK_TAGS and self._pre_depth == 0 and not self._in_table:
            self._flush_line()

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._heading_level = int(tag[1])
            self._heading_title = ""
            self._heading_anchor = a.get("id") or None
            self._heading_label = None
            return

        if self._heading_level:
            if tag == "span" and "label" in cls:
                self._in_label = True
            return

        if tag in ("pre",):
            self._pre_depth += 1
            return
        if tag == "code":
            if self._pre_depth > 0:
                lang = a.get("data-lang") or ""
                if not lang:
                    m = re.search(r"language-(\w+)", cls)
                    lang = m.group(1) if m else ""
                self._code_lang = lang
                return
            self._code_inline_depth += 1
            self._append_text("`")
            return

        if tag in ("strong", "b"):
            self._append_text("**")
            self._inline_stack.append("**")
            return
        if tag in ("em", "i"):
            self._append_text("*")
            self._inline_stack.append("*")
            return
        if tag == "small":
            self._append_text(" _")
            self._inline_stack.append("_")
            return

        if tag == "a":
            href = a.get("href", "")
            self._inline_stack.append(self._link_close(href))
            opener = self._link_open(href)
            if opener:
                self._append_text(opener)
            return

        if tag == "br":
            if self._in_table:
                self._cell += "<br>"
            else:
                self._append_text(" ")
            return

        if tag in ("ul", "ol"):
            if self._in_table:
                self._cell_list_depth += 1
            else:
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
            return
        if tag == "tr" and self._in_table:
            self._commit_row()  # tolerate rows left unclosed in the source HTML
            return
        if tag in ("td", "th") and self._in_table:
            self._cell = ""
            self._cell_list_depth = 0
            return

        if tag == "img" and not self._in_table:
            src = urljoin(WIKI_URL, a.get("src", ""))
            alt = a.get("alt", "")
            self._cur.emit(f"![{alt}]({src})")
            return

    # -- end tags -----------------------------------------------------------

    def handle_endtag(self, tag):
        if not self._in_content:
            return

        if self._skip_depth > 0:
            if tag == self._skip_tag:
                self._skip_depth -= 1
                if self._skip_depth == 0:
                    self._skip_tag = None
            return

        if tag == "div":
            self._content_depth -= 1
            if self._content_depth <= 0:
                self._finish_heading_if_open()
                self._flush_line()
                self._in_content = False
            return

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            self._finish_heading_if_open()
            return

        if self._heading_level:
            if tag == "span" and self._in_label:
                self._in_label = False
            return

        if tag == "pre":
            if self._pre_depth > 0:
                self._pre_depth -= 1
                if self._pre_depth == 0:
                    self._emit_code_block()
            return
        if tag == "code":
            if self._code_inline_depth > 0:
                self._append_text("`")
                self._code_inline_depth -= 1
            return

        if tag in ("strong", "b", "em", "i", "small", "a"):
            if self._inline_stack:
                self._append_text(self._inline_stack.pop())
            return

        if tag in ("ul", "ol"):
            if self._in_table:
                self._cell_list_depth = max(0, self._cell_list_depth - 1)
            elif self._list_stack:
                self._list_stack.pop()
                if not self._list_stack:
                    self._cur.emit("")
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
            self._commit_row()  # flush a final unclosed row, if any
            self._emit_table()
            self._in_table = False
            return

        if tag == "p":
            self._flush_line()

    def handle_data(self, data):
        if not self._in_content or self._skip_depth > 0:
            return
        self._append_text(data)

    # -- link rendering -----------------------------------------------------

    def _link_open(self, href: str) -> str:
        """Opening marker for an <a>. Internal #anchors render as plain text
        (their targets live in sibling files, so a real link would dangle)."""
        if self._code_inline_depth > 0:
            return ""
        if href.startswith("#") or not href:
            return ""
        return "["

    def _link_close(self, href: str) -> str:
        if self._code_inline_depth > 0:
            return ""
        if href.startswith("#") or not href:
            return ""
        return f"]({urljoin(WIKI_URL, href)})"

    # -- list rendering -----------------------------------------------------

    def _start_list_item(self):
        self._flush_line()
        depth = len(self._list_stack) - 1
        indent = "  " * max(0, depth)
        kind, counter = self._list_stack[-1] if self._list_stack else ["ul", 0]
        if kind == "ol":
            counter += 1
            self._list_stack[-1][1] = counter
            self._line = f"{indent}{counter}. "
        else:
            self._line = f"{indent}- "

    def _flush_list_item(self):
        line = re.sub(r"[ \t]+", " ", self._line).rstrip()
        self._line = ""
        if line.strip(" -0123456789."):
            self._cur.lines.append(line)

    # -- code block rendering ----------------------------------------------

    def _emit_code_block(self):
        code = self._code_buf.strip("\n")
        self._code_buf = ""
        lang = self._code_lang
        self._code_lang = ""
        if not code.strip():
            return
        fence = "```"
        while fence in code:
            fence += "`"
        self._cur.lines.append(f"{fence}{lang}")
        self._cur.lines.extend(code.split("\n"))
        self._cur.lines.append(fence)
        self._cur.lines.append("")

    # -- table rendering ----------------------------------------------------

    def _commit_row(self):
        if self._row:
            if self._table_header is None:
                self._table_header = self._row
            else:
                self._table_rows.append(self._row)
        self._row = []

    def _render_cell(self, cell: str) -> str:
        # Markdown table cells must stay on one physical line; collapse the
        # source's newlines/indentation and tidy the <br>-joined list items.
        cell = cell.replace("\n", " ")
        cell = re.sub(r"(\s*<br>\s*)+", "<br>", cell)
        cell = re.sub(r"[ \t]+", " ", cell).strip()
        cell = re.sub(r"^(<br>)+|(<br>)+$", "", cell)
        return cell.replace("|", "\\|").strip()

    def _emit_table(self):
        header = self._table_header or []
        rows = self._table_rows

        # Navigation tables at the top of each group duplicate the index we
        # auto-generate below; drop them.
        if header[:2] == ["Function", "Description"]:
            return

        if not header and rows:
            header = rows[0]
            rows = rows[1:]
        if not header:
            return

        ncol = max([len(header)] + [len(r) for r in rows])
        header = header + [""] * (ncol - len(header))
        self._cur.lines.append("| " + " | ".join(header) + " |")
        self._cur.lines.append("|" + "|".join(["---"] * ncol) + "|")
        for r in rows:
            r = r + [""] * (ncol - len(r))
            self._cur.lines.append("| " + " | ".join(r) + " |")
        self._cur.lines.append("")

    # -- heading completion -------------------------------------------------

    def _finish_heading_if_open(self):
        if not self._heading_level:
            return
        level = self._heading_level
        title = re.sub(r"\s+", " ", self._heading_title).strip()
        anchor = self._heading_anchor
        label = (self._heading_label or "").strip() or None
        self._heading_level = 0
        self._heading_title = ""
        self._heading_anchor = None
        self._heading_label = None
        self._in_label = False
        if not title and not anchor:
            return  # empty placeholder heading
        self.sections.append(WikiSection(level, title, anchor, label))


# ---------------------------------------------------------------------------
# Section grouping
# ---------------------------------------------------------------------------

class Group:
    def __init__(self, title: str, anchor: str | None):
        self.title = title
        self.anchor = anchor
        self.slug = sanitize_filename(title) or (anchor or "group")
        self.preamble: list[str] = []   # markdown before the first function
        self.functions: list[dict] = []  # {title, anchor, label, filename, markdown}


def group_sections(sections: list[WikiSection]) -> list[Group]:
    """Walk the flat section list into h1 groups, each with h2 functions.

    Content before the first h1 (only the page's jump-navigation) is dropped.
    h3+ headings are folded back into the body of the function/group they
    belong to.
    """
    groups: list[Group] = []
    cur_group: Group | None = None
    cur_func: dict | None = None
    used_filenames: dict[str, set] = {}

    for sec in sections:
        if sec.level == 1:
            cur_group = Group(sec.title, sec.anchor)
            groups.append(cur_group)
            cur_func = None
            used_filenames[cur_group.slug] = set()
            # The <h1>'s own body (e.g. the Introduction lead-in) precedes the
            # first <h2>, so it belongs to the group preamble.
            h1_body = sec.body_markdown()
            if h1_body:
                cur_group.preamble.append(h1_body)
            continue

        if cur_group is None:
            continue  # drop pre-first-h1 content (jump nav)

        if sec.level == 2:
            base = sec.anchor or sanitize_filename(sec.title) or "section"
            base = sanitize_filename(base)
            name = base
            n = 2
            while name in used_filenames[cur_group.slug]:
                name = f"{base}-{n}"
                n += 1
            used_filenames[cur_group.slug].add(name)
            cur_func = {
                "title": sec.title,
                "anchor": sec.anchor,
                "label": sec.label,
                "filename": f"{name}.md",
                "body": sec.body_markdown(),
            }
            cur_group.functions.append(cur_func)
            continue

        # level 0 preamble lines, or h3+ headings -> attach as body.
        body = sec.body_markdown()
        heading_md = ""
        if sec.level >= 3 and sec.title:
            heading_md = f"### {sec.title}"
        chunk = "\n\n".join(p for p in (heading_md, body) if p).strip()
        if not chunk:
            continue
        if cur_func is not None:
            cur_func["body"] = (cur_func["body"] + "\n\n" + chunk).strip()
        else:
            cur_group.preamble.append(chunk)

    return groups


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------

def build_function_markdown(group: Group, func: dict) -> str:
    lines = [f"# {func['title']}\n"]
    meta = []
    if func["anchor"]:
        meta.append(f"**API mode:** `{func['anchor']}`")
    if func["label"]:
        meta.append(f"**Returns:** {func['label']}")
    if meta:
        lines.append("  \n".join(meta) + "\n")
    if func["anchor"]:
        src = f"{WIKI_URL}#{func['anchor']}"
    else:
        src = WIKI_URL
    lines.append(f"Source: [{src}]({src})\n")
    if func["body"]:
        lines.append(func["body"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_group_readme(group: Group) -> str:
    lines = [f"# {group.title}\n"]
    if group.anchor:
        src = f"{WIKI_URL}#{group.anchor}"
        lines.append(f"Source: [{src}]({src})\n")
    if group.preamble:
        lines.append("\n\n".join(group.preamble).strip() + "\n")
    if group.functions:
        lines.append("## Functions\n")
        for func in group.functions:
            label = f" -- {func['label']}" if func["label"] else ""
            mode = f" (`{func['anchor']}`)" if func["anchor"] else ""
            lines.append(f"- [{func['title']}](./{func['filename']}){mode}{label}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_top_readme(groups: list[Group]) -> str:
    lines = ["# SABnzbd API Documentation\n"]
    lines.append(
        "Reference for the SABnzbd HTTP API. Every request goes to "
        "`http://host:port/api` and (except `version` and `auth`) requires the "
        "user's API key. See the Introduction group for output formats and "
        "authentication.\n"
    )
    lines.append(f"Source: [{WIKI_URL}]({WIKI_URL})\n")
    total = sum(len(g.functions) for g in groups)
    lines.append(f"**Functions documented:** {total}\n")
    lines.append("## Groups\n")
    for g in groups:
        lines.append(f"- [{g.title}](./{g.slug}/) ({len(g.functions)} functions)")
    lines.append("")
    for g in groups:
        if not g.functions:
            continue
        lines.append(f"### {g.title}\n")
        for func in g.functions:
            label = f" -- {func['label']}" if func["label"] else ""
            mode = f"`{func['anchor']}` " if func["anchor"] else ""
            lines.append(f"- {mode}[{func['title']}](./{g.slug}/{func['filename']}){label}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print(f"Fetching SABnzbd API wiki page...\n  {WIKI_URL}")
    html_text = fetch_url(WIKI_URL)
    if not html_text:
        sys.exit(1)

    parser = WikiParser()
    parser.feed(html_text)
    groups = group_sections(parser.sections)

    total_funcs = sum(len(g.functions) for g in groups)
    print(f"  Groups: {len(groups)}")
    print(f"  Functions: {total_funcs}")
    if total_funcs == 0:
        print("ERROR: no functions parsed -- page structure may have changed",
              file=sys.stderr)
        sys.exit(1)

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}

    def write_file(rel_path: str, content: str):
        nonlocal added, updated, unchanged
        cache_key = rel_path
        content_hash = sha256(content)
        full_path = os.path.join(DOCS_DIR, rel_path)
        if cache.get(cache_key, {}).get("sha256") == content_hash and os.path.exists(full_path):
            unchanged += 1
            new_cache[cache_key] = cache[cache_key]
            return
        is_new = cache_key not in cache or not os.path.exists(full_path)
        action = "ADD" if is_new else "UPDATE"
        if args.dry_run:
            print(f"  {action} {rel_path}")
        else:
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            with open(full_path, "w") as f:
                f.write(content)
            if args.verbose:
                print(f"  {action} {rel_path}")
        new_cache[cache_key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    for g in groups:
        write_file(os.path.join(g.slug, "README.md"), build_group_readme(g))
        for func in g.functions:
            write_file(os.path.join(g.slug, func["filename"]),
                       build_function_markdown(g, func))

    write_file("README.md", build_top_readme(groups))

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

    # Prune empty group directories.
    if not args.dry_run:
        for entry in os.scandir(DOCS_DIR):
            if entry.is_dir() and not os.listdir(entry.path):
                os.rmdir(entry.path)
                if args.verbose:
                    print(f"  RMDIR {entry.name}/")
        save_cache(new_cache)

    print(f"\nSync complete:")
    print(f"  Added:      {added}")
    print(f"  Updated:    {updated}")
    print(f"  Unchanged:  {unchanged}")
    print(f"  Removed:    {removed}")
    print(f"  Total files: {added + updated + unchanged}")


def main():
    parser = argparse.ArgumentParser(
        description="Fetch SABnzbd API docs from the wiki page and convert to markdown"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true",
                        help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
