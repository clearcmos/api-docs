#!/usr/bin/env python3

"""
Spotify Web API Documentation Fetcher

Two sources, both authoritative:

1. The official OpenAPI 3.0 spec at /reference/web-api/open-api-schema.yaml
   (the one YAML path robots.txt explicitly allows), converted into per-tag
   endpoint markdown. Endpoint files are named after the operationId, which is
   also the slug the vendor docs use, so docs/reference/albums/get-an-album.md
   corresponds to /documentation/web-api/reference/get-an-album.

2. The guide pages (concepts, tutorials, howtos, monthly change notes).
   developer.spotify.com is a Next.js site serving docs from a
   /documentation/[...mdx] catch-all route, and its page-data endpoint returns
   the compiled MDX for each page. That compiled JS is a far better source than
   the rendered HTML, whose class names are styled-components hashes that change
   every build. Code blocks arrive as Code Hike token streams.

Both sources honour If-None-Match, so a routine sync transfers almost nothing:
unchanged pages answer 304 and the recorded outputs are kept as-is.
"""

import argparse
import concurrent.futures
import contextlib
import gzip
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from typing import NoReturn
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import yaml  # the Spotify spec is published as YAML only

SITE = "https://developer.spotify.com"
SPEC_URL = f"{SITE}/reference/web-api/open-api-schema.yaml"
ENTRY_PATH = "/documentation/web-api"
ENTRY_URL = SITE + ENTRY_PATH
REFERENCE_PATH = ENTRY_PATH + "/reference"
USER_AGENT = "spotify-api-docs-fetcher/1.0"
MAX_WORKERS = 30
TIMEOUT = 60

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SPEC_FILE = os.path.join(SCRIPT_DIR, "openapi.yaml")

SPEC_KEY = "__spec__"
REFERENCE_DIR = "reference"
HTTP_METHODS = ("get", "post", "put", "patch", "delete", "head", "options")
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetcher_sha() -> str:
    """Hash of this script, so a converter change invalidates cached outputs."""
    with open(os.path.abspath(__file__), "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


FETCHER_SHA = fetcher_sha()


def lower_headers(headers) -> dict:
    """HTTP/1.1 and HTTP/2 disagree on header case; normalise before lookups."""
    return {k.lower(): v for k, v in headers.items()}


def http_get(
    url: str,
    etag: str | None = None,
    timeout: int = TIMEOUT,
    retries: int = 4,
    label: str | None = None,
) -> tuple[int, str | None, dict]:
    """GET with gzip, optional conditional request and bounded backoff.

    Returns (status, body, headers) with header names lowercased. Status 0 means
    the request failed; 304 means the caller's etag is still current and body is
    None.
    """
    headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    if etag:
        headers["If-None-Match"] = etag

    delay = 1.0
    last_error = "unknown error"
    for attempt in range(retries + 1):
        try:
            with urlopen(Request(url, headers=headers), timeout=timeout) as resp:
                data = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    data = gzip.decompress(data)
                return resp.status, data.decode("utf-8"), lower_headers(resp.headers)
        except HTTPError as e:
            if e.code == 304:
                return 304, None, lower_headers(e.headers)
            if e.code < 500 and e.code != 429:
                print(f"ERROR: HTTP {e.code} for {label or url}", file=sys.stderr)
                return e.code, None, {}
            last_error = f"HTTP {e.code}"
        except (URLError, TimeoutError, OSError) as e:
            last_error = str(e)

        if attempt == retries:
            print(f"ERROR: Failed to fetch {label or url}: {last_error}", file=sys.stderr)
            return 0, None, {}
        time.sleep(delay)
        delay = min(delay * 2, 8.0)

    return 0, None, {}


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
    tmp = CACHE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CACHE_FILE)


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Compiled MDX parsing
#
# next-mdx-remote hands us the output of the MDX compiler: a JS module body
# whose _createMdxContent() returns one _jsx()/_jsxs() expression tree. The
# subset of JS that appears there (string/number/bool literals, arrays, object
# literals, dotted identifiers and _jsx calls) is small enough to parse
# directly, which preserves the original markdown structure exactly.
# ---------------------------------------------------------------------------


class MdxParseError(Exception):
    pass


class Ident:
    """A bare identifier such as _Fragment, CH.Code or chCodeConfig."""

    __slots__ = ("name",)

    def __init__(self, name: str):
        self.name = name


class El:
    """One element of the JSX tree."""

    __slots__ = ("tag", "props", "children")

    def __init__(self, tag: str, props: dict, children: list):
        self.tag = tag
        self.props = props
        self.children = children


ESCAPES = {
    '"': '"',
    "'": "'",
    "\\": "\\",
    "/": "/",
    "`": "`",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "0": "\0",
    "\n": "",
    "\r": "",
}


class JsExpr:
    """Recursive-descent parser for the compiled-MDX JS expression subset."""

    IDENT_RE = re.compile(r"[A-Za-z_$][A-Za-z0-9_$]*(?:\.[A-Za-z_$][A-Za-z0-9_$]*)*")
    NUMBER_RE = re.compile(r"-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?")

    def __init__(self, src: str, pos: int = 0):
        self.s = src
        self.i = pos

    def fail(self, msg: str) -> NoReturn:
        context = self.s[max(0, self.i - 40) : self.i + 40]
        raise MdxParseError(f"{msg} at offset {self.i}: {context!r}")

    def ws(self):
        while self.i < len(self.s) and self.s[self.i] in " \t\r\n":
            self.i += 1

    def at(self, ch: str) -> bool:
        self.ws()
        return self.i < len(self.s) and self.s[self.i] == ch

    def expect(self, ch: str):
        if not self.at(ch):
            self.fail(f"expected {ch!r}")
        self.i += 1

    def value(self):
        self.ws()
        if self.i >= len(self.s):
            self.fail("unexpected end of input")
        c = self.s[self.i]
        if c in "\"'":
            return self.string()
        if c == "{":
            return self.object()
        if c == "[":
            return self.array()
        if c == "-" or c.isdigit():
            return self.number()

        m = self.IDENT_RE.match(self.s, self.i)
        if not m:
            self.fail("unexpected token")
        name = m.group(0)
        self.i = m.end()
        if self.at("("):
            return self.call(name)
        if name == "true":
            return True
        if name == "false":
            return False
        if name in ("null", "undefined"):
            return None
        return Ident(name)

    def string(self) -> str:
        quote_char = self.s[self.i]
        self.i += 1
        out = []
        while True:
            if self.i >= len(self.s):
                self.fail("unterminated string")
            c = self.s[self.i]
            if c == "\\":
                self.i += 1
                if self.i >= len(self.s):
                    self.fail("unterminated escape")
                e = self.s[self.i]
                if e == "u":
                    if self.s[self.i + 1 : self.i + 2] == "{":
                        end = self.s.index("}", self.i)
                        out.append(chr(int(self.s[self.i + 2 : end], 16)))
                        self.i = end + 1
                    else:
                        out.append(chr(int(self.s[self.i + 1 : self.i + 5], 16)))
                        self.i += 5
                elif e == "x":
                    out.append(chr(int(self.s[self.i + 1 : self.i + 3], 16)))
                    self.i += 3
                else:
                    out.append(ESCAPES.get(e, e))
                    self.i += 1
                continue
            if c == quote_char:
                self.i += 1
                return self._join_surrogates(out)
            out.append(c)
            self.i += 1

    @staticmethod
    def _join_surrogates(chunks: list[str]) -> str:
        text = "".join(chunks)
        if any(0xD800 <= ord(ch) <= 0xDFFF for ch in text):
            with contextlib.suppress(UnicodeError):
                text = text.encode("utf-16", "surrogatepass").decode("utf-16")
        return text

    def number(self):
        m = self.NUMBER_RE.match(self.s, self.i)
        if not m:
            self.fail("bad number")
        self.i = m.end()
        text = m.group(0)
        return float(text) if re.search(r"[.eE]", text) else int(text)

    def array(self) -> list:
        self.expect("[")
        items: list = []
        while True:
            if self.at("]"):
                self.i += 1
                return items
            items.append(self.value())
            if self.at(","):
                self.i += 1
            elif self.at("]"):
                self.i += 1
                return items
            else:
                self.fail("expected ',' or ']'")

    def object(self) -> dict:
        self.expect("{")
        obj: dict = {}
        while True:
            if self.at("}"):
                self.i += 1
                return obj
            self.ws()
            if self.s[self.i] in "\"'":
                key = self.string()
            else:
                m = self.IDENT_RE.match(self.s, self.i)
                if not m:
                    self.fail("expected object key")
                key = m.group(0)
                self.i = m.end()
            self.expect(":")
            obj[key] = self.value()
            if self.at(","):
                self.i += 1
            elif self.at("}"):
                self.i += 1
                return obj
            else:
                self.fail("expected ',' or '}'")

    def call(self, name: str):
        self.expect("(")
        args: list = []
        while True:
            if self.at(")"):
                self.i += 1
                break
            args.append(self.value())
            if self.at(","):
                self.i += 1
            elif self.at(")"):
                self.i += 1
                break
            else:
                self.fail("expected ',' or ')'")
        if name in ("_jsx", "_jsxs"):
            return self.element(args)
        return Ident(name)

    @staticmethod
    def element(args: list) -> El:
        tag = args[0] if args else None
        props = args[1] if len(args) > 1 and isinstance(args[1], dict) else {}
        if isinstance(tag, Ident):
            name = tag.name
            if name in ("_Fragment", "Fragment"):
                name = "#fragment"
            elif name.startswith("_components."):
                name = name.split(".", 1)[1]
        elif isinstance(tag, str):
            name = tag
        else:
            name = "#unknown"
        children = props.pop("children", None)
        if children is None:
            children = []
        elif not isinstance(children, list):
            children = [children]
        return El(name, dict(props), children)


RETURN_RE = re.compile(r"\breturn\s+(_jsxs?)\s*\(")


def parse_compiled_mdx(compiled: str) -> El:
    start = compiled.find("function _createMdxContent")
    m = RETURN_RE.search(compiled, start if start >= 0 else 0)
    if not m:
        raise MdxParseError("no JSX return expression found")
    node = JsExpr(compiled, m.start(1)).value()
    if not isinstance(node, El):
        raise MdxParseError("compiled MDX did not yield an element tree")
    return node


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------

INLINE_TAGS = {
    "a",
    "code",
    "strong",
    "b",
    "em",
    "i",
    "del",
    "s",
    "br",
    "input",
    "img",
    "span",
    "sup",
    "sub",
    "kbd",
    "abbr",
    "CH.SectionLink",
}
LIST_START_RE = re.compile(r"[ \t]*(?:[-*+] |\d+\. )")
CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s*")


def prefix_lines(text: str, prefix: str) -> str:
    return "\n".join(prefix + line if line.strip() else prefix.rstrip() for line in text.split("\n"))


def collapse_ws(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def emphasis(text: str, marker: str) -> str:
    stripped = text.strip()
    if not stripped:
        return text
    lead = text[: len(text) - len(text.lstrip())]
    trail = text[len(text.rstrip()) :]
    return f"{lead}{marker}{stripped}{marker}{trail}"


def code_span(text: str) -> str:
    text = re.sub(r"\s*\n\s*", " ", text)
    if not text:
        return ""
    ticks = "`"
    while ticks in text:
        ticks += "`"
    pad = " " if text.startswith("`") or text.endswith("`") else ""
    return f"{ticks}{pad}{text}{pad}{ticks}"


def fence(text: str, lang: str) -> str:
    bars = "```"
    while bars in text:
        bars += "`"
    return f"{bars}{lang}\n{text}\n{bars}"


def plain_text(nodes: list) -> str:
    out = []
    for n in nodes:
        if isinstance(n, str):
            out.append(n)
        elif isinstance(n, El):
            out.append(plain_text(n.children))
    return "".join(out)


class MdxRenderer:
    """Renders a parsed compiled-MDX tree to markdown."""

    def __init__(self, resolve_link, unknown: dict):
        self.resolve = resolve_link
        self.unknown = unknown
        self.section_files: list[list] = []

    def document(self, root: El) -> str:
        blocks = [b for b in self.blocks([root]) if b.strip()]
        return "\n\n".join(blocks)

    # -- block level ------------------------------------------------------

    def blocks(self, nodes: list) -> list[str]:
        out: list[str] = []
        buf: list[str] = []

        def flush():
            if not buf:
                return
            text = "".join(buf).strip()
            buf.clear()
            if text:
                out.append(re.sub(r"[ \t]+\n", "\n", text))

        for n in nodes:
            if isinstance(n, str):
                if n.strip() == "":
                    if "\n" in n:
                        flush()
                    elif buf:
                        buf.append(n)
                else:
                    buf.append(n)
                continue
            if not isinstance(n, El):
                continue
            if n.tag in INLINE_TAGS:
                buf.append(self.inline([n]))
                continue
            flush()
            out.extend(self.block(n))

        flush()
        return out

    def block(self, el: El) -> list[str]:
        tag = el.tag

        if tag == "style":
            return []
        if tag in ("#fragment", "section", "div", "article", "main", "figure"):
            return self.blocks(el.children)
        if tag == "p":
            text = "".join(self.inline(el.children)).strip()
            return [re.sub(r"[ \t]+\n", "\n", text)] if text else []
        if tag in HEADING_TAGS:
            text = collapse_ws(self.inline(el.children))
            return [f"{'#' * int(tag[1])} {text}"] if text else []
        if tag == "ul":
            return [self.list_block(el, ordered=False)]
        if tag == "ol":
            return [self.list_block(el, ordered=True)]
        if tag == "table":
            table = self.table_block(el)
            return [table] if table else []
        if tag == "blockquote":
            inner = "\n\n".join(b for b in self.blocks(el.children) if b.strip())
            return [prefix_lines(inner, "> ")] if inner else []
        if tag == "hr":
            return ["---"]
        if tag == "pre":
            return [self.pre_block(el)]
        if tag == "details":
            return self.details_block(el)
        if tag == "Banner":
            return [self.banner_block(el)]
        if tag == "CH.Code":
            return self.code_blocks(el.props.get("files"))
        if tag == "CH.Section":
            self.section_files.append(el.props.get("files") or [])
            try:
                return self.blocks(el.children)
            finally:
                self.section_files.pop()
        if tag == "CH.SectionCode":
            return self.code_blocks(self.section_files[-1]) if self.section_files else []
        if tag in ("li", "tr", "td", "th", "thead", "tbody", "tfoot"):
            # Only reachable if the source nests these oddly; keep the content.
            return self.blocks(el.children)

        self.unknown[tag] = self.unknown.get(tag, 0) + 1
        return self.blocks(el.children)

    def list_block(self, el: El, ordered: bool) -> str:
        items = [c for c in el.children if isinstance(c, El) and c.tag == "li"]
        rendered = []
        for index, li in enumerate(items, 1):
            marker = f"{index}. " if ordered else "- "
            blocks = [b for b in self.blocks(li.children) if b.strip()]
            if not blocks:
                continue
            body = blocks[0]
            for nxt in blocks[1:]:
                body += ("\n" if LIST_START_RE.match(nxt) else "\n\n") + nxt
            body = CHECKBOX_RE.sub(lambda m: f"[{m.group(1).lower()}] ", body, count=1)
            pad = " " * len(marker)
            lines = body.split("\n")
            out = marker + lines[0]
            for line in lines[1:]:
                out += "\n" + (pad + line if line.strip() else "")
            rendered.append(out)
        return "\n".join(rendered)

    def table_block(self, el: El) -> str:
        header: list[str] | None = None
        rows: list[list[str]] = []

        def walk(node: El):
            nonlocal header
            for child in node.children:
                if not isinstance(child, El):
                    continue
                if child.tag == "tr":
                    cells = [
                        self.cell(c) for c in child.children if isinstance(c, El) and c.tag in ("td", "th")
                    ]
                    is_header = any(isinstance(c, El) and c.tag == "th" for c in child.children)
                    if is_header and header is None and not rows:
                        header = cells
                    else:
                        rows.append(cells)
                elif child.tag in ("thead", "tbody", "tfoot"):
                    walk(child)

        walk(el)
        if header is None:
            if not rows:
                return ""
            header, rows = rows[0], rows[1:]

        width = max([len(header)] + [len(r) for r in rows])

        def pad(row: list[str]) -> list[str]:
            return row + [""] * (width - len(row))

        lines = [
            "| " + " | ".join(pad(header)) + " |",
            "|" + "|".join([" --- "] * width) + "|",
        ]
        for row in rows:
            lines.append("| " + " | ".join(pad(row)) + " |")
        return "\n".join(lines)

    def cell(self, el: El) -> str:
        text = re.sub(r"\s*\n\s*", " ", self.inline(el.children)).strip()
        return text.replace("|", "\\|")

    def pre_block(self, el: El) -> str:
        code_el = next((c for c in el.children if isinstance(c, El) and c.tag == "code"), None)
        source = code_el if code_el is not None else el
        class_name = source.props.get("className")
        lang = ""
        if isinstance(class_name, str):
            m = re.search(r"language-([\w+#-]+)", class_name)
            if m:
                lang = m.group(1)
        return fence(plain_text(source.children).strip("\n"), lang)

    def details_block(self, el: El) -> list[str]:
        summary = next((c for c in el.children if isinstance(c, El) and c.tag == "summary"), None)
        out = []
        if summary is not None:
            label = collapse_ws(self.inline(summary.children))
            if label:
                out.append(f"**{label}**")
        rest = [c for c in el.children if c is not summary]
        out.extend(self.blocks(rest))
        return out

    def banner_block(self, el: El) -> str:
        color = el.props.get("color")
        kind = {
            "warning": "WARNING",
            "danger": "CAUTION",
            "error": "CAUTION",
            "critical": "CAUTION",
            "success": "TIP",
            "tip": "TIP",
            "important": "IMPORTANT",
            "info": "NOTE",
        }.get(color.lower() if isinstance(color, str) else "", "NOTE")
        inner = "\n\n".join(b for b in self.blocks(el.children) if b.strip())
        body = f"[!{kind}]\n{inner}" if inner else f"[!{kind}]"
        return prefix_lines(body, "> ")

    def code_blocks(self, files) -> list[str]:
        out: list[str] = []
        for f in files or []:
            if not isinstance(f, dict):
                continue
            code = f.get("code") or {}
            lines = []
            for line in code.get("lines") or []:
                tokens = line.get("tokens") or [] if isinstance(line, dict) else []
                lines.append("".join(str(t.get("content", "")) for t in tokens if isinstance(t, dict)))
            text = "\n".join(lines).strip("\n")
            if not text.strip():
                continue
            lang = str(code.get("lang") or "").strip()
            if lang in ("text", "txt", "plaintext"):
                lang = ""
            name = str(f.get("name") or "").strip()
            if name:
                out.append(f"**{name}**")
            out.append(fence(text, lang))
        return out

    # -- inline level -----------------------------------------------------

    def inline(self, nodes: list) -> str:
        parts: list[str] = []
        for n in nodes:
            if isinstance(n, str):
                parts.append(n)
                continue
            if not isinstance(n, El):
                continue
            tag = n.tag
            if tag == "a":
                href = n.props.get("href")
                label = self.inline(n.children).strip()
                target = self.resolve(href) if isinstance(href, str) else ""
                if not label:
                    label = target
                parts.append(f"[{label}]({target})" if target else label)
            elif tag == "code":
                parts.append(code_span(self.inline(n.children)))
            elif tag in ("strong", "b"):
                parts.append(emphasis(self.inline(n.children), "**"))
            elif tag in ("em", "i"):
                parts.append(emphasis(self.inline(n.children), "*"))
            elif tag in ("del", "s"):
                parts.append(emphasis(self.inline(n.children), "~~"))
            elif tag == "br":
                parts.append("\n")
            elif tag == "img":
                src = n.props.get("src")
                alt = str(n.props.get("alt") or "").strip()
                target = self.resolve(src) if isinstance(src, str) else ""
                parts.append(f"![{alt}]({target})" if target else alt)
            elif tag == "input":
                if n.props.get("type") == "checkbox":
                    parts.append("[x]" if n.props.get("checked") else "[ ]")
            elif tag == "style":
                continue
            else:
                parts.append(self.inline(n.children))
        return "".join(parts)


# ---------------------------------------------------------------------------
# Link resolution
# ---------------------------------------------------------------------------


def relative_link(target: str, from_dir: str) -> str:
    rel = os.path.relpath(target, from_dir) if from_dir else target
    return rel if rel.startswith(".") else "./" + rel


def make_resolver(from_dir: str, local_paths: dict[str, str]):
    """Resolve an href against the generated tree.

    Site-relative links to pages we mirror become relative file links; anything
    else on developer.spotify.com is absolutised.
    """

    def resolve(href: str | None) -> str:
        if not href:
            return ""
        if href.startswith(("#", "http://", "https://", "mailto:", "tel:")):
            return href
        if not href.startswith("/"):
            return href
        path, _, fragment = href.partition("#")
        key = path.rstrip("/") or "/"
        target = local_paths.get(key)
        if target:
            link = relative_link(target, from_dir)
            return f"{link}#{fragment}" if fragment else link
        absolute = SITE + href
        return absolute.replace(" ", "%20")

    return resolve


MD_LINK_RE = re.compile(r"(\[[^\]]*\]\()(/[^)\s]*)(\))")


def rewrite_markdown_links(text: str, resolve) -> str:
    return MD_LINK_RE.sub(lambda m: m.group(1) + resolve(m.group(2)) + m.group(3), text)


HTML_ANCHOR_RE = re.compile(r"<a\s+[^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", re.S)


def clean_spec_text(text: str | None, resolve, inline: bool = False) -> str:
    """Normalise the light HTML the Spotify spec mixes into its markdown."""
    if not text:
        return ""
    out = HTML_ANCHOR_RE.sub(lambda m: f"[{collapse_ws(m.group(2))}]({resolve(m.group(1))})", text)
    out = re.sub(r"</?p>", "", out)
    out = re.sub(r"<br\s*/?>", "\n", out, flags=re.I)
    out = rewrite_markdown_links(out, resolve)
    if inline:
        return re.sub(r"\s*\n\s*", " ", out).strip().replace("|", "\\|")
    return "\n".join(line.rstrip() for line in out.strip().split("\n"))


# ---------------------------------------------------------------------------
# OpenAPI to markdown
# ---------------------------------------------------------------------------


def resolve_ref(ref: str, spec: dict):
    """Resolve a local $ref pointer such as '#/components/schemas/AlbumObject'."""
    if not ref.startswith("#/"):
        return {}
    node: object = spec
    for part in ref[2:].split("/"):
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return {}
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return {}
        if node is None:
            return {}
    return node if isinstance(node, dict) else {}


def deref(node, spec: dict):
    if isinstance(node, dict) and "$ref" in node:
        return resolve_ref(node["$ref"], spec) or {}
    return node


def resolve_server_url(spec: dict) -> str:
    servers = spec.get("servers") or []
    if not servers:
        return ""
    server = servers[0]
    url = str(server.get("url", ""))
    for var, info in (server.get("variables") or {}).items():
        url = url.replace("{" + var + "}", str(info.get("default", "")))
    return url


def flatten_all_of(schema: dict, spec: dict) -> dict:
    """Merge an allOf chain (which Spotify nests, e.g. paging objects) into one schema."""
    merged: dict = {}
    props: dict = {}
    required: list = []
    visited: set[str] = set()

    def visit(node):
        if isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if ref in visited:
                return
            visited.add(ref)
        node = deref(node, spec)
        if not isinstance(node, dict):
            return
        for sub in node.get("allOf") or []:
            visit(sub)
        for key, value in node.items():
            if key in ("allOf", "properties", "required"):
                continue
            merged[key] = value
        props.update(node.get("properties") or {})
        for name in node.get("required") or []:
            if name not in required:
                required.append(name)

    visit(schema)
    if props:
        merged["properties"] = props
    if required:
        merged["required"] = required
    merged.setdefault("type", "object")
    return merged


def schema_parts(
    schema, spec: dict, resolve, depth: int = 0, seen: set | None = None
) -> tuple[str, list[str]]:
    """Render a schema as (inline type label, indented property lines)."""
    if not isinstance(schema, dict):
        return "any", []
    if seen is None:
        seen = set()

    if "$ref" in schema:
        ref = schema["$ref"]
        if ref in seen:
            return "`(circular reference)`", []
        seen = seen | {ref}
        resolved = resolve_ref(ref, spec)
        if not isinstance(resolved, dict) or not resolved:
            return f"`{ref.split('/')[-1]}`", []
        schema = resolved

    if schema.get("allOf"):
        return schema_parts(flatten_all_of(schema, spec), spec, resolve, depth, seen)

    variants = schema.get("oneOf") or schema.get("anyOf")
    if variants:
        label = "One of" if schema.get("oneOf") else "Any of"
        parts = [schema_parts(v, spec, resolve, depth, seen)[0] for v in variants[:5]]
        if len(variants) > 5:
            parts.append(f"... and {len(variants) - 5} more")
        return f"{label}: " + " | ".join(parts), []

    schema_type = schema.get("type", "")

    if schema_type == "array":
        item_label, item_lines = schema_parts(schema.get("items") or {}, spec, resolve, depth, seen)
        return f"array of {item_label}", item_lines

    if schema_type == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = set(schema.get("required") or [])
        if not props:
            additional = schema.get("additionalProperties")
            if isinstance(additional, dict):
                inner, _ = schema_parts(additional, spec, resolve, depth + 1, seen)
                return f"object (values: {inner})", []
            return "object", []
        if depth > 1:
            return f"object ({len(props)} properties)", []

        lines = []
        for name, prop in props.items():
            prop = prop if isinstance(prop, dict) else {}
            req = " **required**" if name in required else ""
            prop_label, prop_lines = schema_parts(prop, spec, resolve, depth + 1, seen)
            desc = clean_spec_text(deref(prop, spec).get("description"), resolve, inline=True)
            entry = f"- `{name}` ({prop_label}){req}"
            if desc:
                entry += f": {desc}"
            lines.append(entry)
            lines.extend("  " + line for line in prop_lines)
        return "object", lines

    if schema_type:
        result = schema_type
        if schema.get("format"):
            result += f" ({schema['format']})"
        enum = schema.get("enum")
        if enum:
            values = ", ".join(f"`{e}`" for e in enum[:10])
            if len(enum) > 10:
                values += f", ... ({len(enum)} total)"
            result += f" - enum: {values}"
        return result, []

    return "any", []


def schema_to_markdown(schema, spec: dict, resolve) -> str:
    label, lines = schema_parts(schema, spec, resolve)
    if not lines:
        return label
    return label + "\n\n" + "\n".join(lines)


def format_parameters(parameters: list, spec: dict, resolve) -> list[str]:
    rows = []
    for param in parameters:
        param = deref(param, spec)
        if not isinstance(param, dict):
            continue
        schema = deref(param.get("schema") or {}, spec)
        param_type = schema.get("type", "string")
        if schema.get("format"):
            param_type += f" ({schema['format']})"
        if schema.get("enum"):
            param_type += " enum"
        default = schema.get("default")
        description = clean_spec_text(
            param.get("description") or schema.get("description"), resolve, inline=True
        )
        if default is not None:
            description = (description + " " if description else "") + f"Default: `{default}`."
        rows.append(
            f"| `{param.get('name', '')}` | {param.get('in', '')} | {param_type} | "
            f"{'Yes' if param.get('required') else 'No'} | {description} |"
        )
    if not rows:
        return []
    return [
        "### Parameters",
        "\n".join(["| Name | In | Type | Required | Description |", "|---|---|---|---|---|"] + rows),
    ]


def format_request_body(request_body, spec: dict, resolve) -> list[str]:
    request_body = deref(request_body, spec)
    if not isinstance(request_body, dict) or not request_body:
        return []

    out = ["### Request body"]
    description = clean_spec_text(request_body.get("description"), resolve)
    if description:
        out.append(description)
    out.append(f"**Required:** {'Yes' if request_body.get('required') else 'No'}")

    for content_type, media in (request_body.get("content") or {}).items():
        media = media if isinstance(media, dict) else {}
        out.append(f"**Content type:** `{content_type}`")
        schema = media.get("schema")
        if schema:
            out.append(schema_to_markdown(schema, spec, resolve))
        example = media.get("example") or (deref(schema or {}, spec) or {}).get("example")
        if example is not None:
            out.append("**Example:**")
            out.append(fence(json.dumps(example, indent=2), "json"))
    return out


def format_responses(responses: dict, spec: dict, resolve) -> list[str]:
    if not responses:
        return []
    out = ["### Responses"]
    for status in sorted(responses, key=lambda c: (len(str(c)), str(c))):
        response = deref(responses[status], spec)
        if not isinstance(response, dict):
            continue
        out.append(f"#### {status}")
        description = clean_spec_text(response.get("description"), resolve)
        if description:
            out.append(description)
        for content_type, media in (response.get("content") or {}).items():
            media = media if isinstance(media, dict) else {}
            schema = media.get("schema")
            out.append(f"**Content type:** `{content_type}`")
            if schema:
                out.append(schema_to_markdown(schema, spec, resolve))
    return out


def operation_scopes(operation: dict) -> list[str]:
    scopes: list[str] = []
    for entry in operation.get("security") or []:
        for names in (entry or {}).values():
            for scope in names or []:
                if scope not in scopes:
                    scopes.append(scope)
    return scopes


def operation_policies(operation: dict, spec: dict) -> list[str]:
    node = operation.get("x-spotify-policy-list")
    if isinstance(node, dict) and "$ref" in node:
        node = resolve_ref(node["$ref"], spec)
    names = []
    for item in node or []:
        if isinstance(item, dict) and "$ref" in item:
            names.append(item["$ref"].rsplit("/", 1)[-1])
        elif isinstance(item, str):
            names.append(item)
    return names


def op_title(operation: dict, method: str, path: str) -> str:
    summary = operation.get("summary")
    if summary:
        return collapse_ws(summary)
    return f"{method.upper()} {path}"


def build_endpoint_markdown(
    endpoint: dict,
    spec: dict,
    policy_refs: dict,
    local_paths: dict[str, str],
    deprecation_note: str,
) -> str:
    operation = endpoint["operation"]
    path = endpoint["path"]
    method = endpoint["method"]
    from_dir = endpoint["dir"]
    resolve = make_resolver(from_dir, local_paths)

    lines = [f"# {endpoint['title']}", ""]
    doc_url = f"{SITE}{REFERENCE_PATH}/{endpoint['operation_id']}" if endpoint["operation_id"] else ""
    if doc_url:
        lines.append(f"**Source:** {doc_url}")
        lines.append("")

    if operation.get("deprecated"):
        lines.append(prefix_lines(f"[!WARNING]\n{deprecation_note}", "> "))
        lines.append("")

    description = clean_spec_text(operation.get("description"), resolve)
    if description:
        lines.append(description)
        lines.append("")

    lines.append("## Request")
    lines.append("")
    lines.append(f"**Method:** `{method.upper()}`")
    lines.append("")
    lines.append(f"**Path:** `{path}`")
    lines.append("")
    base_url = resolve_server_url(spec)
    if base_url:
        lines.append(f"**Full URL:** `{base_url}{path}`")
        lines.append("")
    if endpoint["operation_id"]:
        lines.append(f"**Operation ID:** `{endpoint['operation_id']}`")
        lines.append("")
    tags = operation.get("tags") or []
    if tags:
        lines.append("**Tags:** " + ", ".join(f"`{t}`" for t in tags))
        lines.append("")

    scopes = operation_scopes(operation)
    lines.append("### Authorization")
    lines.append("")
    lines.append("A valid OAuth 2.0 access token is required.")
    lines.append("")
    if scopes:
        lines.append("**Scopes:** " + ", ".join(f"`{s}`" for s in scopes))
    else:
        lines.append("**Scopes:** none")
    lines.append("")

    for section in (
        format_parameters(operation.get("parameters") or [], spec, resolve)
        + format_request_body(operation.get("requestBody"), spec, resolve)
        + format_responses(operation.get("responses") or {}, spec, resolve)
    ):
        lines.append(section)
        lines.append("")

    policies = operation_policies(operation, spec)
    if policies:
        lines.append("### Policy considerations")
        lines.append("")
        for name in policies:
            info = policy_refs.get(name) or {}
            title = collapse_ws(info.get("title") or name)
            entry = f"- **{title}**"
            detail = collapse_ws(info.get("description") or "")
            if detail:
                entry += f": {detail}"
            url = info.get("url")
            if isinstance(url, str) and url:
                target = (SITE + url if url.startswith("/") else url).replace(" ", "%20")
                entry += f" ([policy]({target}))"
            lines.append(entry)
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def build_tag_readme(tag: str, endpoints: list[dict], tag_description: str) -> str:
    lines = [f"# {tag}", ""]
    if tag_description:
        lines.append(tag_description)
        lines.append("")
    lines.append(f"{plural(len(endpoints), 'endpoint')}.")
    lines.append("")
    for ep in endpoints:
        suffix = " (deprecated)" if ep["operation"].get("deprecated") else ""
        lines.append(f"- [{ep['title']}](./{ep['filename']}) - `{ep['method'].upper()} {ep['path']}`{suffix}")
    lines.append("")
    return "\n".join(lines)


def plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def build_reference_readme(groups: dict, info: dict, base_url: str, deprecated: int, total: int) -> str:
    lines = [
        "# Spotify Web API Reference",
        "",
        f"Generated from the official OpenAPI {info.get('openapi', '3.0')} spec.",
        "",
        f"**API version:** {info.get('version', '?')}",
        "",
        f"**Base URL:** `{base_url}`",
        "",
        f"**Spec:** {SPEC_URL}",
        "",
        f"{plural(total, 'endpoint')} in {plural(len(groups), 'group')}, {deprecated} of them deprecated.",
        "",
        "## Groups",
        "",
    ]
    for tag in sorted(groups):
        group = groups[tag]
        lines.append(f"- [{tag}](./{group['dir']}/README.md) ({plural(group['count'], 'endpoint')})")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Guide page discovery and conversion
# ---------------------------------------------------------------------------

BUILD_ID_RE = re.compile(r'"buildId":"([^"]+)"')
DOC_LINK_RE = re.compile(r'href="(/documentation/web-api[^"#?]*)"')
COMPILED_LINK_RE = re.compile(r'href: "(/documentation/web-api[^"#?]*)"')

SECTION_TITLES = {
    "": "Overview",
    "concepts": "Concepts",
    "tutorials": "Tutorials",
    "howtos": "How-tos",
    "references/changes": "Change notes",
}


def normalize_path(path: str) -> str:
    return path.rstrip("/") or "/"


def is_reference_path(path: str) -> bool:
    return path == REFERENCE_PATH or path.startswith(REFERENCE_PATH + "/")


def guide_output(path: str) -> str:
    rel = path[len(ENTRY_PATH) :].strip("/")
    return f"{rel}.md" if rel else "index.md"


def guide_section(output: str) -> str:
    return os.path.dirname(output)


def page_data_url(build_id: str, path: str) -> str:
    segments = path[len("/documentation/") :].split("/")
    query = "&".join("mdx=" + quote(s, safe="") for s in segments)
    return f"{SITE}/_next/data/{build_id}{path}.json?{query}"


def fetch_page(path: str, build_id: str, etag: str | None, output_path: str) -> tuple:
    url = page_data_url(build_id, path)
    status, body, headers = http_get(url, etag=etag, label=path)
    if status == 304 and not os.path.exists(output_path):
        # A source-level cache hit only counts when the output still exists.
        status, body, headers = http_get(url, label=path)
    return path, url, status, body, headers


def build_guide_markdown(
    title: str,
    description: str,
    path: str,
    root: El,
    output: str,
    local_paths: dict[str, str],
    unknown: dict,
) -> str:
    resolve = make_resolver(os.path.dirname(output), local_paths)
    body = MdxRenderer(resolve, unknown).document(root)
    lines = [f"# {title}", "", f"**Source:** {SITE}{path}", ""]
    if description:
        lines.append(collapse_ws(description))
        lines.append("")
    lines.append(body)
    return "\n".join(lines).rstrip() + "\n"


def build_top_readme(
    guides: list[dict],
    groups: dict,
    info: dict,
    base_url: str,
    scopes: dict,
    deprecated: int,
    total_endpoints: int,
) -> str:
    lines = [
        "# Spotify Web API Documentation",
        "",
        "Spotify Web API guides and endpoint reference, generated from the official",
        f"OpenAPI spec and the guide pages at {ENTRY_URL}.",
        "",
        f"**API version:** {info.get('version', '?')}",
        "",
        f"**Base URL:** `{base_url}`",
        "",
        "## Authorization",
        "",
        "All requests use OAuth 2.0 access tokens. Authorization code, authorization code",
        f"with PKCE and client credentials flows are supported, with {len(scopes)} scopes",
        "controlling access to user data.",
        "",
        "## Guides",
        "",
    ]

    by_section: dict[str, list[dict]] = {}
    for guide in guides:
        by_section.setdefault(guide["section"], []).append(guide)

    ordered_sections = list(SECTION_TITLES) + sorted(s for s in by_section if s not in SECTION_TITLES)
    for section in ordered_sections:
        pages = by_section.get(section)
        if not pages:
            continue
        lines.append(f"### {SECTION_TITLES.get(section, section)}")
        lines.append("")
        for page in pages:
            lines.append(f"- [{page['title']}](./{page['output']})")
        lines.append("")

    lines.append("## API reference")
    lines.append("")
    lines.append(
        f"{plural(total_endpoints, 'endpoint')} in {plural(len(groups), 'group')} "
        f"({deprecated} deprecated). See [reference/README.md](./{REFERENCE_DIR}/README.md)."
    )
    lines.append("")
    for tag in sorted(groups):
        group = groups[tag]
        lines.append(
            f"- [{tag}](./{REFERENCE_DIR}/{group['dir']}/README.md) ({plural(group['count'], 'endpoint')})"
        )
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------


class Writer:
    """Applies cache decisions and writes output files deterministically."""

    def __init__(self, cache: dict, args: argparse.Namespace):
        self.cache = cache
        self.args = args
        self.new_cache: dict = {}
        self.added = 0
        self.updated = 0
        self.unchanged = 0

    def write(self, key: str, content: str, extra: dict | None = None) -> None:
        target = os.path.join(DOCS_DIR, key)
        content_hash = sha256(content)
        cached = self.cache.get(key, {})
        entry = {"sha256": content_hash, "last_updated": cached.get("last_updated") or now_iso()}
        if extra:
            entry.update(extra)

        if not self.args.force and cached.get("sha256") == content_hash and os.path.exists(target):
            self.unchanged += 1
            self.new_cache[key] = entry
            return

        is_new = key not in self.cache or not os.path.exists(target)
        label = "ADD" if is_new else "UPDATE"
        if self.args.dry_run:
            print(f"  {label} {key}")
        else:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w") as f:
                f.write(content)
            if self.args.verbose:
                print(f"  {label} {key}")
        entry["last_updated"] = now_iso()
        self.new_cache[key] = entry
        if is_new:
            self.added += 1
        else:
            self.updated += 1

    def keep(self, key: str, entry: dict | None = None) -> None:
        """Carry a cache entry forward untouched (unchanged or preserved page)."""
        entry = entry if entry is not None else self.cache.get(key)
        if entry is None:
            return
        self.new_cache[key] = dict(entry)
        self.unchanged += 1


def collect_endpoints(spec: dict) -> tuple[dict, dict, int]:
    """Group operations by tag. Returns (endpoints_by_tag, groups, total)."""
    endpoints_by_tag: dict[str, list[dict]] = {}
    total = 0
    for path, path_item in (spec.get("paths") or {}).items():
        if not isinstance(path_item, dict):
            continue
        for method in HTTP_METHODS:
            operation = path_item.get(method)
            if not isinstance(operation, dict):
                continue
            total += 1
            operation_id = operation.get("operationId") or ""
            slug = sanitize_filename(operation_id) or sanitize_filename(f"{method}-{path}")
            for tag in operation.get("tags") or ["Other"]:
                tag_dir = sanitize_filename(tag)
                endpoints_by_tag.setdefault(tag, []).append(
                    {
                        "path": path,
                        "method": method,
                        "operation": operation,
                        "operation_id": operation_id,
                        "title": op_title(operation, method, path),
                        "filename": f"{slug}.md",
                        "dir": f"{REFERENCE_DIR}/{tag_dir}",
                        "key": f"{REFERENCE_DIR}/{tag_dir}/{slug}.md",
                    }
                )

    for endpoints in endpoints_by_tag.values():
        endpoints.sort(key=lambda e: (e["path"], HTTP_METHODS.index(e["method"])))

    groups = {
        tag: {"dir": sanitize_filename(tag), "count": len(endpoints)}
        for tag, endpoints in endpoints_by_tag.items()
    }
    return endpoints_by_tag, groups, total


def sync(args: argparse.Namespace) -> int:
    # The previous cache is always loaded: --force only disables the
    # unchanged-skip, removal detection still needs the old keys.
    cache = load_cache()
    lookup = {} if args.force else cache

    print("Discovering Spotify Web API doc pages...")
    status, entry_html, _ = http_get(ENTRY_URL, label=ENTRY_URL)
    if not entry_html:
        print("ERROR: could not fetch the Web API landing page", file=sys.stderr)
        return 1

    build_match = BUILD_ID_RE.search(entry_html)
    if not build_match:
        print("ERROR: could not find the Next.js buildId in the landing page", file=sys.stderr)
        return 1
    build_id = build_match.group(1)

    nav_order: list[str] = []
    for href in DOC_LINK_RE.findall(entry_html):
        path = normalize_path(href)
        if path == "/" or is_reference_path(path) or path in nav_order:
            continue
        nav_order.append(path)
    if ENTRY_PATH not in nav_order:
        nav_order.insert(0, ENTRY_PATH)

    print(f"  buildId: {build_id}")
    print(f"  guide pages in navigation: {len(nav_order)}")

    # -- fetch guide page data (breadth-first, so pages missing from the
    #    navigation are still picked up from in-page links) ----------------
    pages: dict[str, dict] = {}
    order: dict[str, int] = {p: i for i, p in enumerate(nav_order)}
    queue = list(nav_order)
    seen: set[str] = set()

    while queue:
        batch = [p for p in queue if p not in seen]
        seen.update(batch)
        queue = []
        if not batch:
            break

        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
            futures = []
            for path in batch:
                key = guide_output(path)
                cached = lookup.get(key, {})
                etag = cached.get("etag") if cached.get("fetcher_sha") == FETCHER_SHA else None
                futures.append(pool.submit(fetch_page, path, build_id, etag, os.path.join(DOCS_DIR, key)))
            results = [f.result() for f in futures]

        for path, url, status, body, headers in sorted(results):
            record = {"url": url, "status": status, "etag": headers.get("etag")}
            if status == 200 and body:
                try:
                    props = json.loads(body).get("pageProps") or {}
                except json.JSONDecodeError as e:
                    print(f"ERROR: bad page data for {path}: {e}", file=sys.stderr)
                    record["status"] = 0
                    pages[path] = record
                    continue
                record["props"] = props
                for href in COMPILED_LINK_RE.findall((props.get("source") or {}).get("compiledSource") or ""):
                    found = normalize_path(href)
                    if found not in seen and not is_reference_path(found):
                        order.setdefault(found, len(order))
                        queue.append(found)
            pages[path] = record

    fetched = sum(1 for r in pages.values() if r["status"] == 200)
    not_modified = sum(1 for r in pages.values() if r["status"] == 304)
    failed_pages = [p for p, r in pages.items() if r["status"] not in (200, 304)]
    print(f"  pages: {len(pages)} ({fetched} fetched, {not_modified} unchanged, {len(failed_pages)} failed)")

    # -- fetch the OpenAPI spec -----------------------------------------
    spec_cached = cache.get(SPEC_KEY, {}) or {}
    recorded_outputs = spec_cached.get("outputs") or []
    outputs_intact = bool(recorded_outputs) and all(
        os.path.exists(os.path.join(DOCS_DIR, key)) for key in recorded_outputs
    )
    spec_etag = None
    if (
        not args.force
        and outputs_intact
        and spec_cached.get("fetcher_sha") == FETCHER_SHA
        and spec_cached.get("etag")
    ):
        spec_etag = spec_cached["etag"]

    print("Fetching OpenAPI spec...")
    status, raw_spec, spec_headers = http_get(SPEC_URL, etag=spec_etag, label=SPEC_URL)
    spec_unchanged = status == 304
    spec_failed = raw_spec is None and not spec_unchanged
    spec: dict = {}
    if raw_spec:
        try:
            spec = yaml.safe_load(raw_spec) or {}
        except yaml.YAMLError as e:
            print(f"ERROR: could not parse the OpenAPI spec: {e}", file=sys.stderr)
            spec = {}
            spec_failed = True
    if spec_unchanged:
        print("  spec unchanged (304), reusing generated reference")
    elif spec_failed:
        print("  spec unavailable, preserving the existing reference", file=sys.stderr)

    # -- plan the reference tree ----------------------------------------
    endpoints_by_tag: dict[str, list[dict]] = {}
    if spec:
        endpoints_by_tag, groups, total_endpoints = collect_endpoints(spec)
        info = {
            "version": (spec.get("info") or {}).get("version", "?"),
            "openapi": spec.get("openapi", "3.0"),
        }
        base_url = resolve_server_url(spec)
        scopes = (
            ((spec.get("components") or {}).get("securitySchemes") or {})
            .get("oauth_2_0", {})
            .get("flows", {})
            .get("authorizationCode", {})
            .get("scopes", {})
        )
        deprecated_count = len(
            {
                e["operation_id"]
                for eps in endpoints_by_tag.values()
                for e in eps
                if e["operation"].get("deprecated")
            }
        )
    else:
        groups = spec_cached.get("groups") or {}
        info = spec_cached.get("info") or {}
        base_url = spec_cached.get("base_url", "")
        scopes = dict.fromkeys(spec_cached.get("scopes") or [], "")
        total_endpoints = spec_cached.get("endpoint_count", 0)
        deprecated_count = spec_cached.get("deprecated_count", 0)

    # -- resolve local link targets -------------------------------------
    local_paths: dict[str, str] = {}
    for path, record in pages.items():
        key = guide_output(path)
        if record["status"] == 200 or os.path.exists(os.path.join(DOCS_DIR, key)) or args.dry_run:
            local_paths[path] = key
    if endpoints_by_tag:
        for endpoints in endpoints_by_tag.values():
            for ep in endpoints:
                if ep["operation_id"]:
                    local_paths.setdefault(f"{REFERENCE_PATH}/{ep['operation_id']}", ep["key"])
    else:
        for key in recorded_outputs:
            name = os.path.basename(key)
            if name != "README.md":
                local_paths.setdefault(f"{REFERENCE_PATH}/{name[:-3]}", key)
    local_paths.setdefault(REFERENCE_PATH, f"{REFERENCE_DIR}/README.md")

    migration_guide = local_paths.get(f"{ENTRY_PATH}/tutorials/february-2026-migration-guide")
    policy_refs: dict = {}
    for record in pages.values():
        refs = (record.get("props") or {}).get("policyReferences")
        if isinstance(refs, dict) and refs:
            policy_refs = refs
            break

    writer = Writer(cache, args)
    unknown_components: dict = {}
    conversion_failures: list[str] = []

    # -- reference tree --------------------------------------------------
    reference_outputs: list[str] = []
    if endpoints_by_tag:
        for tag in sorted(endpoints_by_tag):
            endpoints = endpoints_by_tag[tag]
            tag_dir = groups[tag]["dir"]
            tag_description = ""
            for entry in spec.get("tags") or []:
                if isinstance(entry, dict) and entry.get("name") == tag:
                    tag_description = clean_spec_text(
                        entry.get("description"), make_resolver(f"{REFERENCE_DIR}/{tag_dir}", local_paths)
                    )
            readme_key = f"{REFERENCE_DIR}/{tag_dir}/README.md"
            writer.write(readme_key, build_tag_readme(tag, endpoints, tag_description))
            reference_outputs.append(readme_key)

            for ep in endpoints:
                note = "This endpoint is deprecated."
                if migration_guide:
                    link = relative_link(migration_guide, ep["dir"])
                    note += f" See the [February 2026 migration guide]({link})."
                writer.write(
                    ep["key"],
                    build_endpoint_markdown(ep, spec, policy_refs, local_paths, note),
                )
                reference_outputs.append(ep["key"])

        reference_readme_key = f"{REFERENCE_DIR}/README.md"
        writer.write(
            reference_readme_key,
            build_reference_readme(groups, info, base_url, deprecated_count, total_endpoints),
        )
        reference_outputs.append(reference_readme_key)
    else:
        for key in recorded_outputs:
            writer.keep(key)
        reference_outputs = list(recorded_outputs)

    # -- guide pages -----------------------------------------------------
    guide_index: list[dict] = []
    for path in sorted(pages, key=lambda p: (order.get(p, len(order)), p)):
        record = pages[path]
        key = guide_output(path)
        cached = cache.get(key, {})

        if record["status"] == 200:
            props = record.get("props") or {}
            source = props.get("source") or {}
            frontmatter = source.get("frontmatter") or {}
            title = collapse_ws(str(frontmatter.get("title") or props.get("pageTitle") or key[:-3]))
            description = str(frontmatter.get("description") or "")
            try:
                root = parse_compiled_mdx(source.get("compiledSource") or "")
                content = build_guide_markdown(
                    title, description, path, root, key, local_paths, unknown_components
                )
            except (MdxParseError, RecursionError) as e:
                print(f"ERROR: could not convert {path}: {e}", file=sys.stderr)
                conversion_failures.append(path)
                if cached:
                    writer.keep(key, cached)
                    guide_index.append(
                        {
                            "output": key,
                            "title": cached.get("title") or key[:-3],
                            "section": cached.get("section", guide_section(key)),
                        }
                    )
                continue
            writer.write(
                key,
                content,
                extra={
                    "url": record["url"],
                    "etag": record.get("etag"),
                    "fetcher_sha": FETCHER_SHA,
                    "title": title,
                    "section": guide_section(key),
                },
            )
            guide_index.append({"output": key, "title": title, "section": guide_section(key)})
            continue

        # 304, or a failed fetch: keep the last known good page.
        if not cached:
            continue
        entry = dict(cached)
        if record["status"] == 304 and record.get("etag"):
            entry["etag"] = record["etag"]
        writer.keep(key, entry)
        guide_index.append(
            {
                "output": key,
                "title": entry.get("title") or key[:-3],
                "section": entry.get("section", guide_section(key)),
            }
        )

    # -- top-level catalogue --------------------------------------------
    writer.write(
        "README.md",
        build_top_readme(guide_index, groups, info, base_url, scopes, deprecated_count, total_endpoints),
    )

    # -- source snapshot -------------------------------------------------
    if endpoints_by_tag:
        writer.new_cache[SPEC_KEY] = {
            "sha256": sha256(raw_spec or ""),
            "etag": spec_headers.get("etag"),
            "last_modified": spec_headers.get("last-modified"),
            "fetcher_sha": FETCHER_SHA,
            "outputs": sorted(reference_outputs),
            "groups": groups,
            "info": info,
            "base_url": base_url,
            "scopes": sorted(scopes),
            "endpoint_count": total_endpoints,
            "deprecated_count": deprecated_count,
            "last_updated": now_iso(),
        }
        if not args.dry_run and raw_spec:
            with open(SPEC_FILE, "w") as f:
                f.write(raw_spec)
    elif spec_cached:
        entry = dict(spec_cached)
        if spec_unchanged and spec_headers.get("etag"):
            entry["etag"] = spec_headers["etag"]
        writer.new_cache[SPEC_KEY] = entry

    # -- removals --------------------------------------------------------
    # Only safe when discovery succeeded for every source: a failed fetch or a
    # preserved page keeps its cache key, so it is never treated as a removal.
    removed = 0
    if not failed_pages and not conversion_failures and not spec_failed:
        for key in sorted(cache):
            if key == SPEC_KEY or key in writer.new_cache:
                continue
            target = os.path.join(DOCS_DIR, key)
            if not os.path.exists(target):
                continue
            if args.dry_run:
                print(f"  REMOVE {key}")
            else:
                os.remove(target)
                if args.verbose:
                    print(f"  REMOVE {key}")
            removed += 1

        if not args.dry_run:
            for dirpath, dirs, files in os.walk(DOCS_DIR, topdown=False):
                if dirpath != DOCS_DIR and not dirs and not files:
                    os.rmdir(dirpath)
                    if args.verbose:
                        print(f"  RMDIR {os.path.relpath(dirpath, DOCS_DIR)}/")

    if not args.dry_run:
        save_cache(writer.new_cache)

    print("\nSync complete:")
    print(f"  Added:      {writer.added}")
    print(f"  Updated:    {writer.updated}")
    print(f"  Unchanged:  {writer.unchanged}")
    print(f"  Removed:    {removed}")
    print(f"  Guides:     {len(guide_index)}")
    print(f"  Endpoints:  {total_endpoints} ({deprecated_count} deprecated)")
    if unknown_components:
        summary = ", ".join(f"{k} x{v}" for k, v in sorted(unknown_components.items()))
        print(f"  Unhandled MDX components: {summary}")
    if failed_pages:
        print(f"  Failed pages: {len(failed_pages)} (previous output preserved)")
        for path in failed_pages:
            print(f"    {path}")
    if conversion_failures:
        print(f"  Conversion failures: {len(conversion_failures)}")
    return 1 if (failed_pages or conversion_failures or spec_failed) else 0


def main():
    parser = argparse.ArgumentParser(
        description="Fetch Spotify Web API docs (OpenAPI spec plus guide pages) as markdown"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true", help="Re-generate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    args = parser.parse_args()
    sys.exit(sync(args))


if __name__ == "__main__":
    main()
