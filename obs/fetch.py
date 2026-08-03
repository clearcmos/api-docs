#!/usr/bin/env python3

"""
OBS Studio Documentation Fetcher

docs.obsproject.com is a Sphinx site (sphinx_rtd_theme) documenting the OBS
Studio C API (libobs, modules, frontend API) plus the Lua/Python scripting API.
It publishes no OpenAPI spec, no sitemap.xml, and no llms.txt (requests for those
paths just return the index HTML). Two Sphinx artifacts give a clean path in:

  1. searchindex.js  -- the search index. Its `docnames` array is the
     authoritative list of every page in the project (44 pages).
  2. /_sources/{docname}.rst.txt  -- the raw reStructuredText source of each
     page, exactly as authored.

We enumerate pages from searchindex.js and convert each RST source to markdown.
The RST sources are dramatically cleaner than the rendered HTML: a C signature
like `int astrcmpi(const char *str1, const char *str2)` arrives verbatim in the
RST, whereas the rendered page explodes it into dozens of nested
syntax-highlight <span>s that would have to be reassembled.

Conversion handles the OBS Sphinx dialect: the C domain directives
(function/member/type/struct/enum/macro) and the Python domain (py:function),
`.. code::` blocks, `:param:`/`:return:` field lists, cross-reference roles
(:c:func:, :ref:, :doc:, :wiki:, ...), admonitions, versionadded/deprecated
notes, and named/inline external links. The page tree (for the README) comes
from recursively parsing `.. toctree::` directives starting at index.rst.
"""

import argparse
import gzip
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from typing import cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SITE = "https://docs.obsproject.com"
SEARCHINDEX_URL = f"{SITE}/searchindex.js"
SOURCE_URL = f"{SITE}/_sources/{{docname}}.rst.txt"
WIKI_BASE = "https://obsproject.com/wiki/"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

USER_AGENT = "obs-api-docs-fetcher/1.0"
MAX_WORKERS = 8


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
            return data.decode("utf-8")
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
# Page discovery (searchindex.js)
# ---------------------------------------------------------------------------


def discover_docnames() -> list[str]:
    """Return the authoritative page list from Sphinx's searchindex.js."""
    raw = fetch_url(SEARCHINDEX_URL)
    if not raw:
        print("ERROR: failed to fetch searchindex.js", file=sys.stderr)
        sys.exit(1)
    m = re.search(r"Search\.setIndex\((\{.*\})\)\s*$", raw.strip(), re.S)
    if not m:
        print("ERROR: could not parse searchindex.js", file=sys.stderr)
        sys.exit(1)
    obj = json.loads(m.group(1))
    docnames = obj.get("docnames", [])
    if not docnames:
        print("ERROR: searchindex.js has no docnames", file=sys.stderr)
        sys.exit(1)
    return list(docnames)


# ---------------------------------------------------------------------------
# RST helpers shared across passes
# ---------------------------------------------------------------------------

# Underline characters Sphinx/RST uses for section titles, in the order OBS
# actually uses them per page (level is assigned by first appearance).
UNDERLINE_CHARS = set("=-~^\"'+*#.:_`")

DIRECTIVE_RE = re.compile(r"^(\s*)\.\.\s+([A-Za-z][\w:-]*?)::(?:[ \t]+(.*))?\s*$")
LABEL_RE = re.compile(r"^(\s*)\.\.\s+_([\w.:+-]+):(?:[ \t]+(.*))?\s*$")
# Named hyperlink target: `.. _<name>: <url>`. The name may contain slashes,
# spaces, brackets ([1] footnote-style) and `::`; the greedy name backtracks to
# the last `: ` before the URL.
NAMED_TARGET_RE = re.compile(r"^\s*\.\.\s+_(.+):[ \t]+(\S.*)$")
COMMENT_RE = re.compile(r"^(\s*)\.\.(?:\s+.*)?$")
FIELD_RE = re.compile(
    r"^(\s*):(param|parameter|arg|argument|type|return|returns|rtype|raises|"
    r"keyword|kwarg|var|ivar|cvar)\b([^:]*):[ \t]*(.*)$"
)
BULLET_RE = re.compile(r"^(\s*)([-*+])[ \t]+(.*)$")


def expand(line: str) -> str:
    return line.replace("\t", "        ").rstrip("\n")


def indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def is_underline(line: str) -> bool:
    s = line.strip()
    if len(s) < 2:
        return False
    return len(set(s)) == 1 and s[0] in UNDERLINE_CHARS


def is_transition(line: str) -> bool:
    s = line.strip()
    return len(s) >= 4 and len(set(s)) == 1 and s[0] in UNDERLINE_CHARS


def slugify(text: str) -> str:
    """GitHub-style heading anchor slug."""
    # Strip inline markup that would not survive into the rendered heading.
    text = re.sub(r"`+", "", text)
    text = text.replace("*", "")
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s]+", "-", text.strip())
    return text


def heading_text(lines: list[str], i: int) -> str | None:
    """If line i is a section title (text followed by an underline), return the
    title text; otherwise None. Handles optional overline."""
    line = lines[i]
    s = line.strip()
    if not s or is_underline(line) or s.startswith(".."):
        return None
    if i + 1 >= len(lines):
        return None
    nxt = lines[i + 1]
    if not is_underline(nxt):
        return None
    # Underline must be at least as long as the title (RST rule); guard against
    # a transition line landing right under a paragraph.
    if len(nxt.strip()) < len(s):
        return None
    return s


# ---------------------------------------------------------------------------
# Pass 1: collect titles + label anchors across the whole corpus
# ---------------------------------------------------------------------------


def collect_metadata(sources: dict[str, str]) -> tuple[dict[str, str], dict[str, dict]]:
    """Return (doc_titles, label_map).

    doc_titles maps docname -> H1 title.
    label_map maps an RST label -> {"doc", "slug", "title"} for :ref: resolution.
    A `.. _label:` anchor attaches to the next section heading.
    """
    doc_titles: dict[str, str] = {}
    label_map: dict[str, dict] = {}

    for docname, text in sources.items():
        lines = [expand(line) for line in text.split("\n")]
        pending: list[str] = []
        title = None
        i = 0
        n = len(lines)
        while i < n:
            ht = heading_text(lines, i)
            if ht is not None:
                if title is None:
                    title = ht
                slug = slugify(ht)
                for lbl in pending:
                    label_map[lbl] = {"doc": docname, "slug": slug, "title": ht}
                pending = []
                i += 2
                continue
            m = LABEL_RE.match(lines[i])
            if m and not (m.group(3) or "").strip():
                # anchor label (no URL after) -- attaches to next heading
                pending.append(m.group(2))
                i += 1
                continue
            i += 1
        doc_titles[docname] = title or docname

    return doc_titles, label_map


# ---------------------------------------------------------------------------
# RST -> markdown converter
# ---------------------------------------------------------------------------

ADMONITIONS = {
    "note": "NOTE",
    "seealso": "NOTE",
    "hint": "TIP",
    "tip": "TIP",
    "important": "IMPORTANT",
    "warning": "WARNING",
    "caution": "WARNING",
    "attention": "WARNING",
    "danger": "CAUTION",
    "error": "CAUTION",
}

# C/Python domain object directives -> keyword prefix prepended to the signature.
OBJECT_PREFIX = {
    "function": "",
    "member": "",
    "macro": "",
    "type": "type ",
    "struct": "struct ",
    "enum": "enum ",
    "union": "union ",
    "py:function": "",
    "py:method": "",
    "py:class": "class ",
    "py:data": "",
    "c:function": "",
    "c:member": "",
    "c:macro": "",
    "c:type": "type ",
    "c:struct": "struct ",
    "c:enum": "enum ",
}

FIELD_LABELS = {
    "return": "Returns",
    "returns": "Returns",
    "rtype": "Return type",
    "raises": "Raises",
}

CODE_LANG = {"cpp": "cpp", "c": "c", "lua": "lua", "python": "python", "text": ""}


class RstConverter:
    def __init__(self, docname: str, text: str, doc_titles: dict[str, str], label_map: dict[str, dict]):
        self.docname = docname
        self.lines = [expand(line) for line in text.split("\n")]
        self.doc_titles = doc_titles
        self.label_map = label_map
        self.level_for_char: dict[str, int] = {}
        self.named_links: dict[str, str] = {}
        self.out: list[str] = []
        self._scan_heading_levels()
        self._scan_named_links()

    # -- pre-scans -----------------------------------------------------------

    def _scan_heading_levels(self) -> None:
        order: list[str] = []
        i = 0
        n = len(self.lines)
        while i < n:
            if heading_text(self.lines, i) is not None:
                ch = self.lines[i + 1].strip()[0]
                if ch not in order:
                    order.append(ch)
                i += 2
                continue
            i += 1
        self.level_for_char = {ch: idx + 1 for idx, ch in enumerate(order)}

    def _scan_named_links(self) -> None:
        for line in self.lines:
            m = NAMED_TARGET_RE.match(line)
            if m:
                self.named_links[m.group(1).strip()] = m.group(2).strip()

    # -- inline markup -------------------------------------------------------

    def inline(self, text: str) -> str:
        if not text:
            return text
        placeholders: list[str] = []

        def stash(s: str) -> str:
            placeholders.append(s)
            return f"\x00{len(placeholders) - 1}\x00"

        # 1. Inline literals ``code`` -> `code`
        def repl_literal(m: re.Match) -> str:
            return stash(f"`{m.group(1)}`")

        text = re.sub(r"``(.+?)``", repl_literal, text)

        # 2. Inline / named external links: `text <target>`_
        def repl_link(m: re.Match) -> str:
            label = m.group(1).strip()
            target = m.group(2).strip()
            if target.endswith("_"):
                target = self.named_links.get(target[:-1], target[:-1])
            return stash(f"[{label}]({target})")

        text = re.sub(r"`([^`<]+?)\s*<([^`>]+)>`_", repl_link, text)

        # 2b. Phrase references: `phrase`_ resolves to a named hyperlink target
        # (OBS also defines its footnotes this way, e.g. `[1]`_). Unknown phrases
        # fall back to inline code.
        def repl_phrase(m: re.Match) -> str:
            phrase = m.group(1).strip()
            url = self.named_links.get(phrase)
            if url:
                return stash(f"[{phrase}]({url})")
            return stash(f"`{phrase}`")

        text = re.sub(r"`([^`]+)`_", repl_phrase, text)

        # 3. Cross-reference roles: :domain:role:`content` and :role:`content`
        def repl_role(m: re.Match) -> str:
            domain = m.group(1)
            role = m.group(2)
            content = m.group(3).strip()
            return stash(self.render_role(domain, role, content))

        text = re.sub(r":(?:(\w+):)?(\w+):`([^`]*)`", repl_role, text)

        # 4. Bare named references: name_
        if self.named_links:

            def repl_named(m: re.Match) -> str:
                name = m.group(1)
                if name in self.named_links:
                    return stash(f"[{name}]({self.named_links[name]})")
                return cast(str, m.group(0))

            text = re.sub(r"\b([A-Za-z][\w.:+-]*)_\b(?!`)", repl_named, text)

        for idx, val in enumerate(placeholders):
            text = text.replace(f"\x00{idx}\x00", val)
        return text

    def render_role(self, domain: str | None, role: str, content: str) -> str:
        # Split "Title <target>" forms.
        m = re.match(r"^(.*?)\s*<([^>]+)>$", content)
        if m:
            title = m.group(1).strip()
            target = m.group(2).strip()
        else:
            title = ""
            target = content.strip()

        if role in ("ref",):
            info = self.label_map.get(target)
            if info:
                text = title or info["title"]
                anchor = f"#{info['slug']}" if info["slug"] else ""
                if info["doc"] == self.docname:
                    return f"[{text}]({anchor})" if anchor else f"**{text}**"
                return f"[{text}]({info['doc']}.md{anchor})"
            return title or target

        if role in ("doc",):
            page = target.lstrip("/")
            text = title or self.doc_titles.get(page, page)
            return f"[{text}]({page}.md)"

        if role == "wiki":
            page = target
            text = title or page
            return f"[{text}]({WIKI_BASE}{page})"

        # Symbol roles (func/meth/member/type/struct/macro/data/enum/class...).
        # `~pkg.name` shows only the last dotted component.
        display = title or target
        if display.startswith("~"):
            display = display[1:].split(".")[-1]
        display = display.lstrip("~")
        if role in ("func", "function", "meth", "method") and not display.endswith(")"):
            display = f"{display}()"
        return f"`{display}`"

    # -- block processing ----------------------------------------------------

    def emit(self, text: str = "") -> None:
        self.out.append(text)

    def collect_body(self, start: int, marker_indent: int) -> tuple[list[str], int]:
        """Collect the indented body of a directive/field starting at `start`.

        Returns (dedented_body_lines, next_index). Body lines are those more
        indented than `marker_indent`, plus interior blank lines.
        """
        body: list[str] = []
        i = start
        n = len(self.lines)
        # skip leading blank lines
        while i < n and not self.lines[i].strip():
            i += 1
        min_indent = None
        raw: list[str] = []
        while i < n:
            line = self.lines[i]
            if not line.strip():
                raw.append("")
                i += 1
                continue
            ind = indent_of(line)
            if ind <= marker_indent:
                break
            if min_indent is None or ind < min_indent:
                min_indent = ind
            raw.append(line)
            i += 1
        # trim trailing blanks
        while raw and not raw[-1].strip():
            raw.pop()
        body = [line[min_indent:] if line.strip() else "" for line in raw] if min_indent else raw
        return body, i

    def render_subblock(self, lines: list[str], prefix: str = "") -> None:
        sub = RstConverter.__new__(RstConverter)
        sub.docname = self.docname
        sub.lines = lines
        sub.doc_titles = self.doc_titles
        sub.label_map = self.label_map
        sub.level_for_char = self.level_for_char
        sub.named_links = self.named_links
        sub.out = []
        sub._process()
        # trim leading/trailing blanks and collapse interior blank runs so a
        # prefixed blockquote does not accumulate empty `>` lines
        rendered: list[str] = []
        blank = 0
        for line in sub.out:
            if line == "":
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            rendered.append(line)
        while rendered and rendered[0] == "":
            rendered.pop(0)
        while rendered and rendered[-1] == "":
            rendered.pop()
        for line in rendered:
            self.emit(prefix + line if line else prefix.rstrip())

    def run(self) -> str:
        self._process()
        # collapse 3+ blank lines into a single blank line
        result: list[str] = []
        blank = 0
        for line in self.out:
            if line == "":
                blank += 1
                if blank > 1:
                    continue
            else:
                blank = 0
            result.append(line)
        return "\n".join(result).strip("\n") + "\n"

    def _process(self) -> None:
        i = 0
        n = len(self.lines)
        while i < n:
            line = self.lines[i]
            stripped = line.strip()

            if not stripped:
                self.emit("")
                i += 1
                continue

            # Section heading (title + underline)
            ht = heading_text(self.lines, i)
            if ht is not None:
                ch = self.lines[i + 1].strip()[0]
                level = self.level_for_char.get(ch, 6)
                self.emit(f"{'#' * min(level, 6)} {self.inline(ht)}")
                self.emit("")
                i += 2
                continue

            # Transition / horizontal separator -> drop (functions are already
            # separated by blank lines + bold signatures).
            if is_transition(line):
                i += 1
                continue

            # Directive
            m = DIRECTIVE_RE.match(line)
            if m:
                i = self.handle_directive(i, m)
                continue

            # Label anchor / named link target -> drop (anchors resolved via
            # :ref:, named links inlined at use site)
            if LABEL_RE.match(line):
                i += 1
                continue

            # Field list (:param:/:return:/...)
            if FIELD_RE.match(line):
                i = self.handle_field_list(i)
                continue

            # Bullet list
            if BULLET_RE.match(line):
                i = self.handle_bullet_list(i)
                continue

            # Comment block (.. text with no ::)
            if COMMENT_RE.match(line) and not m:
                _, i = self.collect_body(i + 1, indent_of(line))
                continue

            # Paragraph
            i = self.handle_paragraph(i)

    def handle_paragraph(self, start: int) -> int:
        i = start
        n = len(self.lines)
        base = indent_of(self.lines[start])
        buf: list[str] = []
        while i < n:
            line = self.lines[i]
            if not line.strip():
                break
            if heading_text(self.lines, i) is not None:
                break
            if DIRECTIVE_RE.match(line) or LABEL_RE.match(line) or FIELD_RE.match(line):
                break
            if BULLET_RE.match(line):
                break
            if is_transition(line):
                break
            buf.append(line.strip())
            i += 1
        if not buf:
            return start + 1
        text = " ".join(buf)

        # RST literal block: a paragraph ending in `::` introduces a verbatim
        # indented block. `word::` -> `word:`, ` ::` / a lone `::` -> dropped.
        if text.endswith("::"):
            core = text[:-2]
            literal, nxt = self.collect_body(i, base)
            if core.strip():
                marker = "" if core.endswith(" ") else ":"
                self.emit(self.inline(core.rstrip() + marker))
                self.emit("")
            if literal:
                self.emit("```")
                for line in literal:
                    self.emit(line)
                self.emit("```")
                self.emit("")
                return nxt
            return i

        self.emit(self.inline(text))
        self.emit("")
        return i if i > start else start + 1

    def handle_field_list(self, start: int) -> int:
        i = start
        n = len(self.lines)
        base = indent_of(self.lines[start])
        items: list[str] = []
        while i < n:
            line = self.lines[i]
            if not line.strip():
                # peek: a blank then another field continues the list
                j = i + 1
                while j < n and not self.lines[j].strip():
                    j += 1
                if j < n and FIELD_RE.match(self.lines[j]) and indent_of(self.lines[j]) == base:
                    i = j
                    continue
                break
            fm = FIELD_RE.match(line)
            if not fm or indent_of(line) != base:
                break
            role = fm.group(2)
            arg = fm.group(3).strip()
            value = fm.group(4).strip()
            # continuation lines (more indented than the field marker)
            i += 1
            cont: list[str] = []
            while i < n:
                nl = self.lines[i]
                if not nl.strip():
                    # allow a blank inside a field only if the next content is
                    # still deeper-indented
                    j = i + 1
                    while j < n and not self.lines[j].strip():
                        j += 1
                    if j < n and indent_of(self.lines[j]) > base and not FIELD_RE.match(self.lines[j]):
                        cont.append("")
                        i = j
                        continue
                    break
                if indent_of(nl) <= base:
                    break
                if FIELD_RE.match(nl) and indent_of(nl) == base:
                    break
                cont.append(nl.strip())
                i += 1
            body = " ".join(x for x in ([value] + cont) if x).strip()
            label = self._field_label(role, arg)
            items.append(f"- **{label}**: {self.inline(body)}" if body else f"- **{label}**")
        if items:
            self.emit("")
            for it in items:
                self.emit(it)
            self.emit("")
        return i

    def _field_label(self, role: str, arg: str) -> str:
        arg = arg.strip()
        if role in FIELD_LABELS:
            return FIELD_LABELS[role]
        if role in ("param", "parameter", "arg", "argument", "keyword", "kwarg"):
            parts = arg.split()
            if len(parts) == 2:
                return f"{parts[1]} (`{parts[0]}`)"
            return arg or role
        if role == "type":
            return f"type {arg}" if arg else "type"
        return f"{role} {arg}".strip()

    def handle_bullet_list(self, start: int) -> int:
        i = start
        n = len(self.lines)
        base = indent_of(self.lines[start])
        # Determine indentation levels encountered to map to 2-space nesting.
        indents: list[int] = []

        def level_for(ind: int) -> int:
            if ind not in indents:
                indents.append(ind)
                indents.sort()
            return indents.index(ind)

        self.emit("")
        while i < n:
            line = self.lines[i]
            if not line.strip():
                j = i + 1
                while j < n and not self.lines[j].strip():
                    j += 1
                if j < n and BULLET_RE.match(self.lines[j]) and indent_of(self.lines[j]) >= base:
                    i = j
                    continue
                break
            bm = BULLET_RE.match(line)
            if not bm or indent_of(line) < base:
                break
            ind = len(bm.group(1))
            marker_col = ind + len(bm.group(2)) + 1
            text = bm.group(3).strip()
            i += 1
            # continuation lines of this item (indented past the marker)
            cont: list[str] = []
            while i < n:
                nl = self.lines[i]
                if not nl.strip():
                    break
                if BULLET_RE.match(nl):
                    break
                if indent_of(nl) < marker_col:
                    break
                cont.append(nl.strip())
                i += 1
            if cont:
                text = text + " " + " ".join(cont)
            lvl = level_for(ind)
            self.emit(f"{'  ' * lvl}- {self.inline(text.strip())}")
        self.emit("")
        return i

    def handle_directive(self, start: int, m: re.Match) -> int:
        marker_indent = len(m.group(1))
        name = m.group(2).lower()
        arg = (m.group(3) or "").strip()
        body_start = start + 1
        body, nxt = self.collect_body(body_start, marker_indent)

        if name == "toctree":
            return nxt  # tree is rendered separately in the README

        if name in ("code", "code-block", "sourcecode"):
            lang = CODE_LANG.get(arg.strip().lower(), arg.strip().lower())
            self.emit("")
            self.emit(f"```{lang}".rstrip())
            for line in body:
                self.emit(line)
            self.emit("```")
            self.emit("")
            return nxt

        if name in ("versionadded", "versionchanged", "deprecated"):
            verb = {
                "versionadded": "New in version",
                "versionchanged": "Changed in version",
                "deprecated": "Deprecated since version",
            }[name]
            note = f"{verb} {arg}." if arg else f"{verb}."
            if name == "deprecated":
                self.emit("")
                self.emit("> [!WARNING]")
                self.emit(f"> **{note}**")
                if any(x.strip() for x in body):
                    self.render_subblock(body, prefix="> ")
                self.emit("")
            else:
                self.emit("")
                self.emit(f"*{note}*")
                self.emit("")
                if any(x.strip() for x in body):
                    self.render_subblock(body)
                    self.emit("")
            return nxt

        if name in ADMONITIONS:
            self.emit("")
            self.emit(f"> [!{ADMONITIONS[name]}]")
            if arg:
                self.emit(f"> **{self.inline(arg)}**")
            self.render_subblock(body, prefix="> ")
            self.emit("")
            return nxt

        if name in OBJECT_PREFIX:
            prefix = OBJECT_PREFIX[name]
            sig = f"{prefix}{arg}".strip()
            self.emit("")
            self.emit(f"**`{sig}`**")
            self.emit("")
            if any(x.strip() for x in body):
                self.render_subblock(body)
                self.emit("")
            return nxt

        # Unknown directive: keep the argument as a paragraph and render body.
        if arg:
            self.emit(self.inline(arg))
            self.emit("")
        if any(x.strip() for x in body):
            self.render_subblock(body)
            self.emit("")
        return nxt


def convert_page(docname: str, text: str, doc_titles: dict[str, str], label_map: dict[str, dict]) -> str:
    return RstConverter(docname, text, doc_titles, label_map).run()


def build_page_markdown(docname: str, body: str) -> str:
    url = f"{SITE}/{docname}"
    return f"*Source: [{url}]({url})*\n\n{body}"


# ---------------------------------------------------------------------------
# toctree parsing (for the README tree)
# ---------------------------------------------------------------------------


def parse_toctrees(text: str) -> list[dict]:
    """Parse every `.. toctree::` in a page.

    Returns a list of {"caption": str|None, "entries": [(title, target)]} where
    target is a docname (internal) or a URL (external, when it contains '://').
    """
    lines = [expand(line) for line in text.split("\n")]
    trees: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        m = DIRECTIVE_RE.match(lines[i])
        if not m or m.group(2).lower() != "toctree":
            i += 1
            continue
        marker_indent = len(m.group(1))
        caption = None
        i += 1
        entries: list[tuple[str | None, str]] = []
        while i < n:
            line = lines[i]
            if not line.strip():
                i += 1
                continue
            if indent_of(line) <= marker_indent:
                break
            s = line.strip()
            opt = re.match(r"^:(\w+):\s*(.*)$", s)
            if opt:
                if opt.group(1) == "caption":
                    caption = opt.group(2).strip()
                i += 1
                continue
            em = re.match(r"^(.*?)\s*<([^>]+)>$", s)
            if em:
                entries.append((em.group(1).strip(), em.group(2).strip()))
            else:
                entries.append((None, s))
            i += 1
        trees.append({"caption": caption, "entries": entries})
    return trees


def build_readme(docnames: list[str], sources: dict[str, str], doc_titles: dict[str, str]) -> str:
    # child docname -> parsed toctrees of that page
    tree_of: dict[str, list[dict]] = {dn: parse_toctrees(sources[dn]) for dn in docnames if dn in sources}

    lines = ["# OBS Studio Documentation", ""]
    project = "OBS Studio"
    lines.append(f"API and developer documentation for {project}, mirrored from [{SITE}]({SITE}).")
    lines.append("")

    reachable: set[str] = set()

    def title_for(target: str, explicit: str | None) -> str:
        if explicit:
            return explicit
        return doc_titles.get(target, target)

    def render_entries(entries: list[tuple[str | None, str]], depth: int) -> None:
        pad = "  " * depth
        for explicit, target in entries:
            if "://" in target:
                lines.append(f"{pad}- [{explicit or target}]({target})")
                continue
            page = target.lstrip("/")
            reachable.add(page)
            title = title_for(page, explicit)
            lines.append(f"{pad}- [{title}]({page}.md)")
            for sub in tree_of.get(page, []):
                render_entries(sub["entries"], depth + 1)

    root_trees = tree_of.get("index", [])
    for tree in root_trees:
        if tree["caption"]:
            lines.append(f"## {tree['caption']}")
            lines.append("")
        render_entries(tree["entries"], 0)
        lines.append("")

    # index itself is reachable (the root page)
    reachable.add("index")
    orphans = [dn for dn in docnames if dn not in reachable]
    if orphans:
        lines.append("## Other")
        lines.append("")
        for dn in sorted(orphans):
            lines.append(f"- [{doc_titles.get(dn, dn)}]({dn}.md)")
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print("Discovering pages from searchindex.js...")
    docnames = discover_docnames()
    print(f"  pages: {len(docnames)}")

    print(f"Fetching {len(docnames)} RST sources (concurrency={MAX_WORKERS})...")
    sources: dict[str, str] = {}
    missing: list[str] = []

    def fetch_one(dn: str) -> tuple[str, str | None]:
        return dn, fetch_url(SOURCE_URL.format(docname=dn))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, dn) for dn in docnames]
        for fut in as_completed(futures):
            dn, content = fut.result()
            if content is None:
                missing.append(dn)
            else:
                sources[dn] = content

    print(f"  fetched: {len(sources)}")
    if missing:
        print(f"  unavailable (.rst.txt 404): {len(missing)}")
        if args.verbose:
            for dn in sorted(missing):
                print(f"    SKIP {dn}")

    print("Building cross-reference metadata...")
    doc_titles, label_map = collect_metadata(sources)
    print(f"  titles: {len(doc_titles)}  labels: {len(label_map)}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}

    for dn in docnames:
        text = sources.get(dn)
        if text is None:
            previous = cache.get(dn)
            previous_path = os.path.join(DOCS_DIR, f"{dn}.md")
            if previous and os.path.exists(previous_path):
                new_cache[dn] = previous
            continue
        body = convert_page(dn, text, doc_titles, label_map)
        content = build_page_markdown(dn, body)
        file_path = os.path.join(DOCS_DIR, f"{dn}.md")
        content_hash = sha256(content)

        prev = cache.get(dn, {})
        if prev.get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[dn] = prev
            continue
        is_new = dn not in cache or not os.path.exists(file_path)
        write_file(
            file_path,
            content,
            dry_run=args.dry_run,
            verbose=args.verbose,
            label="ADD" if is_new else "UPDATE",
        )
        new_cache[dn] = {
            "sha256": content_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Top-level README (catalogue built from the toctree graph)
    readme = build_readme(docnames, sources, doc_titles)
    readme_path = os.path.join(DOCS_DIR, "README.md")
    readme_key = "__readme__"
    readme_hash = sha256(readme)
    prev = cache.get(readme_key, {})
    if prev.get("sha256") == readme_hash and os.path.exists(readme_path):
        unchanged += 1
        new_cache[readme_key] = prev
    else:
        is_new = readme_key not in cache or not os.path.exists(readme_path)
        write_file(
            readme_path,
            readme,
            dry_run=args.dry_run,
            verbose=args.verbose,
            label="ADD" if is_new else "UPDATE",
        )
        new_cache[readme_key] = {
            "sha256": readme_hash,
            "last_updated": datetime.now(UTC).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Removals
    removed = 0
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        old_path = readme_path if old_key == "__readme__" else os.path.join(DOCS_DIR, f"{old_key}.md")
        if not os.path.exists(old_path):
            continue
        if args.dry_run:
            print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        else:
            os.remove(old_path)
            if args.verbose:
                print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        removed += 1

    if not args.dry_run:
        save_cache(new_cache)

    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Unavailable: {len(missing)}")
    print(f"  Total pages: {len(sources)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch OBS Studio documentation and mirror to local markdown"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
