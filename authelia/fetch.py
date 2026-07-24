#!/usr/bin/env python3

"""
Authelia Documentation Fetcher

Mirrors the Authelia docs (https://www.authelia.com) from the Hugo source in
the authelia/authelia GitHub repository (docs/content). The site serves
text/markdown alternates at {page}/index.md and an llms.txt, but both are
unsuitable as the primary source: the .md alternates strip frontmatter and
mangle the H1 (title and description are concatenated), and llms.txt omits
every page deeper than two section levels (~300 pages, including all OpenID
Connect client integration guides and the entire CLI reference). The repo
tree is complete, and the frontmatter carries title/description/weight for
proper page headers and index ordering.

Hugo shortcodes are expanded inline: {{< sitevar >}} substitutions use their
nojs fallbacks, {{< confkey >}} option metadata becomes a definition line,
{{< callout >}} becomes a GitHub alert blockquote, tab groups become bold
labels, and the data-driven table shortcodes (config keys, i18n locales,
TOTP app support, PBKDF2 variants, CSP template) are rendered from the
repo's docs/data/*.json files. Anything unrecognized is left as-is and
reported in the run summary.

Sections mirrored: overview, configuration, integration, contributing,
blog, roadmap, reference (policies, information, and contributors are
intentionally skipped).
"""

import argparse
import gzip
import hashlib
import json
import os
import posixpath
import re
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO = "authelia/authelia"
BRANCH = "master"
CONTENT_PREFIX = "docs/content/"
DATA_PREFIX = "docs/data/"
SITE = "https://www.authelia.com"

SECTIONS = ("overview", "configuration", "integration", "contributing",
            "blog", "roadmap", "reference")

TREE_URL = f"https://api.github.com/repos/{REPO}/git/trees/{BRANCH}?recursive=1"
RAW_BASE = f"https://raw.githubusercontent.com/{REPO}/{BRANCH}"

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DOCS_DIR = os.path.join(SCRIPT_DIR, "docs")
CACHE_FILE = os.path.join(SCRIPT_DIR, ".cache.json")
SOURCE_CACHE_KEY = "__source__/tree"

MAX_WORKERS = 12


def sha256(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def fetch_url(url: str, timeout: int = 60) -> str | None:
    headers = {
        "User-Agent": "authelia-docs-fetcher/1.0",
        "Accept-Encoding": "gzip",
    }
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
    req = Request(url, headers=headers)
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
# Frontmatter
# ---------------------------------------------------------------------------

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """Tiny YAML-ish parser: top-level scalars, flow lists, and block lists.

    Nested mappings (e.g. the seo: block) are skipped. Values keep only what
    the fetcher needs as plain strings.
    """
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}, text
    meta: dict = {}
    list_key: str | None = None
    for line in m.group(1).splitlines():
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip(" "))
        s = line.strip()
        if indent:
            if list_key and s.startswith("- "):
                meta[list_key].append(s[2:].strip().strip("'\""))
            continue
        list_key = None
        if s.startswith("#") or ":" not in s:
            continue
        key, _, value = s.partition(":")
        key = key.strip()
        value = value.split(" #", 1)[0].strip()
        if value == "":
            meta[key] = []
            list_key = key
        elif value.startswith("[") and value.endswith("]"):
            inner = value[1:-1].strip()
            meta[key] = ([v.strip().strip("'\"") for v in inner.split(",")]
                         if inner else [])
        else:
            meta[key] = value.strip("'\"")
    return meta, text[m.end():]


# ---------------------------------------------------------------------------
# Shortcode expansion
# ---------------------------------------------------------------------------

ATTR_RE = re.compile(r'([a-zA-Z][\w-]*)\s*=\s*"([^"]*)"')


def parse_attrs(s: str) -> dict[str, str]:
    return dict(ATTR_RE.findall(s))


SC = r"\{\{[<%]\s*"   # shortcode open
CS = r"\s*[%>]\}\}"   # shortcode close

PRINT_RE = re.compile(SC + r'print\s+"((?:[^"\\]|\\.)*)"' + CS)
SITEVAR_RE = re.compile(SC + r"sitevar\s+([^>%]*?)" + CS)
LATEST_RE = re.compile(SC + r"latest" + CS)
GITHUB_LINK_RE = re.compile(SC + r"github-link\b([^>%]*?)" + CS)
SUPPORT_RE = re.compile(SC + r"support\b([^>%]*?)" + CS)
# confkey values may contain ">" (e.g. default="<thumbprint of public key>"),
# so match whole attribute pairs rather than excluding ">".
CONFKEY_RE = re.compile(SC + r'confkey\b((?:\s+[\w-]+="[^"]*")*)' + CS)
ROADMAP_RE = re.compile(SC + r"roadmap-status\b([^>%]*?)" + CS)
FIGURE_RE = re.compile(SC + r"(figure|picture|inline-svg)\b([^>%]*?)" + CS)

CALLOUT_OPEN_RE = re.compile(SC + r"callout\b([^>%]*?)" + CS)
CALLOUT_CLOSE_RE = re.compile(SC + r"/callout" + CS)
TABS_RE = re.compile(SC + r'/?(?:envTabs|sessionTabs)(?:\s+"[^"]*")?' + CS)
TAB_OPEN_RE = re.compile(SC + r'(?:envTab|sessionTab)\s+"([^"]+)"' + CS)
TAB_CLOSE_RE = re.compile(SC + r"/(?:envTab|sessionTab)" + CS)
DETAILS_OPEN_RE = re.compile(SC + r'details\s+"([^"]+)"(?:\s+"[^"]*")?' + CS)
DETAILS_CLOSE_RE = re.compile(SC + r"/details" + CS)

FIGURE_ML_RE = re.compile(SC + r"(figure|picture|inline-svg)\b(.*?)" + CS,
                          re.DOTALL)
OIDC_COMMON_RE = re.compile(SC + r"oidc-common\b([^>%]*?)" + CS)
OIDC_ESCAPE_RE = re.compile(
    SC + r"oidc-escape-hatch-claims-hydration\b([^>%]*?)" + CS)
CONFIG_ALERT_RE = re.compile(SC + r"config-alert-example" + CS)
SITEVAR_PREFS_RE = re.compile(SC + r"sitevar-preferences" + CS)
CSP_RE = re.compile(SC + r"csp" + CS)
TABLE_CONFIG_KEYS_RE = re.compile(SC + r"table-config-keys\b([^>%]*?)" + CS)
TABLE_I18N_LOCALES_RE = re.compile(SC + r"table-i18n-locales" + CS)
TABLE_I18N_OVERRIDES_RE = re.compile(SC + r"table-i18n-overrides" + CS)
TABLE_TOTP_RE = re.compile(SC + r"table-totp-support" + CS)
PBKDF2_ITER_RE = re.compile(SC + r"hashing-pbkdf2-iterations" + CS)
PBKDF2_VARIANTS_RE = re.compile(SC + r"hashing-pbkdf2-variants" + CS)
SUPPORTED_PRODUCT_RE = re.compile(SC + r"supported-product\b([^>%]*?)" + CS)

FENCE_RE = re.compile(r"^\s*(`{3,})")
FENCE_TITLE_RE = re.compile(
    r'^(\s*)(`{3,})([A-Za-z0-9_+-]*)\s*\{\s*title\s*=\s*"?([^"}]+?)"?\s*\}\s*$')
CODE_SPAN_RE = re.compile(r"(`+)[^`]*?\1")
LEFTOVER_RE = re.compile(r"\{\{[<%]")

ALERT_KIND = {"note": "NOTE", "tip": "TIP", "caution": "WARNING",
              "danger": "CAUTION", "warning": "WARNING", "info": "NOTE"}
SUPPORT_TEXT = {"full": "Full", "partial": "Partial", "legacy": "Legacy",
                "unknown": "Unknown"}
ROADMAP_TEXT = {"in-progress": "in progress", "needs-design": "needs design",
                "waiting": "waiting", "complete": "complete",
                "abandoned": "abandoned"}

CONFIG_ALERT_LINES = [
    "> [!NOTE]",
    "> **Example Configuration**",
    ">",
    "> This section is intended as an example configuration to help users "
    "with a rough contextual layout of this configuration section, it is "
    "not intended to explain the options. The configuration shown may not "
    "be a valid configuration, and you should see the "
    "[options section](#options) below and the navigation links to "
    "properly understand each option individually.",
]


def mask_code_spans(line: str) -> str:
    """Blank out inline code spans so marker detection ignores them."""
    return CODE_SPAN_RE.sub(lambda m: " " * len(m.group(0)), line)


def render_confkey(attrs: dict[str, str]) -> str:
    parts: list[str] = []
    ctype = attrs.get("type", "string")
    parts.append("Type: " + " or ".join(f"`{t.strip()}`"
                                        for t in ctype.split(",") if t.strip()))
    syntax = attrs.get("syntax", "")
    structure = attrs.get("structure", "")
    ref = attrs.get("common", "")
    if not ref:
        if ctype == "structure" and structure:
            ref = structure
        elif syntax in ("duration", "address", "network"):
            ref = syntax
    common_url = "/configuration/prologue/common/#"
    if syntax:
        if ref:
            parts.append(f"Syntax: [{syntax}]({common_url}{ref})")
        else:
            parts.append(f"Syntax: `{syntax}`")
    if structure:
        parts.append(f"Structure: [{structure}]({common_url}{ref or structure})")
    if "default" in attrs:
        parts.append(f"Default: `{attrs['default']}`")
    required = {"no": "no", "situational": "situational"}.get(
        attrs.get("required"), "yes")
    parts.append(f"Required: {required}")
    line = "*" + " | ".join(parts) + "*"
    if attrs.get("secret") == "yes":
        line += ("\n\n*This option can also be defined using a "
                 "[secret](/configuration/methods/secrets/) which is "
                 "strongly recommended.*")
    return line


def render_support(attrs: dict[str, str]) -> str:
    text = attrs.get("title") or SUPPORT_TEXT.get(attrs.get("support", ""), "No")
    link = attrs.get("link")
    return f"[{text}]({link})" if link else text


def render_roadmap(attrs: dict[str, str]) -> str:
    text = ROADMAP_TEXT.get(attrs.get("stage", ""), "not started")
    version = attrs.get("version")
    return f"**Status:** {text} ({version})" if version else f"**Status:** {text}"


def render_figure(kind: str, attrs: dict[str, str], page: dict) -> str:
    src = attrs.get("src", "")
    alt = attrs.get("alt", "") or attrs.get("caption", "")
    if kind == "inline-svg":
        # Site inlines these from the asset pipeline; there is no stable URL.
        return f"*{alt}*" if alt else ""
    if src.startswith("/"):
        url = SITE + src
    else:
        url = f"{SITE}/{posixpath.normpath(posixpath.join(page['link_base'], src))}"
    out = f"![{alt}]({url})"
    if attrs.get("caption"):
        out += f"\n\n*{attrs['caption']}*"
    return out


OIDC_CORE = "https://openid.net/specs/openid-connect-core-1_0.html"
OIDC_FAQ = "/integration/openid-connect/frequently-asked-questions/"
OIDC_CONFIG = "/configuration/identity-providers/openid-connect"

OIDC_BUG_NOTES = {
    "claims-hydration": (
        f"> **Claims Hydration:** this client outright does not support "
        f"[OpenID Connect 1.0]({OIDC_CORE}) as it does not honor the expected "
        "process to retrieve the claims it needs to access. The workaround is "
        "documented in "
        "[Configuration Escape Hatch](#configuration-escape-hatch)."),
    "client-credentials-encoding": (
        "> **Client Credentials Encoding:** this client does not properly "
        "encode the client credentials before using them for authentication "
        "as per [RFC6749 Appendix B]"
        "(https://datatracker.ietf.org/doc/html/rfc6749#appendix-B). It is "
        "required that the Client ID and Client Secret are both URL Escaped "
        "before being used for both the `client_secret_post` and "
        "`client_secret_basic` authentication mechanisms. Avoiding special "
        "characters in both the Client ID and Client Secret or URL Escaping "
        "them before adding them to the clients configuration are the only "
        "workarounds. Authelia's random password generator will "
        "automatically output both a normal version and a pre-encoded "
        "version which you could utilize."),
    "claim-binding": (
        f"> **Claim Binding:** this client outright does not support "
        f"[OpenID Connect 1.0]({OIDC_CORE}) as it does not bind the identity "
        "provider identity (the `sub` and `iss` claims which are guaranteed "
        "not to change) to local accounts, instead it uses claims like "
        "`email` and `preferred_username` which is a vulnerability that "
        "could result in a simple privilege escalation. The developer has "
        "been made aware of this vulnerability but has decided not to fix "
        "it. See [OpenID Connect 1.0 Section 5.7 Claim Stability and "
        f"Uniqueness]({OIDC_CORE}#ClaimStability) for more information."),
}


def render_oidc_common(attrs: dict[str, str]) -> list[str]:
    """Expansion of the shared 'Before You Begin' include on every OpenID
    Connect 1.0 client integration guide. The relative faq/config defaults
    in the upstream template resolve to these absolute paths on the
    rendered site."""
    faq = attrs.get("faq") or OIDC_FAQ
    config = attrs.get("config") or OIDC_CONFIG
    faq_generate = f"{faq}#how-do-i-generate-a-client-identifier-or-client-secret"
    lines = [
        "## Before You Begin",
        "",
        "> [!WARNING]",
        "> **Important Reading**",
        ">",
        "> This section contains important elements that you should "
        "carefully consider before configuration of an OpenID Connect 1.0 "
        "Registered Client.",
        "",
    ]
    bugs = [b.strip() for b in attrs.get("bugs", "").split(",") if b.strip()]
    if bugs:
        lines += [
            "### Known Bugs",
            "",
            "> [!CAUTION]",
            "> **Client Has Known Significant Bugs**",
            ">",
            "> Unfortunately at the time this guide was last modified (noted "
            "at the bottom of the guide) this third-party application has "
            "bugs which are significant and indicate either a fairly low "
            f"level of support for [OpenID Connect 1.0]({OIDC_CORE}) or no "
            "effective support at all. This guide may have workarounds to "
            "adapt to this but this is done solely on a best effort basis. "
            "The developers of the application should be encouraged to fix "
            "these bugs.",
        ]
        for bug in bugs:
            if bug in OIDC_BUG_NOTES:
                lines += [">", OIDC_BUG_NOTES[bug]]
        lines.append("")
    lines += [
        "### Common Notes",
        "",
        f"1. The [OpenID Connect 1.0]({OIDC_CORE}) `client_id` parameter:",
        "    1. This *__must__* be a unique value for every client.",
        "    2. The value used in this guide is merely for readability and "
        "demonstration purposes and you *__should not__* use this value in "
        "production and should instead utilize the [How do I generate a "
        f"client identifier or client secret?]({faq_generate}) FAQ. We "
        "recommend 64 random characters but you can use any arbitrary value "
        "that meets the other criteria.",
        "    3. This *__must__* only contain [RFC3986 Unreserved Characters]"
        "(https://datatracker.ietf.org/doc/html/rfc3986#section-2.3).",
        "    4. This *__must__* be no more than 100 characters in length.",
        f"2. The [OpenID Connect 1.0]({OIDC_CORE}) `client_secret` parameter:",
        "    1. The value used in this guide is merely for demonstration "
        "purposes and you *__should absolutely not__* use this value in "
        "production and should instead utilize the [How do I generate a "
        f"client identifier or client secret?]({faq_generate}) FAQ.",
        "    2. This string may be stored as plaintext in the Authelia "
        "configuration but this behavior is deprecated and is not guaranteed "
        "to be supported in the future. See the "
        f"[Plaintext]({faq}#plaintext) guide for more information.",
        "    3. When the secret is stored in hashed form in the Authelia "
        "configuration (*__heavily recommended__*), the cost of hashing "
        "can, if too great, cause timeouts for clients. See the "
        f"[Tuning the work factors]({faq}#tuning-work-factors) guide for "
        "more information.",
        "3. The configuration example for Authelia:",
        "    1. Only contains an example configuration for the client "
        "registration and you *__MUST__* also configure the required "
        "elements from the [OpenID Connect 1.0 Provider Configuration]"
        f"({config}/provider/) guide.",
        "    2. Only contains a small portion of all of the available "
        "options for a registered client and users may wish to configure "
        "portions that are not part of this guide or configure them "
        "differently, as such it's important to both familiarize yourself "
        "with the other options available and the effect of each of the "
        "options configured in this section by looking at the "
        f"[OpenID Connect 1.0 Clients Configuration]({config}/clients/) "
        "guide.",
    ]
    return lines


def render_oidc_escape_hatch(attrs: dict[str, str]) -> list[str]:
    client_id = attrs.get("client_id") or "example"
    policy = attrs.get("policy_name") or client_id
    claims = attrs.get("claims") or ("rat,groups,email,email_verified,"
                                     "alt_emails,preferred_username,name")
    lines = [
        "> [!TIP]",
        "> **Potential Escape Hatch Configuration Required**",
        ">",
        "> Unfortunately at the time of writing this integration this "
        f"client does not support [OpenID Connect 1.0]({OIDC_CORE}). "
        "Fortunately Authelia has implemented an escape hatch that works "
        "for most clients which don't properly support "
        f"[OpenID Connect 1.0]({OIDC_CORE}). This requires additional "
        "configuration to that which is described above. You can read more "
        "about this in the [OpenID Connect 1.0 Claims Guide]"
        "(/integration/openid-connect/openid-connect-1.0-claims/"
        "#restore-functionality-prior-to-claims-parameter).",
        ">",
        "> Clients are required to operate under the assumption that claims "
        "requested by scope values are available by using the Access Token "
        "(the scope is granted and issued to the Access Token) at the "
        "UserInfo Endpoint as described by [5.4. Requesting Claims using "
        f"Scope Values]({OIDC_CORE}#ScopeClaims) with the exception of an "
        "Implicit Flow that does not return an Access Token, or explicitly "
        "request them via the claims parameter as described by "
        "[5.5. Requesting Claims using the \"claims\" Request Parameter]"
        f"({OIDC_CORE}#ClaimsParameter).",
        ">",
        "> The requirement to use this option is also often a clear "
        "indication the client also ignores the [claims stability "
        f"requirements]({OIDC_CORE}#ClaimStability) which only allows "
        "clients to anchor accounts via the `sub` and `iss` claims. This "
        "requirement is strictly required by the specification.",
        ">",
        "> Both of these elements are clear indications the client does not "
        f"properly support [OpenID Connect 1.0]({OIDC_CORE}) and is not "
        "conformant.",
        "",
        "The following is an example of adaptation to the above "
        "configuration that works around the fact this client does not "
        f"support [OpenID Connect 1.0]({OIDC_CORE}):",
    ]
    if attrs.get("example") != "disable":
        quoted = ", ".join(f"'{c.strip()}'" for c in claims.split(",") if c.strip())
        lines += [
            "",
            "```yaml",
            "identity_providers:",
            "  oidc:",
            "    claims_policies:",
            f"      {policy}:",
            f"        id_token: [{quoted}]",
            "    clients:",
            f"      - client_id: '{client_id}'",
            f"        claims_policy: '{policy}'",
            "```",
        ]
    return lines


def md_table(headers: list[str], rows: list[list[str]]) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |",
             "|" + "|".join("---" for _ in headers) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return lines


class Converter:
    """Expands Hugo shortcodes in one page body and rewrites links."""

    def __init__(self, data: dict, pages_by_url: dict, aliases: dict):
        self.data = data
        self.latest = data.get("misc", {}).get("latest", BRANCH)
        self.pages_by_url = pages_by_url
        self.aliases = aliases
        self.unconverted: list[tuple[str, str]] = []

    # -- block-level expansions ---------------------------------------------

    def expand_block(self, line: str, page: dict) -> list[str] | None:
        """Expansion for shortcodes that occupy their own line."""
        s = line.strip()
        if CONFIG_ALERT_RE.fullmatch(s):
            return CONFIG_ALERT_LINES
        m = OIDC_COMMON_RE.fullmatch(s)
        if m:
            return render_oidc_common(parse_attrs(m.group(1)))
        m = OIDC_ESCAPE_RE.fullmatch(s)
        if m:
            return render_oidc_escape_hatch(parse_attrs(m.group(1)))
        if SITEVAR_PREFS_RE.fullmatch(s):
            return []
        if CSP_RE.fullmatch(s):
            csp = self.data.get("misc", {}).get("csp", {})
            return [f"**Placeholder Value:** `{csp.get('nonce', '')}`",
                    "",
                    f"**Default Template:** `{csp.get('default', '')}`"]
        m = TABLE_CONFIG_KEYS_RE.fullmatch(s)
        if m:
            secrets = parse_attrs(m.group(1)).get("secrets") == "true"
            keys = self.data.get("configkeys", [])
            rows = [[f"`{e['path']}`", f"`{e['env']}`"]
                    for e in keys if bool(e.get("secret")) == secrets]
            return md_table(["Configuration Key", "Environment Variable"], rows)
        if TABLE_I18N_LOCALES_RE.fullmatch(s):
            langs = self.data.get("languages", {}).get("languages", [])
            rows = [[l.get("display", ""), l.get("locale", ""),
                     ", ".join(l.get("namespaces", [])),
                     ", ".join(l.get("fallbacks", []))] for l in langs]
            return md_table(["Language", "Locale", "Namespaces", "Fallbacks"], rows)
        if TABLE_I18N_OVERRIDES_RE.fullmatch(s):
            langs = self.data.get("languages", {}).get("languages", [])
            rows = [[l.get("display", ""), l.get("locale", ""),
                     f"`locales/{l.get('locale', '')}/*.json`"] for l in langs]
            return md_table(["Language", "Locale", "Override Path"], rows)
        if TABLE_TOTP_RE.fullmatch(s):
            apps = self.data.get("support", {}).get("totp", [])
            rows = []
            for a in apps:
                alg = a.get("algorithms", {})
                dig = a.get("digits", {})
                yn = lambda v: "Yes" if v else "No"
                rows.append([f"[{a.get('name', '')}]({a.get('url', '')})",
                             yn(alg.get("SHA1")), yn(alg.get("SHA256")),
                             yn(alg.get("SHA512")),
                             yn(dig.get("six")), yn(dig.get("eight"))])
            return md_table(["Application", "SHA1", "SHA256", "SHA512",
                             "6 digits", "8 digits"], rows)
        variants = (self.data.get("misc", {})
                    .get("hashing_algorithms", {})
                    .get("pbkdf2", {}).get("variants", {}))
        if PBKDF2_ITER_RE.fullmatch(s):
            rows = [[f"`{v}`", str(d.get("default_iterations", ""))]
                    for v, d in variants.items()]
            return md_table(["Variant", "Default Iterations"], rows)
        if PBKDF2_VARIANTS_RE.fullmatch(s):
            rows = [[f"`{v}`", str(d.get("fips", "")),
                     str(d.get("default_iterations", ""))]
                    for v, d in variants.items()]
            return md_table(["Variant", "FIPS 140", "Iterations"], rows)
        m = SUPPORTED_PRODUCT_RE.fullmatch(s)
        if m:
            attrs = parse_attrs(m.group(1))
            versions = (self.data.get("misc", {}).get("support", {})
                        .get(attrs.get("product", ""), []))
            fmt = attrs.get("format", "$version")
            return [fmt.replace("$version", str(v)) for v in versions]
        return None

    # -- inline expansions ----------------------------------------------------

    def sub_inline(self, line: str, page: dict) -> str:
        def gh_link(m: re.Match) -> str:
            attrs = parse_attrs(m.group(1))
            repo = attrs.get("repo", REPO)
            branch = attrs.get("branch", f"v{self.latest}")
            path = attrs.get("path", "")
            url = f"https://github.com/{repo}/blob/{branch}/{path}"
            name = attrs.get("name") or (path if repo == REPO else url)
            return f"[{name}]({url})"

        line = GITHUB_LINK_RE.sub(gh_link, line)
        line = SUPPORT_RE.sub(lambda m: render_support(parse_attrs(m.group(1))), line)
        line = CONFKEY_RE.sub(lambda m: render_confkey(parse_attrs(m.group(1))), line)
        line = ROADMAP_RE.sub(lambda m: render_roadmap(parse_attrs(m.group(1))), line)
        return line

    # -- main pass ------------------------------------------------------------

    def convert(self, body: str, page: dict) -> str:
        prints: list[str] = []

        def protect_print(m: re.Match) -> str:
            prints.append("`" + m.group(1).replace('\\"', '"') + "`")
            return f"\x00P{len(prints) - 1}\x00"

        text = PRINT_RE.sub(protect_print, body)

        # Escaped shortcodes ({{</* ... */>}}) inside tab inners survive the
        # first Hugo render pass and execute on the second; treat them as
        # live shortcodes.
        text = text.replace("{{</*", "{{<").replace("*/>}}", ">}}")

        # Global substitutions, applied inside code fences too (config
        # examples and shell commands rely on them).
        text = SITEVAR_RE.sub(
            lambda m: parse_attrs(m.group(1)).get("nojs", ""), text)
        text = LATEST_RE.sub(self.latest, text)
        # figure/picture calls sometimes spread attributes across lines.
        text = FIGURE_ML_RE.sub(
            lambda m: render_figure(m.group(1), parse_attrs(m.group(2)), page),
            text)

        out: list[str] = []
        lines = text.split("\n")
        i = 0
        in_fence = False
        fence_marker = ""
        callout: dict | None = None  # {"kind", "title", "lines"}

        def emit(rendered: str | list[str]) -> None:
            chunk = rendered if isinstance(rendered, list) else rendered.split("\n")
            if callout is not None:
                callout["lines"].extend(chunk)
            else:
                out.extend(chunk)

        def flush_callout() -> None:
            body_lines = callout["lines"]
            while body_lines and not body_lines[0].strip():
                body_lines.pop(0)
            while body_lines and not body_lines[-1].strip():
                body_lines.pop()
            quoted = [f"> [!{callout['kind']}]"]
            if callout["title"]:
                quoted.append(f"> **{callout['title']}**")
                quoted.append(">")
            for bl in body_lines:
                quoted.append(f"> {bl}".rstrip())
            out.extend(quoted)

        while i < len(lines):
            line = lines[i]
            i += 1

            if in_fence:
                emit(line)
                fm = FENCE_RE.match(line)
                if fm and line.strip() == fm.group(1) and \
                        len(fm.group(1)) >= len(fence_marker):
                    in_fence = False
                continue

            fm = FENCE_RE.match(line)
            if fm:
                in_fence = True
                fence_marker = fm.group(1)
                tm = FENCE_TITLE_RE.match(line)
                if tm:
                    indent, fence, lang, title = tm.groups()
                    emit(f"{indent}**{title}**")
                    emit("")
                    emit(f"{indent}{fence}{lang}")
                else:
                    emit(line)
                continue

            masked = mask_code_spans(line)

            m = CALLOUT_OPEN_RE.search(masked)
            if m and callout is None:
                pre = line[:m.start()].rstrip()
                if pre:
                    emit(pre)
                attrs = parse_attrs(line[m.start():m.end()])
                callout = {"kind": ALERT_KIND.get(attrs.get("context", ""), "NOTE"),
                           "title": attrs.get("title", ""), "lines": []}
                post = line[m.end():].strip()
                if post:
                    callout["lines"].append(post)
                continue
            m = CALLOUT_CLOSE_RE.search(masked)
            if m and callout is not None:
                post = line[m.end():].strip()
                pre = line[:m.start()].strip()
                if pre:
                    callout["lines"].append(pre)
                flush_callout()
                callout = None
                if post:
                    out.append(post)
                continue

            if TABS_RE.fullmatch(masked.strip()):
                continue
            tm = TAB_OPEN_RE.fullmatch(masked.strip())
            if tm:
                label = TAB_OPEN_RE.fullmatch(line.strip()).group(1)
                emit(f"**{label}**")
                emit("")
                continue
            if TAB_CLOSE_RE.fullmatch(masked.strip()):
                emit("")
                continue
            dm = DETAILS_OPEN_RE.fullmatch(masked.strip())
            if dm:
                label = DETAILS_OPEN_RE.fullmatch(line.strip()).group(1)
                emit(f"**{label}**")
                emit("")
                continue
            if DETAILS_CLOSE_RE.fullmatch(masked.strip()):
                emit("")
                continue

            if LEFTOVER_RE.search(masked):
                block = self.expand_block(line, page)
                if block is not None:
                    emit(block)
                    continue
                line = self.sub_inline(line, page)
                if LEFTOVER_RE.search(mask_code_spans(line)):
                    self.unconverted.append((page["url"], line.strip()[:80]))
            emit(line)

        if callout is not None:  # unterminated; flush what we have
            flush_callout()

        text = "\n".join(out)
        text = self.rewrite_links(text, page)
        for idx, literal in enumerate(prints):
            text = text.replace(f"\x00P{idx}\x00", literal)
        return text

    # -- link rewriting ---------------------------------------------------------

    LINK_RE = re.compile(r"(!?)\[([^\]]*)\]\(([^()\s]+)\)")
    REFDEF_RE = re.compile(r"^(\s{0,3}\[[^\]]+\]:\s*)(\S+)(.*)$")
    ASSET_EXT_RE = re.compile(
        r"\.(png|jpe?g|gif|webp|svg|ico|pdf|ya?ml|json|txt|zip|tar\.gz)$", re.I)

    def map_target(self, target: str, page: dict) -> str | None:
        """Return a replacement link target, or None to leave it alone."""
        if target.startswith(SITE):
            rest = target[len(SITE):] or "/"
            mapped = self.map_target(rest, page)
            return mapped
        if target.startswith(("http://", "https://", "mailto:", "tel:", "#")):
            return None
        path, _, frag = target.partition("#")
        frag = f"#{frag}" if frag else ""
        if not path:
            return None
        bare_word = False
        if path.startswith("/"):
            key = path.strip("/")
        else:
            bare_word = ("/" not in path and not path.endswith(".md")
                         and not path.startswith("."))
            key = posixpath.normpath(
                posixpath.join(page["link_base"], path)).strip("/")
            if key == ".":
                key = ""
        url_key = key
        if url_key.endswith(".md"):
            url_key = url_key[:-3]
            if url_key.endswith("/index"):
                url_key = url_key[: -len("/index")]
            elif url_key.endswith("/_index"):
                url_key = url_key[: -len("/_index")]
        url_key = url_key.strip("/")
        canon = url_key if url_key in self.pages_by_url else self.aliases.get(url_key)
        if canon and canon in self.pages_by_url:
            start = posixpath.dirname(page["url"]) or "."
            return posixpath.relpath(canon + ".md", start) + frag
        if bare_word:
            # Unresolvable bare relative word (e.g. footnote-ish text);
            # leave it alone.
            return None
        if self.ASSET_EXT_RE.search(path):
            if path.startswith("/"):
                return SITE + path
            return f"{SITE}/{key}"
        if not url_key:
            return None
        return f"{SITE}/{url_key}/{frag}"

    def rewrite_links(self, text: str, page: dict) -> str:
        def repl(m: re.Match) -> str:
            mapped = self.map_target(m.group(3), page)
            if mapped is None:
                return m.group(0)
            return f"{m.group(1)}[{m.group(2)}]({mapped})"

        def sub_line(line: str) -> str:
            rd = self.REFDEF_RE.match(line)
            if rd and not rd.group(0).lstrip().startswith("[^"):
                mapped = self.map_target(rd.group(2), page)
                if mapped is not None:
                    return f"{rd.group(1)}{mapped}{rd.group(3)}"
                return line
            # Skip links living entirely inside an inline code span, but
            # still rewrite links whose label merely contains one
            # (e.g. [`key`](target)).
            spans = [cm.span() for cm in CODE_SPAN_RE.finditer(line)]

            def guarded(m: re.Match) -> str:
                if any(s <= m.start() and m.end() <= e for s, e in spans):
                    return m.group(0)
                return repl(m)

            return self.LINK_RE.sub(guarded, line)

        def fence_token(line: str) -> tuple[str, bool] | None:
            # Strip blockquote prefixes so fences inside converted callouts
            # are still recognized.
            s = re.sub(r"^(\s*>)*\s*", "", line)
            m = re.match(r"^(`{3,})(.*)$", s)
            if not m:
                return None
            return m.group(1), m.group(2).strip() == ""

        out: list[str] = []
        in_fence = False
        fence_marker = ""
        for line in text.split("\n"):
            token = fence_token(line)
            if token:
                marker, bare = token
                if not in_fence:
                    in_fence = True
                    fence_marker = marker
                elif bare and len(marker) >= len(fence_marker):
                    in_fence = False
                out.append(line)
                continue
            out.append(line if in_fence else sub_line(line))
        return "\n".join(out)


# ---------------------------------------------------------------------------
# Discovery and page model
# ---------------------------------------------------------------------------

def discover() -> tuple[list[str], str]:
    """Return content-relative paths and a hash of all relevant Git blobs."""
    print("Fetching repository tree...")
    body = fetch_url(TREE_URL, timeout=90)
    if not body:
        print("ERROR: failed to fetch tree", file=sys.stderr)
        sys.exit(1)
    data = json.loads(body)
    if data.get("truncated"):
        print("WARNING: tree response truncated; some files may be missing",
              file=sys.stderr)
    rels = []
    manifest: list[tuple[str, str]] = []
    data_paths = {
        f"{DATA_PREFIX}{name}"
        for name in ("misc.json", "configkeys.json", "languages.json",
                     "support.json")
    }
    for t in data.get("tree", []):
        p = t.get("path", "")
        if t.get("type") == "blob" and p in data_paths:
            manifest.append((p, t.get("sha", "")))
        if (t.get("type") != "blob" or not p.startswith(CONTENT_PREFIX)
                or not p.endswith(".md")):
            continue
        rel = p[len(CONTENT_PREFIX):]
        if rel.split("/", 1)[0] in SECTIONS:
            rels.append(rel)
            manifest.append((p, t.get("sha", "")))
    with open(__file__, "rb") as f:
        fetcher_hash = hashlib.sha256(f.read()).hexdigest()
    fingerprint = sha256(json.dumps({
        "blobs": sorted(manifest),
        "fetcher": fetcher_hash,
    }, separators=(",", ":")))
    return sorted(rels), fingerprint


def source_outputs_complete(source: dict) -> bool:
    outputs = source.get("outputs", [])
    return bool(outputs) and all(
        os.path.isfile(os.path.join(DOCS_DIR, rel)) for rel in outputs)


def hugo_urlize(title: str) -> str:
    """Approximate Hugo's :slug/:title permalink token: lowercase, spaces to
    hyphens, drop anything that is not alphanumeric or one of ._- (verified
    against the live blog URLs, e.g. '4.39: Release Notes' ->
    '4.39-release-notes', 'Authelia + Traefik Setup Guide' ->
    'authelia--traefik-setup-guide')."""
    s = title.lower().replace(" ", "-")
    s = "".join(ch for ch in s if ch.isalnum() or ch in "._-")
    return s.strip("-")


def page_from(rel: str, raw: str) -> dict:
    meta, body = parse_frontmatter(raw)
    base = rel[:-3]
    section = rel.split("/", 1)[0]
    is_index = posixpath.basename(base) == "_index"
    bundle = posixpath.basename(base) == "index"
    if is_index or bundle:
        url = posixpath.dirname(base)
    else:
        url = base
    natural_url = url
    title = meta.get("title", "") or posixpath.basename(url)
    slug = meta.get("slug")
    if not is_index:
        if slug:
            url = posixpath.join(posixpath.dirname(url), hugo_urlize(str(slug)))
        elif section == "blog":
            # [permalinks] blog = "/blog/:slug/" with no slug set falls back
            # to the slugified title.
            url = posixpath.join("blog", hugo_urlize(title))
    try:
        weight = int(str(meta.get("weight", "")))
    except ValueError:
        weight = 10 ** 9
    return {
        "rel": rel,
        "url": url,
        "natural_url": natural_url,
        "section": section,
        "is_index": is_index,
        "bundle": bundle,
        # Relative links and page resources resolve against the bundle dir
        # for leaf bundles and against the parent dir for plain .md pages.
        "link_base": url if bundle else posixpath.dirname(url),
        "meta": meta,
        "body": body,
        "title": title,
        "description": meta.get("description", ""),
        "weight": weight,
        "date": str(meta.get("date", ""))[:10],
        "draft": str(meta.get("draft", "")).lower() == "true",
    }


def build_page_markdown(page: dict, conv: Converter) -> str:
    body = conv.convert(page["body"], page).strip("\n")
    source_url = f"{SITE}/{page['url']}/"
    repo_url = f"https://github.com/{REPO}/blob/{BRANCH}/{CONTENT_PREFIX}{page['rel']}"
    parts = [f"# {page['title']}", ""]
    if page["description"]:
        parts += [f"*{page['description']}*", ""]
    parts.append(f"*Source: [{source_url}]({source_url})*")
    parts.append(f"*Repo: [{repo_url}]({repo_url})*")
    if page["section"] == "blog" and page["date"]:
        parts.append(f"*Date: {page['date']}*")
    parts.append("")
    parts.append("")
    return "\n".join(parts) + body + "\n"


# ---------------------------------------------------------------------------
# Index generation
# ---------------------------------------------------------------------------

def sort_pages(pages: list[dict], section: str) -> list[dict]:
    if section == "blog":
        return sorted(pages, key=lambda p: (p["date"], p["title"]), reverse=True)
    return sorted(pages, key=lambda p: (p["weight"], p["title"].lower()))


def build_section_readme(section: str, leaves: list[dict],
                         indexes: dict[str, dict]) -> str:
    sec = indexes.get(section, {})
    title = sec.get("title") or section.title()
    lines = [f"# {title}", ""]
    if sec.get("description"):
        lines += [f"*{sec['description']}*", ""]
    lines.append(f"*Mirrored from [{SITE}/{section}/]({SITE}/{section}/). "
                 f"{len(leaves)} pages.*")
    lines.append("")

    by_dir: dict[str, list[dict]] = {}
    for p in leaves:
        by_dir.setdefault(posixpath.dirname(p["url"]), []).append(p)

    def list_pages(dir_url: str) -> None:
        for p in sort_pages(by_dir.get(dir_url, []), section):
            rel = posixpath.relpath(p["url"] + ".md", section)
            entry = f"- [{p['title']}](./{rel})"
            if section == "blog" and p["date"]:
                entry = f"- {p['date']} [{p['title']}](./{rel})"
            if p["description"]:
                entry += f": {p['description']}"
            lines.append(entry)
        lines.append("")

    def child_dirs(dir_url: str) -> list[str]:
        prefix = dir_url + "/"
        kids = {d[len(prefix):].split("/")[0]
                for d in by_dir if d.startswith(prefix)}
        kids |= {d[len(prefix):].split("/")[0]
                 for d in indexes if d.startswith(prefix) and d != dir_url}
        def key(name: str) -> tuple:
            idx = indexes.get(posixpath.join(dir_url, name), {})
            return (idx.get("weight", 10 ** 9), name)
        return sorted(kids, key=key)

    def walk(dir_url: str, depth: int) -> None:
        if dir_url in by_dir:
            list_pages(dir_url)
        for name in child_dirs(dir_url):
            child = posixpath.join(dir_url, name)
            idx = indexes.get(child, {})
            heading = "#" * min(depth + 2, 6)
            lines.append(f"{heading} {idx.get('title') or name.title()}")
            lines.append("")
            if idx.get("description"):
                lines.append(f"*{idx['description']}*")
                lines.append("")
            walk(child, depth + 1)

    walk(section, 0)
    while lines and not lines[-1]:
        lines.pop()
    return "\n".join(lines) + "\n"


def build_top_readme(by_section: dict[str, list[dict]],
                     indexes: dict[str, dict]) -> str:
    lines = ["# Authelia Documentation", ""]
    lines.append(f"*Mirrored from [{SITE}/]({SITE}/). Source: "
                 f"[github.com/{REPO}](https://github.com/{REPO}) "
                 f"`docs/content/` (branch `{BRANCH}`).*")
    lines.append("")
    lines.append("## Sections")
    lines.append("")
    for section in SECTIONS:
        leaves = by_section.get(section, [])
        idx = indexes.get(section, {})
        title = idx.get("title") or section.title()
        entry = f"- [{title}](./{section}/README.md) ({len(leaves)} pages)"
        if idx.get("description"):
            entry += f": {idx['description']}"
        lines.append(entry)
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(args: argparse.Namespace) -> None:
    # The previous cache is always loaded for removal detection; --force
    # only disables the unchanged-skip.
    old_cache = load_cache()
    cache = {} if args.force else old_cache

    rels, source_fingerprint = discover()
    print(f"  found {len(rels)} markdown files in scope")

    previous_source = cache.get(SOURCE_CACHE_KEY, {})
    if (not args.force
            and previous_source.get("fingerprint") == source_fingerprint
            and source_outputs_complete(previous_source)):
        total_files = len(previous_source["outputs"])
        print("  Source tree unchanged and all outputs present; "
              "skipping content downloads and conversion")
        print("\nSync complete:")
        print("  Added:       0")
        print("  Updated:     0")
        print(f"  Unchanged:   {total_files}")
        print("  Removed:     0")
        print("  Unavailable: 0")
        print(f"  Total pages: {previous_source.get('page_count', 0)}")
        return

    print("Fetching data files...")
    data: dict = {}
    missing_data: list[str] = []
    for name in ("misc.json", "configkeys.json", "languages.json", "support.json"):
        body = fetch_url(f"{RAW_BASE}/{DATA_PREFIX}{name}")
        if body is None:
            print(f"WARNING: missing data file {name}", file=sys.stderr)
            missing_data.append(name)
            continue
        data[name.split(".")[0]] = json.loads(body)

    print(f"Fetching content (concurrency={MAX_WORKERS})...")

    def fetch_one(rel: str) -> tuple[str, str | None]:
        return rel, fetch_url(f"{RAW_BASE}/{CONTENT_PREFIX}{rel}")

    fetched: dict[str, str] = {}
    missing: list[str] = []
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, rel) for rel in rels]
        for fut in as_completed(futures):
            rel, content = fut.result()
            if content is None:
                missing.append(rel)
            else:
                fetched[rel] = content

    print(f"  fetched: {len(fetched)}")
    if missing:
        print(f"  unavailable: {len(missing)}")
        for rel in sorted(missing):
            print(f"    SKIP {rel}")
    if missing or missing_data:
        print("ERROR: source tree entries could not be fetched; leaving the "
              "existing mirror untouched", file=sys.stderr)
        sys.exit(1)

    pages: list[dict] = []
    drafts = 0
    for rel in sorted(fetched):
        page = page_from(rel, fetched[rel])
        if page["draft"]:
            drafts += 1
            continue
        pages.append(page)

    leaves = [p for p in pages if not p["is_index"]]
    indexes = {p["url"]: {"title": p["title"],
                          "description": p["description"],
                          "weight": p["weight"]}
               for p in pages if p["is_index"]}
    pages_by_url = {p["url"]: p for p in leaves}
    aliases: dict[str, str] = {}
    for p in leaves:
        for alias in p["meta"].get("aliases", []) or []:
            aliases[str(alias).strip("/")] = p["url"]
        # Source-path key for pages whose URL diverges from the content
        # layout (blog permalinks), so source-relative links still resolve.
        if p["natural_url"] != p["url"]:
            aliases.setdefault(p["natural_url"], p["url"])

    conv = Converter(data, pages_by_url, aliases)

    if not args.dry_run:
        os.makedirs(DOCS_DIR, exist_ok=True)

    added = updated = unchanged = 0
    new_cache: dict = {}
    output_paths: set[str] = set()

    def emit(cache_key: str, file_path: str, content: str) -> None:
        nonlocal added, updated, unchanged
        output_paths.add(os.path.relpath(file_path, DOCS_DIR))
        content_hash = sha256(content)
        prev = cache.get(cache_key, {})
        if prev.get("sha256") == content_hash and os.path.exists(file_path):
            unchanged += 1
            new_cache[cache_key] = prev
            return
        is_new = cache_key not in cache or not os.path.exists(file_path)
        write_file(file_path, content, dry_run=args.dry_run,
                   verbose=args.verbose, label="ADD" if is_new else "UPDATE")
        new_cache[cache_key] = {
            "sha256": content_hash,
            "last_updated": datetime.now(timezone.utc).isoformat(),
        }
        if is_new:
            added += 1
        else:
            updated += 1

    for page in leaves:
        emit(page["url"], os.path.join(DOCS_DIR, *page["url"].split("/")) + ".md",
             build_page_markdown(page, conv))

    by_section: dict[str, list[dict]] = {}
    for p in leaves:
        by_section.setdefault(p["section"], []).append(p)

    for section in SECTIONS:
        if section not in by_section:
            continue
        emit(f"__readme__/{section}",
             os.path.join(DOCS_DIR, section, "README.md"),
             build_section_readme(section, by_section[section], indexes))

    emit("__readme__/_top", os.path.join(DOCS_DIR, "README.md"),
         build_top_readme(by_section, indexes))

    new_cache[SOURCE_CACHE_KEY] = {
        "fingerprint": source_fingerprint,
        "outputs": sorted(output_paths),
        "page_count": len(leaves),
        "last_updated": datetime.now(timezone.utc).isoformat(),
    }

    # Removals
    removed = 0
    for old_key in sorted(old_cache):
        if old_key in new_cache:
            continue
        if old_key == SOURCE_CACHE_KEY:
            continue
        if old_key == "__readme__/_top":
            old_path = os.path.join(DOCS_DIR, "README.md")
        elif old_key.startswith("__readme__/"):
            old_path = os.path.join(
                DOCS_DIR, old_key[len("__readme__/"):], "README.md")
        else:
            old_path = os.path.join(DOCS_DIR, *old_key.split("/")) + ".md"
        if not os.path.exists(old_path):
            continue
        if args.dry_run:
            print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        else:
            os.remove(old_path)
            if args.verbose:
                print(f"  REMOVE {os.path.relpath(old_path, DOCS_DIR)}")
        removed += 1

    if not args.dry_run and os.path.isdir(DOCS_DIR):
        for root, _dirs, _files in os.walk(DOCS_DIR, topdown=False):
            if root != DOCS_DIR and not os.listdir(root):
                os.rmdir(root)

    if not args.dry_run:
        save_cache(new_cache)

    print("\nSync complete:")
    print(f"  Added:       {added}")
    print(f"  Updated:     {updated}")
    print(f"  Unchanged:   {unchanged}")
    print(f"  Removed:     {removed}")
    print(f"  Unavailable: {len(missing)}")
    print(f"  Drafts:      {drafts}")
    print(f"  Total pages: {len(leaves)}")
    for section in SECTIONS:
        if section in by_section:
            print(f"    {section}: {len(by_section[section])}")
    if conv.unconverted:
        print(f"  Unconverted shortcodes: {len(conv.unconverted)}")
        if args.verbose:
            for url, snippet in conv.unconverted:
                print(f"    {url}: {snippet}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Authelia docs from GitHub and mirror to local markdown"
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
