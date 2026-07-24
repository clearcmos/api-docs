#!/usr/bin/env python3

"""
OpenCode Documentation Fetcher

Fetches https://opencode.ai/docs as markdown. The docs are an Astro Starlight
site (SolidStart SSR shell) with no machine-readable spec, but every page is
served as raw MDX at a `.md` alternate (e.g. /docs/cli.md, /docs/index.md), so
we fetch those directly instead of scraping the rendered HTML.

Discovery is driven by the sidebar nav in one rendered docs page: it lists every
English page in order, grouped (Usage / Configure / Develop) with accurate
titles, none of which the `.md` files carry (Starlight strips frontmatter). The
sitemap is used as a safety net to catch any English page missing from the
sidebar; translated pages (/docs/<lang>/...) are filtered out.

The `.md` payload is MDX, not clean markdown, so a focused, fence-aware
converter degrades the Starlight bits to plain markdown: drops the leading
`import`/`export` module statements, unwraps `<Tabs>`/`<TabItem>`/`<Steps>`,
turns `<code>` + `{"..."}` JSX literals into inline code, converts `<a>` tags
(literal href -> link, `{expr}` href -> plain text since the var is unresolved),
and strips `<nobr>`. Everything inside fenced code blocks is left untouched, so
plist/XML/keybind examples that look like tags survive verbatim.
"""

import argparse
import gzip
import hashlib
import html
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

SITE = "https://opencode.ai"
DOCS_INDEX_URL = f"{SITE}/docs/"
SITEMAP_URL = f"{SITE}/sitemap.xml"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

MAX_WORKERS = 24
USER_AGENT = "opencode-api-docs-fetcher/1.0"


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept-Encoding": "gzip",
    })
    try:
        with urlopen(req, timeout=timeout) as resp:
            data = resp.read()
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


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

# A sidebar token is either a group label (<div class="group-label">...</div>)
# or a doc link wrapping its title in a <span>. Inline body links lack the span,
# so this never matches article content -- but we still bound to the nav region
# and dedupe by slug to be safe against a duplicated (mobile) sidebar.
NAV_TOKEN_RE = re.compile(
    r'<div class="group-label[^"]*">\s*<span[^>]*>([^<]+)</span>'
    r'|<a\s+href="/docs/([^"#?]*?)"[^>]*>\s*<span[^>]*>([^<]+)</span>'
)

ACRONYMS = {"cli", "tui", "sdk", "ide", "lsp", "mcp", "acp", "api", "wsl"}
SLUG_TITLE_OVERRIDE = {
    "": "Intro",
    "mcp-servers": "MCP servers",
    "lsp": "LSP Servers",
    "acp": "ACP Support",
    "windows-wsl": "Windows (WSL)",
    "github": "GitHub",
    "gitlab": "GitLab",
}


def derive_title(slug: str) -> str:
    """Fallback title for a slug not present in the sidebar nav."""
    if slug in SLUG_TITLE_OVERRIDE:
        return SLUG_TITLE_OVERRIDE[slug]
    words = re.split(r"[-/]", slug)
    out = []
    for w in words:
        out.append(w.upper() if w.lower() in ACRONYMS else w.capitalize())
    return " ".join(out)


def parse_sidebar(html_text: str) -> list[dict]:
    """Parse the docs sidebar into ordered {group, slug, title} entries.

    `group` is None for the top-level (ungrouped) pages, otherwise the section
    label (e.g. "Usage"). `slug` is "" for the docs index page.
    """
    start = html_text.find('<ul class="top-level')
    region = html_text[start:] if start != -1 else html_text
    entries: list[dict] = []
    seen: set[str] = set()
    current_group: str | None = None
    for m in NAV_TOKEN_RE.finditer(region):
        if m.group(1) is not None:
            current_group = html.unescape(m.group(1)).strip()
            continue
        slug = m.group(2).strip().strip("/")
        title = html.unescape(m.group(3)).strip()
        if slug in seen:
            continue
        seen.add(slug)
        entries.append({"group": current_group, "slug": slug, "title": title})
    return entries


def parse_sitemap_english(xml_text: str) -> set[str]:
    """Return the set of English doc slugs from sitemap.xml.

    Locale prefixes are detected as the segments that appear as a bare
    `/docs/<seg>/` index (e.g. ar, de, zh-cn), then any `/docs/<locale>/...`
    page is dropped. The English index page maps to slug "".
    """
    locs = re.findall(r"<loc>\s*([^<]+?)\s*</loc>", xml_text)
    rels: list[str] = []
    for loc in locs:
        if not loc.startswith(SITE + "/docs"):
            continue
        path = loc[len(SITE):]
        rel = path[len("/docs/"):] if path.startswith("/docs/") else path[len("/docs"):]
        rels.append(rel)
    locales = {
        rel[:-1]
        for rel in rels
        if rel.endswith("/") and rel.count("/") == 1 and rel[:-1]
    }
    english: set[str] = set()
    for rel in rels:
        r = rel.rstrip("/")
        if r == "":
            english.add("")
            continue
        if r.split("/")[0] in locales:
            continue
        english.add(r)
    return english


def discover() -> list[dict]:
    """Build the ordered page list: sidebar nav first, sitemap as a safety net."""
    print("Fetching docs sidebar...")
    index_html = fetch_url(DOCS_INDEX_URL)
    entries: list[dict] = []
    if index_html:
        entries = parse_sidebar(index_html)
        print(f"  sidebar pages: {len(entries)}")
    else:
        print("  WARNING: could not fetch sidebar; relying on sitemap", file=sys.stderr)

    print("Fetching sitemap...")
    sitemap_xml = fetch_url(SITEMAP_URL)
    nav_slugs = {e["slug"] for e in entries}
    if sitemap_xml:
        english = parse_sitemap_english(sitemap_xml)
        extra = sorted(s for s in english if s not in nav_slugs)
        for slug in extra:
            entries.append({"group": "Other", "slug": slug, "title": derive_title(slug)})
        if extra:
            print(f"  sitemap-only pages: {len(extra)} ({', '.join(extra)})")
        else:
            print("  sitemap adds no pages beyond the sidebar")
    else:
        print("  WARNING: could not fetch sitemap", file=sys.stderr)

    if not entries:
        print("ERROR: no pages discovered", file=sys.stderr)
        sys.exit(1)

    # Guarantee the index page is present.
    if not any(e["slug"] == "" for e in entries):
        entries.insert(0, {"group": None, "slug": "", "title": "Intro"})
    return entries


# ---------------------------------------------------------------------------
# URL / path helpers
# ---------------------------------------------------------------------------

def md_url(slug: str) -> str:
    return f"{SITE}/docs/index.md" if slug == "" else f"{SITE}/docs/{slug}.md"


def page_url(slug: str) -> str:
    return f"{SITE}/docs/" if slug == "" else f"{SITE}/docs/{slug}"


def file_path(slug: str) -> str:
    if slug == "":
        return os.path.join(DOCS_DIR, "index.md")
    return os.path.join(DOCS_DIR, *slug.split("/")) + ".md"


def cache_key(slug: str) -> str:
    return "index" if slug == "" else slug


def path_from_cache_key(key: str) -> str:
    if key == "index":
        return os.path.join(DOCS_DIR, "index.md")
    return os.path.join(DOCS_DIR, *key.split("/")) + ".md"


# ---------------------------------------------------------------------------
# MDX -> markdown conversion
# ---------------------------------------------------------------------------

STR_LITERAL_DQ_RE = re.compile(r'\{\s*"((?:[^"\\]|\\.)*)"\s*\}')
STR_LITERAL_SQ_RE = re.compile(r"\{\s*'((?:[^'\\]|\\.)*)'\s*\}")
CODE_RE = re.compile(r"<code>(.*?)</code>")
A_LITERAL_RE = re.compile(r'<a\s+[^>]*?href="([^"]*)"[^>]*?>(.*?)</a>')
A_EXPR_RE = re.compile(r"<a\s+[^>]*?href=\{[^}]*\}[^>]*?>(.*?)</a>")
TABITEM_INLINE_RE = re.compile(r'<TabItem\b[^>]*?\blabel="([^"]*)"[^>]*?>')
MODULE_LINE_RE = re.compile(r"^\s*(import|export)\s")
TABITEM_LINE_RE = re.compile(r'^(\s*)<TabItem\b[^>]*?\blabel="([^"]*)"[^>]*?>\s*$')

# Starlight component lines that are dropped wholesale when alone on a line.
DROP_LINES = {"<Tabs>", "</Tabs>", "</TabItem>", "<Steps>", "</Steps>"}


def transform_inline(line: str) -> str:
    """Degrade MDX inline constructs on a single non-code line to markdown."""
    line = STR_LITERAL_DQ_RE.sub(lambda m: m.group(1), line)
    line = STR_LITERAL_SQ_RE.sub(lambda m: m.group(1), line)
    line = CODE_RE.sub(lambda m: f"`{m.group(1)}`", line)
    line = A_LITERAL_RE.sub(lambda m: f"[{m.group(2)}]({m.group(1)})", line)
    line = A_EXPR_RE.sub(lambda m: m.group(1), line)
    line = re.sub(r"</?(Tabs|Steps)>", "", line)
    line = TABITEM_INLINE_RE.sub(lambda m: f"**{m.group(1)}**", line)
    line = re.sub(r"</TabItem>", "", line)
    line = re.sub(r"</?nobr>", "", line)
    return line


def mdx_to_markdown(raw: str) -> str:
    out: list[str] = []
    in_fence = False
    marker = ""
    for line in raw.split("\n"):
        stripped = line.lstrip()
        if in_fence:
            out.append(line)
            if stripped.startswith(marker):
                in_fence = False
                marker = ""
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            marker = stripped[:3]
            out.append(line)
            continue
        # Outside fenced code from here on.
        if MODULE_LINE_RE.match(line):
            continue
        if stripped.rstrip() in DROP_LINES:
            continue
        m = TABITEM_LINE_RE.match(line)
        if m:
            out.append(f"{m.group(1)}**{html.unescape(m.group(2))}**")
            continue
        out.append(transform_inline(line))
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.lstrip("\n").rstrip() + "\n"


# Starlight asides (:::note / :::tip[Title] / ... ) -> GitHub alert blockquotes.
ASIDE_KIND = {
    "note": "NOTE",
    "tip": "TIP",
    "info": "IMPORTANT",
    "important": "IMPORTANT",
    "caution": "WARNING",
    "warning": "WARNING",
    "danger": "CAUTION",
}
ASIDE_OPEN_RE = re.compile(r"^:::(\w+)(?:\[([^\]]*)\])?\s*$")


def convert_asides(text: str) -> str:
    """Convert Starlight `:::kind` asides to GitHub alert blockquotes.

    Body lines (including any fenced code block inside the aside) are prefixed
    with `> `; fence state is tracked so a code line that looks like a `:::`
    closer is never mistaken for one.
    """
    out: list[str] = []
    in_fence = False
    marker = ""
    in_aside = False
    for line in text.split("\n"):
        stripped = line.lstrip()
        if in_fence:
            out.append("> " + line if in_aside else line)
            if stripped.startswith(marker):
                in_fence = False
                marker = ""
            continue
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = True
            marker = stripped[:3]
            out.append("> " + line if in_aside else line)
            continue
        if not in_aside:
            m = ASIDE_OPEN_RE.match(line.strip())
            if m:
                in_aside = True
                kind = ASIDE_KIND.get(m.group(1).lower(), "NOTE")
                out.append(f"> [!{kind}]")
                if m.group(2):
                    out.append(f"> **{m.group(2).strip()}**")
                continue
            out.append(line)
            continue
        if line.strip() == ":::":
            in_aside = False
            out.append("")
            continue
        out.append(">" if line.strip() == "" else "> " + line)
    text = "\n".join(out)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.rstrip() + "\n"


def build_page_markdown(raw: str, title: str, source_url: str) -> str:
    body = convert_asides(mdx_to_markdown(raw))
    return f"# {title}\n\n*Source: [{source_url}]({source_url})*\n\n{body}"


def build_readme(entries: list[dict]) -> str:
    lines = ["# OpenCode Documentation", ""]
    lines.append(f"Mirrored from [{SITE}/docs/]({SITE}/docs/).")
    lines.append("")
    lines.append(f"{len(entries)} pages.")
    lines.append("")
    order: list[str | None] = []
    grouped: dict[str | None, list[dict]] = {}
    for e in entries:
        g = e["group"]
        if g not in grouped:
            grouped[g] = []
            order.append(g)
        grouped[g].append(e)
    for g in order:
        heading = g if g else "General"
        lines.append(f"## {heading}")
        lines.append("")
        for e in grouped[g]:
            fname = "index.md" if e["slug"] == "" else f"{e['slug']}.md"
            lines.append(f"- [{e['title']}]({fname})")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    entries = discover()

    print(f"Fetching {len(entries)} pages (concurrency={MAX_WORKERS})...")
    fetched: dict[str, str] = {}
    missing: list[str] = []

    def fetch_one(entry: dict) -> tuple[dict, str | None]:
        return entry, fetch_url(md_url(entry["slug"]))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        for entry, content in pool.map(fetch_one, entries):
            if content is None:
                missing.append(entry["slug"])
            else:
                fetched[entry["slug"]] = content

    print(f"  fetched: {len(fetched)}")
    if missing:
        print(f"  unavailable (.md 404): {len(missing)}")
        if args.verbose:
            for s in sorted(missing):
                print(f"    SKIP {page_url(s)}")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}
    present: list[dict] = []  # entries with content, in discovery order

    for entry in entries:
        slug = entry["slug"]
        raw = fetched.get(slug)
        key = cache_key(slug)
        path = file_path(slug)

        prev = cache.get(key, {})
        if raw is None:
            if prev and os.path.exists(path):
                unchanged += 1
                new_cache[key] = prev
                present.append(entry)
            continue

        present.append(entry)
        content = build_page_markdown(raw, entry["title"], page_url(slug))
        content_hash = sha256(content)
        if prev.get("sha256") == content_hash and os.path.exists(path):
            unchanged += 1
            new_cache[key] = prev
            continue
        is_new = key not in cache or not os.path.exists(path)
        write_file(path, content, dry_run=args.dry_run, verbose=args.verbose,
                   label="ADD" if is_new else "UPDATE")
        new_cache[key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Top-level catalogue README (auto-generated, grouped by sidebar section).
    readme = build_readme(present)
    readme_path = os.path.join(DOCS_DIR, "README.md")
    readme_key = "__readme__"
    readme_hash = sha256(readme)
    prev = cache.get(readme_key, {})
    if prev.get("sha256") == readme_hash and os.path.exists(readme_path):
        unchanged += 1
        new_cache[readme_key] = prev
    else:
        is_new = readme_key not in cache or not os.path.exists(readme_path)
        write_file(readme_path, readme, dry_run=args.dry_run, verbose=args.verbose,
                   label="ADD" if is_new else "UPDATE")
        new_cache[readme_key] = {
            "sha256": readme_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    # Removals: any cached page no longer present.
    removed = 0
    for old_key in sorted(cache):
        if old_key in new_cache:
            continue
        old_path = (readme_path if old_key == "__readme__"
                    else path_from_cache_key(old_key))
        if not os.path.exists(old_path):
            continue
        if args.dry_run:
            print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        else:
            os.remove(old_path)
            if args.verbose:
                print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        removed += 1

    # Prune empty directories left behind by removals.
    if not args.dry_run and os.path.isdir(DOCS_DIR):
        for root, _dirs, _files in os.walk(DOCS_DIR, topdown=False):
            if root == DOCS_DIR:
                continue
            if not os.listdir(root):
                os.rmdir(root)
                if args.verbose:
                    print(f"  RMDIR {os.path.relpath(root, DOCS_DIR)}/")

    if not args.dry_run:
        save_cache(new_cache)

    groups: dict[str, int] = {}
    for e in present:
        g = e["group"] or "General"
        groups[g] = groups.get(g, 0) + 1

    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Unavailable: {len(missing)}")
    print(f"  Total pages: {len(present)}")
    for g in groups:
        print(f"    {g}: {groups[g]}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch OpenCode documentation and mirror to local markdown"
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
