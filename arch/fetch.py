#!/usr/bin/env python3

"""
Arch Linux Wiki Documentation Fetcher

Mirrors the English articles of https://wiki.archlinux.org as local markdown.
Articles are stored flat at docs/{Title_with_underscores}.md (matching the
tsgates/arch-wiki-markdown convention -- subpages flatten via `_`), each
hashed into .cache.json so every run reports exactly which pages were
added, updated, or removed since last sync.

Each article carries a `**Categories:** ...` line linking to local
category index files under docs/_categories/, which mirror the wiki's
category graph -- articles can belong to multiple categories without file
duplication.

Discovery: MediaWiki API list=allpages with apnamespace=0 and
apfilterredir=nonredirects. Translations are detected by the standard Arch
Wiki suffix pattern -- "Title (Italiano)", "Title (Русский)", etc. -- and
excluded.

Fetching: prop=revisions|categories&rvprop=content fetches up to 50 pages
per request along with their categories in a single round trip. Category
continuations are followed transparently when a batch overflows cllimit.

Conversion: wikitext is converted to markdown by a focused, line-based
converter that handles headings, bold/italic, lists, internal/external
links, code, and the most common Arch Wiki templates (ic, bc, hc, Note,
Warning, Tip, Pkg, AUR, man, File, Related). Unknown templates are left
as-is so they remain visible in the output.

Source: https://wiki.archlinux.org/title/Main_page
API:    https://wiki.archlinux.org/api.php
"""

import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://wiki.archlinux.org"
API = f"{BASE}/api.php"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

USER_AGENT = "arch-wiki-docs-fetcher/1.0"

# Per-batch limits used by the public MediaWiki API for non-bot accounts.
BATCH_SIZE = 50
LIST_LIMIT = 500

# Languages used as suffixes on translated Arch Wiki articles. A title
# ending with " (X)" where X is one of these is treated as non-English and
# skipped. Sourced from the Arch Wiki language nav at the bottom of articles.
LANGUAGE_SUFFIXES = frozenset({
    "Bahasa Indonesia", "Bosanski", "Català", "Čeština", "Dansk", "Deutsch",
    "Eesti", "Español", "Esperanto", "Euskara", "Français", "Hrvatski",
    "Italiano", "Lietuvių", "Magyar", "Nederlands", "Norsk Bokmål", "Polski",
    "Português", "Português (Brasil)", "Qhichwa", "Română", "Slovenčina",
    "Slovenský", "Slovenščina", "Suomi", "Svenska", "Türkçe", "Tiếng Việt",
    "Ελληνικά", "Български", "Қазақша", "Македонски", "Русский", "Српски",
    "Українська",
    "العربية", "עברית", "فارسی", "हिन्दी", "ไทย", "বাংলা",
    "中文 (简体)", "中文 (繁體)", "中文（简体）", "中文（繁體）",
    "文言文", "正體中文", "粵語",
    "日本語", "한국어",
})


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def load_cache() -> dict:
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE, "r") as f:
            return json.load(f)
    return {}


def save_cache(cache: dict) -> None:
    with open(CACHE_FILE, "w") as f:
        json.dump(cache, f, indent=2, sort_keys=True)
        f.write("\n")


def http_get_json(url: str, timeout: int = 60, retries: int = 3) -> dict | None:
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    last_err: Exception | None = None
    for attempt in range(retries):
        try:
            with urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                continue
    print(f"  ERROR: GET {url}: {last_err}", file=sys.stderr)
    return None


def title_to_filename(title: str) -> str:
    """Map a wiki title to a flat .md filename.

    Spaces and subpage `/` separators are both flattened to `_`, matching
    the convention used by tsgates/arch-wiki-markdown:
        "Bash"                       -> "Bash.md"
        "Wireless network configuration" -> "Wireless_network_configuration.md"
        "Bluetooth/Headset"          -> "Bluetooth_Headset.md"
        ".NET"                       -> ".NET.md"
    """
    safe = _safe_segment(title.strip().replace("/", "_"))
    return safe + ".md"


def category_to_filename(cat: str) -> str:
    """Map a category name (without `Category:` prefix) to a filename."""
    return _safe_segment(cat.strip().replace("/", "_")) + ".md"


def _safe_segment(seg: str) -> str:
    seg = seg.replace(" ", "_")
    seg = re.sub(r'[<>:"\\|?*\x00-\x1f]', "-", seg)
    return seg or "untitled"


def title_to_url(title: str) -> str:
    return f"{BASE}/title/{urllib.parse.quote(title.replace(' ', '_'), safe='/')}"


def category_to_url(cat: str) -> str:
    return f"{BASE}/title/Category:{urllib.parse.quote(cat.replace(' ', '_'), safe='/')}"


# Categories that are tracking/maintenance buckets, not topical -- filtered
# out of the per-article categories line and from the index generation.
_TRACKING_CATEGORY_PREFIXES = (
    "Pages or sections flagged with Template:",
    "Pages with broken",
    "Pages with dead",
)


def is_tracking_category(cat: str) -> bool:
    if any(cat.startswith(p) for p in _TRACKING_CATEGORY_PREFIXES):
        return True
    # Localized categories like "About Arch (Bosanski)" -- same suffix
    # pattern used to filter translated article titles.
    if is_translated_title(cat):
        return True
    return False


def is_translated_title(title: str) -> bool:
    """Detect translations by their " (Language)" suffix pattern."""
    m = re.search(r"\s\(([^()]+)\)$", title)
    if m and m.group(1) in LANGUAGE_SUFFIXES:
        return True
    return False


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
# Discovery
# ---------------------------------------------------------------------------

def discover_titles(verbose: bool = False) -> list[str]:
    """Page through list=allpages and return all English article titles."""
    titles: list[str] = []
    apcontinue: str | None = None
    page = 0
    while True:
        page += 1
        params = {
            "action": "query",
            "list": "allpages",
            "apnamespace": "0",
            "apfilterredir": "nonredirects",
            "aplimit": str(LIST_LIMIT),
            "format": "json",
        }
        if apcontinue:
            params["apcontinue"] = apcontinue
        url = f"{API}?{urllib.parse.urlencode(params)}"
        data = http_get_json(url)
        if data is None:
            print("ERROR: discovery failed", file=sys.stderr)
            sys.exit(1)
        batch = data.get("query", {}).get("allpages", [])
        for entry in batch:
            t = entry["title"]
            if not is_translated_title(t):
                titles.append(t)
        if verbose:
            print(f"  page {page}: +{len(batch)} ({len(titles)} kept after filter)")
        cont = data.get("continue")
        if not cont:
            break
        apcontinue = cont.get("apcontinue")
        if not apcontinue:
            break
        # Be polite: brief delay between paged requests.
        time.sleep(0.2)
    return titles


def fetch_pages_bulk(titles: list[str], verbose: bool = False) -> dict[str, dict]:
    """Fetch wikitext + categories for all titles via batched API calls.

    Returns {title: {"wikitext": str, "categories": [str, ...]}}. Categories
    are returned without their `Category:` prefix. Missing/unavailable pages
    are silently skipped.

    A single API call returns both content and categories via
    `prop=revisions|categories`. Categories may paginate beyond cllimit when
    a batch has many; the continuation is followed transparently.
    """
    out: dict[str, dict] = {}
    total_batches = (len(titles) + BATCH_SIZE - 1) // BATCH_SIZE
    for batch_idx in range(0, len(titles), BATCH_SIZE):
        batch = titles[batch_idx:batch_idx + BATCH_SIZE]
        merged: dict[str, dict] = {}
        cont: dict | None = None
        while True:
            params = {
                "action": "query",
                "prop": "revisions|categories",
                "rvprop": "content",
                "rvslots": "main",
                "cllimit": "max",
                "clshow": "!hidden",
                "titles": "|".join(batch),
                "format": "json",
                "formatversion": "2",
            }
            if cont:
                params.update(cont)
            url = f"{API}?{urllib.parse.urlencode(params)}"
            data = http_get_json(url)
            if data is None:
                break
            pages = data.get("query", {}).get("pages", [])
            if isinstance(pages, dict):
                pages = list(pages.values())
            for p in pages:
                title = p.get("title")
                if not title:
                    continue
                entry = merged.setdefault(title, {"wikitext": None, "categories": []})
                revs = p.get("revisions") or []
                if revs and entry["wikitext"] is None:
                    slot = revs[0].get("slots", {}).get("main", {})
                    content = slot.get("content") or slot.get("*")
                    if content is not None:
                        entry["wikitext"] = content
                for c in p.get("categories", []) or []:
                    name = c.get("title", "")
                    if name.startswith("Category:"):
                        name = name[len("Category:"):]
                    if name and name not in entry["categories"]:
                        entry["categories"].append(name)
            cont = data.get("continue")
            if not cont:
                break
            # Drop the inner-only `continue` marker; keep the real cursors.
            cont = {k: v for k, v in cont.items() if k != "continue"}
        for t, e in merged.items():
            if e["wikitext"] is not None:
                out[t] = e
        if verbose:
            done = batch_idx // BATCH_SIZE + 1
            print(f"  batch {done}/{total_batches}: {len(out)} pages so far")
        time.sleep(0.3)
    return out


# ---------------------------------------------------------------------------
# Wikitext to markdown
# ---------------------------------------------------------------------------

# Regexes used by the converter. Compiled once at module load.
_RE_HEADING = re.compile(r"^(={1,6})\s*(.+?)\s*\1\s*$")
_RE_BOLDITALIC = re.compile(r"'''''(.+?)'''''", re.DOTALL)
_RE_BOLD = re.compile(r"'''(.+?)'''", re.DOTALL)
_RE_ITALIC = re.compile(r"''(.+?)''", re.DOTALL)
_RE_INTERNAL_LINK = re.compile(r"\[\[([^\[\]|]+?)(?:\|([^\[\]]*?))?\]\]")
_RE_EXTERNAL_LINK = re.compile(r"\[((?:https?|ftp|file)://\S+?)(?:\s+([^\]]+))?\]")
_RE_BARE_URL = re.compile(r"(?<!\()(?<!\[)(https?://[^\s\)\]]+)")
_RE_HTML_CODE = re.compile(r"<code>(.+?)</code>", re.DOTALL)
_RE_NOWIKI = re.compile(r"<nowiki>(.+?)</nowiki>", re.DOTALL)
_RE_REF = re.compile(r"<ref[^>]*>.*?</ref>", re.DOTALL)
_RE_REF_SELF = re.compile(r"<ref[^/]*/>", re.DOTALL)
_RE_HTML_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_RE_HTML_HR = re.compile(r"<hr\s*/?>", re.IGNORECASE)
_RE_HTML_BOLD = re.compile(r"<(b|strong)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_RE_HTML_ITAL = re.compile(r"<(i|em)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_RE_HTML_KBD = re.compile(r"<(kbd|tt)>(.*?)</\1>", re.DOTALL | re.IGNORECASE)
_RE_INTERWIKI = re.compile(
    r"\[\[(?:Category|File|Image|" +
    r"|".join(re.escape(p) for p in [
        "de", "es", "fr", "ja", "ru", "uk", "zh-hans", "zh-hant", "zh-cn",
        "pt", "tr", "it", "pl", "fa", "ar", "ko", "vi", "fi", "cs", "hu",
        "ro", "el", "bg", "kk", "mk", "sr", "hr", "nl", "sv", "no", "da",
        "id", "th", "he", "hi", "ca", "et", "eu", "lt", "sk",
    ]) + r"):[^\]]*\]\]"
)
_RE_MAGIC_WORD = re.compile(r"__[A-Z_]+__")
_RE_LIST_LINE = re.compile(r"^([*#:;]+)(?:\s+(.*)|$)")


def _convert_inline(text: str) -> str:
    """Transform inline wikitext markers to markdown."""
    # Strip <ref>..</ref> footnotes wholesale (Arch Wiki rarely uses them).
    text = _RE_REF.sub("", text)
    text = _RE_REF_SELF.sub("", text)

    # Stripe nowiki wrappers; their content stays literal.
    text = _RE_NOWIKI.sub(r"\1", text)

    # Templates: handled before inline-link matching so {{ic|...}} etc.
    # become backticks and don't confuse the link regex with embedded |.
    text = _convert_templates_inline(text)

    # Bold + italic: '''''X''''' -> ***X***. Order matters.
    text = _RE_BOLDITALIC.sub(r"***\1***", text)
    text = _RE_BOLD.sub(r"**\1**", text)
    text = _RE_ITALIC.sub(r"*\1*", text)

    # HTML inline tags.
    text = _RE_HTML_BOLD.sub(lambda m: f"**{m.group(2)}**", text)
    text = _RE_HTML_ITAL.sub(lambda m: f"*{m.group(2)}*", text)
    text = _RE_HTML_KBD.sub(lambda m: f"`{m.group(2)}`", text)
    text = _RE_HTML_CODE.sub(lambda m: f"`{m.group(1)}`", text)
    text = _RE_HTML_BR.sub("  \n", text)

    # External links: [https://url text]
    def _ext(m):
        url = m.group(1)
        label = (m.group(2) or url).strip()
        return f"[{label}]({url})"
    text = _RE_EXTERNAL_LINK.sub(_ext, text)

    # Internal links: [[Page]] or [[Page|Display]] or [[Page#anchor|Display]]
    def _internal(m):
        target = m.group(1).strip()
        display = (m.group(2) or "").strip() or target
        # Cross-wiki prefixes -- redirect to the appropriate site.
        lower = target.lower()
        if lower.startswith("wikipedia:") or lower.startswith("w:"):
            page = target.split(":", 1)[1]
            url = f"https://en.wikipedia.org/wiki/{urllib.parse.quote(page.replace(' ', '_'))}"
            return f"[{display}]({url})"
        if "#" in target:
            page, _, anchor = target.partition("#")
            url = title_to_url(page) + "#" + urllib.parse.quote(anchor.replace(" ", "_"), safe="")
        else:
            url = title_to_url(target)
        return f"[{display}]({url})"
    text = _RE_INTERNAL_LINK.sub(_internal, text)

    return text


# ---- Templates -------------------------------------------------------------

def _convert_templates_inline(text: str) -> str:
    """Replace common inline templates with markdown equivalents.

    Block templates (Note/Warning/Tip and bc/hc/File) are handled by the
    line-level pass; this only touches templates that fit on one line such
    as `{{ic|...}}`, `{{Pkg|...}}`, `{{AUR|...}}`, `{{man|N|name}}`.
    """
    # Recognize and rewrite known templates iteratively from inside out so
    # nested templates work. Each iteration replaces innermost {{ ... }}
    # with no nested {{ inside.
    pattern = re.compile(r"\{\{([^{}|]+?)((?:\|[^{}]*?)*)\}\}", re.DOTALL)

    def replace_one(m: re.Match) -> str:
        name = m.group(1).strip()
        args_str = m.group(2) or ""
        # Don't strip per-arg here -- some templates (notably {{ic|...}}) need
        # to preserve internal whitespace. Templates that care can strip.
        args = args_str.split("|")[1:] if args_str else []
        return _render_template(name, args, m.group(0))

    prev = None
    for _ in range(8):  # limit nesting iterations
        if text == prev:
            break
        prev = text
        text = pattern.sub(replace_one, text)
    return text


def _render_template(name: str, args: list[str], original: str) -> str:
    n = name.lower()
    # Handle named-arg form for {{ic|1=...}} where arg value contains "=".
    positional: list[str] = []
    named: dict[str, str] = {}
    for a in args:
        if "=" in a:
            k, _, v = a.partition("=")
            if k.strip().isdigit():
                positional.append(v)
            else:
                named[k.strip()] = v
        else:
            positional.append(a)

    def first_pos() -> str:
        return (positional[0] if positional else "").strip()

    if n in ("ic", "kbd", "key"):
        # `{{ic|cmd | grep foo}}` legitimately contains a literal pipe
        # (escaped as `{{!}}` upstream); rejoin positional args with `|` so
        # the literal pipe is preserved inside the inline-code span.
        return f"`{'|'.join(positional) if positional else ''}`"
    if n == "pkg":
        pkg = first_pos()
        return f"[{pkg}](https://archlinux.org/packages/?q={urllib.parse.quote(pkg)})" if pkg else original
    if n == "aur":
        pkg = first_pos()
        return f"[{pkg}](https://aur.archlinux.org/packages/{urllib.parse.quote(pkg)})" if pkg else original
    if n == "man":
        # {{man|1|grep}} -> [grep(1)](https://man.archlinux.org/man/grep.1)
        if len(positional) >= 2:
            section, page = positional[0], positional[1]
            return f"[{page}({section})](https://man.archlinux.org/man/{page}.{section})"
        return original
    if n == "wikipedia":
        page = first_pos()
        if page:
            label = positional[1] if len(positional) >= 2 else page
            return f"[{label}](https://en.wikipedia.org/wiki/{urllib.parse.quote(page.replace(' ', '_'))})"
        return original
    if n == "ic1" or n == "ic2":
        return f"`{first_pos()}`"
    if n in ("nbsp", "bull"):
        return " " if n == "nbsp" else "•"
    if n == "!":
        return "|"  # MediaWiki pipe-escape inside templates.
    if n in ("yes", "y"):
        return "Yes"
    if n in ("no", "n"):
        return "No"
    if n == "n/a":
        return "N/A"
    if n in ("anchor",):
        return ""
    # Drop the "Translation status" / "TranslationStatus" / "i18n" markers.
    if n in ("i18n", "translationstatus", "translateme"):
        return ""
    # Inline boldness / emphasis aliases.
    if n in ("strong", "em"):
        return f"**{first_pos()}**" if n == "strong" else f"*{first_pos()}*"
    # Unknown -- preserve as literal text so the user can see it.
    return original


_BLOCK_TEMPLATE_RE = re.compile(r"\{\{(Note|Warning|Tip|Important|Caution)\|", re.IGNORECASE)
_BC_TEMPLATE_RE = re.compile(r"\{\{(bc|hc|File)\|", re.IGNORECASE)
_RELATED_START_RE = re.compile(r"\{\{Related articles start\}\}", re.IGNORECASE)
_RELATED_END_RE = re.compile(r"\{\{Related articles end\}\}", re.IGNORECASE)
_RELATED_ITEM_RE = re.compile(r"\{\{Related\|([^{}|]+?)(?:\|([^{}|]+?))?\}\}", re.IGNORECASE)
_LAYOUT_TEMPLATES_RE = re.compile(
    r"\{\{(?:Style|Expansion|Accuracy|Out of date|Move|Merge|Stub|Laptop style"
    r"|Translateme|TranslationStatus|i18n|Talk|Lowercase title|DISPLAYTITLE"
    r"|Article summary[^|}]*)(?:\|[^{}]*?)?\}\}",
    re.IGNORECASE | re.DOTALL,
)


def _consume_template(text: str, start: int) -> tuple[str, int]:
    """Scan a balanced {{ ... }} starting at `text[start]` and return
    (raw, end_index_exclusive). Handles nested {{ }}.
    """
    if not text.startswith("{{", start):
        return "", start
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text.startswith("{{", i):
            depth += 1
            i += 2
            continue
        if text.startswith("}}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return text[start:i], i
            continue
        i += 1
    return text[start:], n


def _split_template_args(body: str) -> list[str]:
    """Split a template body 'name|arg|arg' on top-level | only."""
    args: list[str] = []
    depth_brace = 0
    depth_brack = 0
    cur = []
    for ch in body:
        if ch == "{" and depth_brace == 0:
            # could be start of {{ -- track curly depth roughly
            depth_brace += 1
        elif ch == "}" and depth_brace > 0:
            depth_brace -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]" and depth_brack > 0:
            depth_brack -= 1
        if ch == "|" and depth_brace == 0 and depth_brack == 0:
            args.append("".join(cur))
            cur = []
            continue
        cur.append(ch)
    args.append("".join(cur))
    return args


def _render_admonition(kind: str, body: str) -> list[str]:
    """Render a Note / Warning / Tip block to a markdown blockquote."""
    label_map = {
        "note": "Note", "warning": "Warning", "tip": "Tip",
        "important": "Important", "caution": "Caution",
    }
    label = label_map.get(kind.lower(), kind.capitalize())
    out = [f"> **{label}:** {body.strip().splitlines()[0]}" if body.strip() else f"> **{label}**"]
    rest = body.strip().splitlines()[1:] if body.strip() else []
    for line in rest:
        out.append(f"> {line}".rstrip())
    out.append("")
    return out


def _render_code_block(lang: str, body: str) -> list[str]:
    # Strip <nowiki> wrappers used inside templates like {{File}}/{{hc}} -- the
    # body text inside is what we want to render literally.
    body = re.sub(r"</?nowiki\s*/?>", "", body)
    out = [f"```{lang}".rstrip()]
    body = body.rstrip("\n").lstrip("\n")
    if body:
        out.extend(body.split("\n"))
    out.append("```")
    out.append("")
    return out


def _strip_comments(text: str) -> str:
    return re.sub(r"<!--.*?-->", "", text, flags=re.DOTALL)


def _strip_categories_and_interwiki(text: str) -> str:
    return _RE_INTERWIKI.sub("", text)


def _strip_magic_words(text: str) -> str:
    return _RE_MAGIC_WORD.sub("", text)


def _convert_related_articles(text: str) -> str:
    """Replace `{{Related articles start}}...{{Related articles end}}` blocks
    with a "Related articles" markdown section.
    """
    def replace(m: re.Match) -> str:
        body = m.group(1)
        items = []
        for im in _RELATED_ITEM_RE.finditer(body):
            target = im.group(1).strip()
            display = (im.group(2) or target).strip()
            url = title_to_url(target.lstrip("/"))
            items.append(f"- [{display}]({url})")
        if not items:
            return ""
        return "\n\n**Related articles**\n\n" + "\n".join(items) + "\n\n"

    pattern = re.compile(
        r"\{\{Related articles start\}\}(.*?)\{\{Related articles end\}\}",
        re.IGNORECASE | re.DOTALL,
    )
    return pattern.sub(replace, text)


def _strip_layout_templates(text: str) -> str:
    return _LAYOUT_TEMPLATES_RE.sub("", text)


# ---- Top-level converter ---------------------------------------------------

def wikitext_to_markdown(text: str) -> str:
    """Convert a wikitext document to markdown.

    The output is intentionally lossy: unknown templates are preserved as
    `{{name|args}}` so they remain visible. Headings, paragraphs, lists,
    code blocks, links, and the most common Arch templates are translated.
    """
    text = _strip_comments(text)
    text = _strip_categories_and_interwiki(text)
    text = _strip_magic_words(text)
    text = _strip_layout_templates(text)
    text = _convert_related_articles(text)

    out: list[str] = []
    i = 0
    n = len(text)
    in_pre_block = False        # <pre> ... </pre>
    in_syntax_block = False     # <syntaxhighlight lang="X"> ...
    in_table = False
    table_buf: list[str] = []

    # We do block-level scanning by walking the source character by character
    # to handle multi-line constructs (templates, syntaxhighlight, tables).
    while i < n:
        # Block templates: {{Note|...}}, {{bc|...}}, etc.
        if text.startswith("{{", i) and not in_pre_block and not in_syntax_block:
            m_admon = _BLOCK_TEMPLATE_RE.match(text, i)
            m_block = _BC_TEMPLATE_RE.match(text, i)
            if m_admon or m_block:
                raw, end = _consume_template(text, i)
                inner = raw[2:-2]  # strip {{ }}
                # Split on top-level pipes.
                args = _split_template_args(inner)
                name = args[0].strip()
                rest = args[1:]
                # Anchor newline before the block.
                if out and out[-1].strip():
                    out.append("")
                if m_admon:
                    body = "|".join(rest)
                    # Strip "1=" prefix used to escape `=` in template arg
                    # values: `{{Tip|1=Some text with = in it}}`.
                    body = re.sub(r"^\s*\d+\s*=\s*", "", body)
                    body_md = _convert_inline(body)
                    out.extend(_render_admonition(name, body_md))
                else:
                    # bc / hc / File
                    if name.lower() == "file":
                        # {{File|name=foo|content=bar}}
                        named = {}
                        positional = []
                        for a in rest:
                            if "=" in a:
                                k, _, v = a.partition("=")
                                if not k.strip().isdigit():
                                    named[k.strip()] = v
                                else:
                                    positional.append(v)
                            else:
                                positional.append(a)
                        fname = named.get("name", positional[0] if positional else "")
                        content = named.get("content", positional[1] if len(positional) >= 2 else "")
                        if fname:
                            out.append(f"**`{fname}`**")
                            out.append("")
                        out.extend(_render_code_block("", content))
                    elif name.lower() == "hc":
                        # {{hc|header|body}}
                        header = rest[0] if rest else ""
                        body = "|".join(rest[1:])
                        if header.strip():
                            out.append(f"**{_convert_inline(header)}**")
                            out.append("")
                        out.extend(_render_code_block("", body))
                    else:
                        # bc -- a single body arg
                        body = "|".join(rest)
                        out.extend(_render_code_block("", body))
                i = end
                continue

        # <pre>...</pre>
        if not in_syntax_block and text.startswith("<pre", i):
            close = text.find(">", i)
            if close != -1:
                end_close = text.find("</pre>", close)
                if end_close != -1:
                    body = text[close + 1:end_close]
                    if out and out[-1].strip():
                        out.append("")
                    out.extend(_render_code_block("", body))
                    i = end_close + len("</pre>")
                    continue

        # <syntaxhighlight lang="X">...</syntaxhighlight>
        if not in_pre_block and (text.startswith("<syntaxhighlight", i) or text.startswith("<source", i)):
            tag = "syntaxhighlight" if text.startswith("<syntaxhighlight", i) else "source"
            close = text.find(">", i)
            if close != -1:
                attrs = text[i:close]
                m = re.search(r'lang="?(\w+)"?', attrs)
                lang = m.group(1) if m else ""
                end_close = text.find(f"</{tag}>", close)
                if end_close != -1:
                    body = text[close + 1:end_close]
                    if out and out[-1].strip():
                        out.append("")
                    out.extend(_render_code_block(lang, body))
                    i = end_close + len(f"</{tag}>")
                    continue

        # Tables: {| ... |}
        if text.startswith("{|", i):
            end_table = _find_table_end(text, i)
            if end_table > i:
                table_md = _render_table(text[i:end_table])
                if out and out[-1].strip():
                    out.append("")
                out.extend(table_md)
                out.append("")
                i = end_table
                continue

        # Otherwise scan one line at a time.
        nl = text.find("\n", i)
        line_end = nl if nl != -1 else n
        line = text[i:line_end]
        i = line_end + 1 if nl != -1 else n

        out.extend(_handle_line(line))

    return _post_process(out)


def _handle_line(line: str) -> list[str]:
    """Convert a single wikitext line (no multi-line constructs) to markdown."""
    stripped = line.rstrip()

    # Headings
    m = _RE_HEADING.match(stripped)
    if m:
        level = len(m.group(1))
        return ["", "#" * level + " " + _convert_inline(m.group(2)), ""]

    # Lists
    m = _RE_LIST_LINE.match(stripped)
    if m:
        markers = m.group(1)
        body = m.group(2) or ""
        depth = len(markers)
        indent = "  " * (depth - 1)
        last = markers[-1]
        if last == "*":
            return [f"{indent}- {_convert_inline(body)}"]
        if last == "#":
            return [f"{indent}1. {_convert_inline(body)}"]
        if last == ":":
            # Indented paragraph -- render as blockquote.
            return [f"> {_convert_inline(body)}"]
        if last == ";":
            # Definition term -- render as bold.
            return [f"**{_convert_inline(body)}**"]

    # Horizontal rule
    if stripped == "----":
        return ["", "---", ""]

    # <hr>
    if _RE_HTML_HR.fullmatch(stripped or ""):
        return ["", "---", ""]

    # Pre-formatted single-line: starts with a single space (legacy syntax).
    # Treat single-space-leading lines as code blocks individually -- merging
    # adjacent ones is handled in post-processing.
    if line.startswith(" ") and stripped:
        return ["    " + line[1:].rstrip()]

    return [_convert_inline(stripped)]


def _post_process(lines: list[str]) -> str:
    # Merge consecutive 4-space-indented lines into fenced code blocks, but
    # never touch lines that are already inside a fenced block emitted by
    # `_render_code_block` (those bodies often contain 4-space-indented
    # source, which would otherwise be mistaken for new code blocks).
    out: list[str] = []
    in_fence = False
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            i += 1
            continue
        if not in_fence and line.startswith("    ") and not line.strip().startswith("---"):
            block = []
            while i < len(lines) and (lines[i].startswith("    ") or lines[i] == ""):
                block.append(lines[i][4:] if lines[i].startswith("    ") else lines[i])
                i += 1
            while block and not block[-1].strip():
                block.pop()
            if block:
                if out and out[-1].strip():
                    out.append("")
                out.append("```")
                out.extend(block)
                out.append("```")
                out.append("")
            continue
        out.append(line)
        i += 1

    # Collapse runs of blank lines.
    cleaned: list[str] = []
    prev_blank = True
    for ln in out:
        blank = not ln.strip()
        if blank and prev_blank:
            continue
        cleaned.append(ln.rstrip())
        prev_blank = blank
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    return ("\n".join(cleaned) + "\n") if cleaned else ""


# ---- Tables ----------------------------------------------------------------

def _find_table_end(text: str, start: int) -> int:
    depth = 0
    i = start
    n = len(text)
    while i < n:
        if text.startswith("{|", i):
            depth += 1
            i += 2
            continue
        if text.startswith("|}", i):
            depth -= 1
            i += 2
            if depth == 0:
                return i
            continue
        i += 1
    return n


def _render_table(table_src: str) -> list[str]:
    """Render a wikitext table as a markdown table. Lossy on complex tables."""
    body = table_src
    if body.startswith("{|"):
        body = body[2:]
    if body.endswith("|}"):
        body = body[:-2]
    # Drop the optional table-attributes line.
    body = re.sub(r"^[^\n]*\n", "", body, count=1)

    rows: list[list[str]] = []
    current: list[str] = []
    is_header_row = False
    header_idx = -1

    for raw_line in body.split("\n"):
        line = raw_line.rstrip()
        if line.startswith("|+"):
            # Caption -- ignore.
            continue
        if line.startswith("|-"):
            if current:
                rows.append(current)
                if is_header_row and header_idx == -1:
                    header_idx = len(rows) - 1
                current = []
                is_header_row = False
            continue
        if line.startswith("!"):
            is_header_row = True
            cells = re.split(r"\s*!!\s*|\s*\|\|\s*", line[1:])
            current.extend(_clean_cell(c) for c in cells)
            continue
        if line.startswith("|"):
            cells = re.split(r"\s*\|\|\s*", line[1:])
            current.extend(_clean_cell(c) for c in cells)
            continue
        # Continuation line of the previous cell.
        if current:
            current[-1] = (current[-1] + " " + line.strip()).strip()

    if current:
        rows.append(current)

    if not rows:
        return []

    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]

    if header_idx == -1:
        header_idx = 0
    md_rows: list[str] = []
    for idx, row in enumerate(rows):
        cells = [c.replace("|", "\\|").replace("\n", " ") for c in row]
        md_rows.append("| " + " | ".join(cells) + " |")
        if idx == header_idx:
            md_rows.append("|" + "|".join([" --- "] * width) + "|")
    return md_rows


def _clean_cell(c: str) -> str:
    c = c.strip()
    # Strip cell-attributes like 'align="right" |' -- the | separator from
    # attrs is escaped here; just take the content after a single |.
    if "|" in c and not c.startswith("[["):
        # Heuristic: if there's a "|" in the first chunk that doesn't look
        # like wiki markup, treat it as attribute separator.
        attrs, _, body = c.partition("|")
        if "=" in attrs and "[" not in attrs and "{" not in attrs:
            c = body.strip()
    return _convert_inline(c)


# ---------------------------------------------------------------------------
# Markdown assembly
# ---------------------------------------------------------------------------

def build_page_markdown(title: str, body: str, categories: list[str]) -> str:
    src = title_to_url(title)
    parts = [f"# {title}", "", f"*Source: [{src}]({src})*"]
    cats = [c for c in categories if not is_tracking_category(c)]
    if cats:
        cat_links = ", ".join(
            f"[{c}](./_categories/{category_to_filename(c)})" for c in sorted(cats)
        )
        parts += ["", f"**Categories:** {cat_links}"]
    parts += ["", body.rstrip(), ""]
    return "\n".join(parts)


def build_category_md(cat: str, members: list[str]) -> str:
    """Build the index page listing all articles in a category."""
    cat_url = category_to_url(cat)
    lines = [f"# Category: {cat}", ""]
    lines.append(f"*Source: [{cat_url}]({cat_url})*")
    lines.append("")
    n = len(members)
    lines.append(f"{n} article{'s' if n != 1 else ''} in this category.")
    lines.append("")
    for title in sorted(members, key=str.lower):
        rel = title_to_filename(title)
        lines.append(f"- [{title}](../{rel})")
    lines.append("")
    return "\n".join(lines)


def build_categories_index(category_members: dict[str, list[str]]) -> str:
    """Build _categories/README.md listing every category."""
    lines = ["# Categories", ""]
    lines.append(f"{len(category_members)} categories.")
    lines.append("")
    for cat in sorted(category_members, key=str.lower):
        n = len(category_members[cat])
        lines.append(f"- [{cat}](./{category_to_filename(cat)}) ({n})")
    lines.append("")
    return "\n".join(lines)


def build_top_readme(total_articles: int, total_categories: int) -> str:
    lines = ["# Arch Linux Wiki (English)", ""]
    lines.append(f"Mirror of [{BASE}]({BASE}). {total_articles} English "
                 "articles as flat markdown files; each article carries its "
                 "categories inline and is indexed under "
                 "[`_categories/`](./_categories/) by topic.")
    lines.append("")
    lines.append(f"- {total_articles} articles at the root (one `.md` per page)")
    lines.append(f"- {total_categories} category indexes under [`_categories/`](./_categories/)")
    lines.append("")
    lines.append("## Conventions")
    lines.append("")
    lines.append("- Article filenames replace spaces and subpage `/` separators with `_`. "
                 "For example `Pacman/Tips and tricks` becomes `Pacman_Tips_and_tricks.md`.")
    lines.append("- Each article starts with the source URL and a `**Categories:**` line "
                 "linking to the local category index files.")
    lines.append("- Maintenance/tracking categories (`Pages or sections flagged with ...`) are "
                 "filtered out.")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache: dict = {} if args.force else load_cache()
    new_cache: dict = {}

    print("Discovering English articles...")
    titles = discover_titles(verbose=args.verbose)
    print(f"  {len(titles)} English articles found")

    if args.limit:
        titles = titles[:args.limit]
        print(f"  --limit applied: trimmed to {len(titles)}")

    print(f"\nFetching wikitext (batches of {BATCH_SIZE})...")
    pages = fetch_pages_bulk(titles, verbose=args.verbose)
    print(f"  fetched {len(pages)} pages")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added: list[str] = []
    updated: list[str] = []
    removed: list[str] = []
    unchanged = 0
    errors = 0
    category_members: dict[str, list[str]] = {}

    def commit(rel: str, content: str, label_for_log: str) -> None:
        nonlocal unchanged
        out_path = os.path.join(DOCS_DIR, rel)
        digest = sha256(content)
        prev = cache.get(rel, {})
        if prev.get("sha256") == digest and os.path.exists(out_path):
            unchanged += 1
            new_cache[rel] = prev
            return
        is_new = rel not in cache or not os.path.exists(out_path)
        write_file(out_path, content, dry_run=args.dry_run, verbose=args.verbose,
                   label="ADD" if is_new else "UPDATE")
        new_cache[rel] = {
            "sha256": digest,
            "last_updated": datetime.now(timezone.utc).isoformat(),
            "title": label_for_log,
        }
        entry = f"{label_for_log} ({rel})"
        (added if is_new else updated).append(entry)

    # --- Articles ------------------------------------------------------
    for idx, title in enumerate(titles, start=1):
        page = pages.get(title)
        if page is None or page.get("wikitext") is None:
            errors += 1
            continue
        body_md = wikitext_to_markdown(page["wikitext"])
        cats = page.get("categories", []) or []
        page_md = build_page_markdown(title, body_md, cats)
        rel = title_to_filename(title)
        commit(rel, page_md, title)

        for c in cats:
            if is_tracking_category(c):
                continue
            category_members.setdefault(c, []).append(title)

        if args.verbose and idx % 100 == 0:
            print(f"  processed {idx}/{len(titles)}")

    # --- Per-category indexes -----------------------------------------
    for cat, members in category_members.items():
        rel = os.path.join("_categories", category_to_filename(cat))
        commit(rel, build_category_md(cat, members), f"Category: {cat}")

    # --- Categories README (overview) ---------------------------------
    commit(
        os.path.join("_categories", "README.md"),
        build_categories_index(category_members),
        "Categories index",
    )

    # --- Top-level README ---------------------------------------------
    commit(
        "README.md",
        build_top_readme(len(titles), len(category_members)),
        "Top-level index",
    )

    # Detect removals.
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
        old_title = cache.get(old_key, {}).get("title", "")
        removed.append(f"{old_title} ({old_key})" if old_title else old_key)

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

    print("\nSync complete:")
    print(f"  Added:     {len(added)}")
    print(f"  Updated:   {len(updated)}")
    print(f"  Unchanged: {unchanged}")
    print(f"  Removed:   {len(removed)}")
    print(f"  Errors:    {errors}")

    def _print(label: str, items: list[str]) -> None:
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
        description="Mirror the English Arch Linux Wiki as local markdown"
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Show what would change without writing files")
    parser.add_argument("--force", action="store_true",
                        help="Re-generate everything, ignoring cache")
    parser.add_argument("--verbose", action="store_true",
                        help="Detailed per-batch and per-page logging")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most this many articles (for testing)")
    args = parser.parse_args()
    sync(args)


if __name__ == "__main__":
    main()
