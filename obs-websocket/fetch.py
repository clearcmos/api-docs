#!/usr/bin/env python3
"""Fetch obs-websocket 5.x protocol docs and mirror to local markdown.

obs-websocket publishes a machine-readable spec (`protocol.json`) and a rendered
`protocol.md`, both committed to the repo on `master`. This fetcher pulls both from
GitHub raw (stdlib only), converts the JSON spec into per-category markdown (one file per
request/event, plus an enums page), and slices the connection/auth/opcode narrative out of
protocol.md into `index.md`.

Source:
  https://github.com/obsproject/obs-websocket  (docs/generated/protocol.{json,md})

This is the runtime remote-control protocol, distinct from the sibling `obs/` fetcher,
which mirrors the libobs C plugin/scripting API.
"""
import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = "obsproject/obs-websocket"
BRANCH = "master"
RAW = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"
PROTOCOL_JSON_URL = f"{RAW}/docs/generated/protocol.json"
PROTOCOL_MD_URL = f"{RAW}/docs/generated/protocol.md"
SOURCE_LINK = f"https://github.com/{REPO}/blob/{BRANCH}/docs/generated/protocol.md"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")

USER_AGENT = "obs-websocket-api-docs-fetcher/1.0"


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except HTTPError as e:
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


def slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s or "misc"


def esc(text) -> str:
    """Escape a value for use inside a markdown table cell."""
    if text is None:
        return ""
    return str(text).replace("|", "\\|").replace("\r", " ").replace("\n", " ").strip()


def header(title: str) -> str:
    return f"*Source: [obs-websocket protocol]({SOURCE_LINK}) (generated, do not edit)*\n\n# {title}\n\n"


def meta_line(obj: dict) -> str:
    bits = [f"**Category:** {obj.get('category', 'general')}"]
    if obj.get("complexity") is not None:
        bits.append(f"**Complexity:** {obj['complexity']}/5")
    if obj.get("rpcVersion"):
        bits.append(f"**Latest RPC version:** {obj['rpcVersion']}")
    if obj.get("initialVersion"):
        bits.append(f"**Added in:** {obj['initialVersion']}")
    if obj.get("eventSubscription"):
        bits.append(f"**Subscription:** `{obj['eventSubscription']}`")
    line = " | ".join(bits) + "\n\n"
    if obj.get("deprecated"):
        line += "> [!WARNING]\n> This is deprecated and may be removed in a future version.\n\n"
    return line


def request_field_desc(f: dict) -> str:
    desc = esc(f.get("valueDescription"))
    extra = []
    if f.get("valueRestrictions"):
        extra.append(f"restrictions: {esc(f['valueRestrictions'])}")
    behavior = f.get("valueOptionalBehavior")
    if f.get("valueOptional") and behavior and behavior.strip().lower() != "unknown":
        extra.append(f"if omitted: {esc(behavior)}")
    if extra:
        desc = f"{desc} ({'; '.join(extra)})" if desc else "; ".join(extra)
    return desc


def render_request(r: dict) -> str:
    out = [header(r["requestType"]), meta_line(r)]
    if r.get("description"):
        out.append(r["description"].strip() + "\n\n")
    fields = r.get("requestFields") or []
    out.append("## Request Fields\n\n")
    if fields:
        out.append("| Name | Type | Required | Description |\n| --- | --- | --- | --- |\n")
        for f in fields:
            req = "No" if f.get("valueOptional") else "Yes"
            out.append(f"| `{esc(f['valueName'])}` | {esc(f['valueType'])} | {req} | {request_field_desc(f)} |\n")
        out.append("\n")
    else:
        out.append("_None._\n\n")
    resp = r.get("responseFields") or []
    out.append("## Response Fields\n\n")
    if resp:
        out.append("| Name | Type | Description |\n| --- | --- | --- |\n")
        for f in resp:
            out.append(f"| `{esc(f['valueName'])}` | {esc(f['valueType'])} | {esc(f.get('valueDescription'))} |\n")
        out.append("\n")
    else:
        out.append("_None._\n\n")
    return "".join(out)


def render_event(e: dict) -> str:
    out = [header(e["eventType"]), meta_line(e)]
    if e.get("description"):
        out.append(e["description"].strip() + "\n\n")
    fields = e.get("dataFields") or []
    out.append("## Data Fields\n\n")
    if fields:
        out.append("| Name | Type | Description |\n| --- | --- | --- |\n")
        for f in fields:
            out.append(f"| `{esc(f['valueName'])}` | {esc(f['valueType'])} | {esc(f.get('valueDescription'))} |\n")
        out.append("\n")
    else:
        out.append("_None._\n\n")
    return "".join(out)


def render_enums(enums: list) -> str:
    out = [header("Enumerations")]
    out.append("Enumeration values used throughout the protocol.\n\n")
    for en in enums:
        out.append(f"## {en['enumType']}\n\n")
        out.append("| Identifier | Value | Description | Added |\n| --- | --- | --- | --- |\n")
        for i in en.get("enumIdentifiers", []):
            dep = " (deprecated)" if i.get("deprecated") else ""
            out.append(
                f"| `{esc(i['enumIdentifier'])}` | {esc(i.get('enumValue'))} | "
                f"{esc(i.get('description'))}{dep} | {esc(i.get('initialVersion'))} |\n"
            )
        out.append("\n")
    return "".join(out)


def extract_intro(md: str) -> str:
    """Slice the connection/auth/opcodes narrative out of protocol.md."""
    start = md.find("## General Intro")
    end = md.find("## Enumerations")
    body = md[start:end].strip() if start != -1 and end != -1 else md.strip()
    return header("Connecting to obs-websocket") + body + "\n"


def group_by_category(items: list, type_key: str) -> dict:
    groups: dict[str, list] = {}
    for it in items:
        groups.setdefault(it.get("category", "general"), []).append(it)
    for cat in groups:
        groups[cat].sort(key=lambda x: x[type_key])
    return dict(sorted(groups.items()))


def build_files(spec: dict, protocol_md: str) -> dict:
    """Return {relpath_under_docs: content} for every generated file."""
    files: dict[str, str] = {}

    files["index.md"] = extract_intro(protocol_md)
    files["enums.md"] = render_enums(spec.get("enums", []))

    req_groups = group_by_category(spec.get("requests", []), "requestType")
    evt_groups = group_by_category(spec.get("events", []), "eventType")

    # Per-request / per-event pages
    for cat, items in req_groups.items():
        slug = slugify(cat)
        for r in items:
            files[f"requests/{slug}/{r['requestType']}.md"] = render_request(r)
    for cat, items in evt_groups.items():
        slug = slugify(cat)
        for e in items:
            files[f"events/{slug}/{e['eventType']}.md"] = render_event(e)

    # Section + per-category READMEs
    def section_readme(title, groups, kind, type_key):
        lines = [header(title)]
        total = sum(len(v) for v in groups.values())
        lines.append(f"{total} {kind} across {len(groups)} categories.\n\n")
        for cat, items in groups.items():
            slug = slugify(cat)
            lines.append(f"## {cat}\n\n")
            for it in items:
                name = it[type_key]
                dep = " _(deprecated)_" if it.get("deprecated") else ""
                desc = (it.get("description") or "").strip().split("\n")[0]
                lines.append(f"- [{name}]({slug}/{name}.md){dep} - {desc}\n")
            lines.append("\n")
        return "".join(lines)

    files["requests/README.md"] = section_readme("Requests", req_groups, "requests", "requestType")
    files["events/README.md"] = section_readme("Events", evt_groups, "events", "eventType")

    for cat, items in req_groups.items():
        slug = slugify(cat)
        lines = [header(f"Requests: {cat}")]
        for r in items:
            desc = (r.get("description") or "").strip().split("\n")[0]
            lines.append(f"- [{r['requestType']}]({r['requestType']}.md) - {desc}\n")
        files[f"requests/{slug}/README.md"] = "".join(lines)
    for cat, items in evt_groups.items():
        slug = slugify(cat)
        lines = [header(f"Events: {cat}")]
        for e in items:
            desc = (e.get("description") or "").strip().split("\n")[0]
            lines.append(f"- [{e['eventType']}]({e['eventType']}.md) - {desc}\n")
        files[f"events/{slug}/README.md"] = "".join(lines)

    # Top-level catalogue
    top = [header("obs-websocket Protocol")]
    top.append(
        "Runtime remote-control protocol for OBS Studio (obs-websocket 5.x). Mirrored from "
        f"the [generated spec]({SOURCE_LINK}).\n\n"
        "For the libobs C plugin/scripting API instead, see the sibling `obs/` docs.\n\n"
    )
    top.append("## Protocol basics\n\n")
    top.append("- [Connecting, authentication & OpCodes](index.md)\n")
    top.append("- [Enumerations](enums.md)\n\n")
    top.append(f"## Requests ({sum(len(v) for v in req_groups.values())})\n\n")
    for cat, items in req_groups.items():
        slug = slugify(cat)
        names = ", ".join(f"[{r['requestType']}](requests/{slug}/{r['requestType']}.md)" for r in items)
        top.append(f"- **{cat}** ([index](requests/{slug}/README.md)): {names}\n")
    top.append(f"\n## Events ({sum(len(v) for v in evt_groups.values())})\n\n")
    for cat, items in evt_groups.items():
        slug = slugify(cat)
        names = ", ".join(f"[{e['eventType']}](events/{slug}/{e['eventType']}.md)" for e in items)
        top.append(f"- **{cat}** ([index](events/{slug}/README.md)): {names}\n")
    files["README.md"] = "".join(top)

    return files


def sync(args: argparse.Namespace) -> None:
    cache = {} if args.force else load_cache()

    print("Fetching protocol.json and protocol.md from GitHub...")
    raw_json = fetch_url(PROTOCOL_JSON_URL)
    raw_md = fetch_url(PROTOCOL_MD_URL)
    if not raw_json or not raw_md:
        print("ERROR: failed to fetch protocol sources", file=sys.stderr)
        sys.exit(1)
    try:
        spec = json.loads(raw_json)
    except ValueError as e:
        print(f"ERROR: protocol.json did not parse: {e}", file=sys.stderr)
        sys.exit(1)
    print(
        f"  requests: {len(spec.get('requests', []))}  "
        f"events: {len(spec.get('events', []))}  enums: {len(spec.get('enums', []))}"
    )

    files = build_files(spec, raw_md)
    print(f"Generating {len(files)} markdown files...")

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}

    for rel, content in sorted(files.items()):
        path = os.path.join(DOCS_DIR, rel)
        content_hash = sha256(content)
        prev = cache.get(rel, {})
        if prev.get("sha256") == content_hash and os.path.exists(path):
            unchanged += 1
            new_cache[rel] = prev
            continue
        is_new = rel not in cache or not os.path.exists(path)
        write_file(path, content, dry_run=args.dry_run, verbose=args.verbose,
                   label="ADD" if is_new else "UPDATE")
        new_cache[rel] = {"sha256": content_hash, "last_updated": datetime.now(timezone.utc).isoformat()}
        if is_new:
            added += 1
        else:
            updated += 1

    # Removals: cache keys no longer generated
    removed = 0
    for old in sorted(cache):
        if old in new_cache:
            continue
        old_path = os.path.join(DOCS_DIR, old)
        if not os.path.exists(old_path):
            continue
        if args.dry_run:
            print(f"  REMOVE {old}")
        else:
            os.remove(old_path)
            if args.verbose:
                print(f"  REMOVE {old}")
        removed += 1

    if not args.dry_run:
        # prune now-empty category directories
        for root, dirs, filenames in os.walk(DOCS_DIR, topdown=False):
            if not filenames and not os.listdir(root) and root != DOCS_DIR:
                os.rmdir(root)
        save_cache(new_cache)

    print("\nSync complete:")
    print(f"  Added:     {added}")
    print(f"  Updated:   {updated}")
    print(f"  Unchanged: {unchanged}")
    print(f"  Removed:   {removed}")
    print(f"  Total:     {len(files)}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch obs-websocket 5.x protocol docs and mirror to local markdown"
    )
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing")
    parser.add_argument("--force", action="store_true", help="Regenerate everything ignoring cache")
    parser.add_argument("--verbose", action="store_true", help="Detailed per-file logging")
    sync(parser.parse_args())


if __name__ == "__main__":
    main()
