import json
import sys
from types import SimpleNamespace

import pytest

from tests.support import load_fetcher

fetcher = load_fetcher("authelia")


def test_frontmatter_and_page_model_handle_blog_permalink():
    raw = "---\ntitle: '4.39: Release Notes'\ndate: 2026-01-02\nweight: 7\naliases: [/old/]\n---\nBody"
    meta, body = fetcher.parse_frontmatter(raw)
    page = fetcher.page_from("blog/4.39.md", raw)

    assert meta["aliases"] == ["/old/"]
    assert body == "Body"
    assert page["url"] == "blog/4.39-release-notes"
    assert page["weight"] == 7
    assert page["date"] == "2026-01-02"


def test_converter_expands_shortcodes_and_rewrites_internal_links():
    pages = {"overview/start": {}}
    converter = fetcher.Converter({"misc": {"latest": "4.39.0"}}, pages, {})
    page = {"url": "configuration/example", "link_base": "configuration"}
    body = (
        '{{< callout context="tip" title="Heads up" >}}\nRead this\n{{< /callout >}}\n\n'
        '{{< confkey type="string" required="no" >}}\n\n[Start](/overview/start/)'
    )

    rendered = converter.convert(body, page)

    assert "> [!TIP]" in rendered
    assert "> **Heads up**" in rendered
    assert "Type: `string` | Required: no" in rendered
    assert "[Start](../overview/start.md)" in rendered
    assert converter.unconverted == []


def test_page_markdown_renders_source_metadata():
    page = fetcher.page_from("overview/start.md", "---\ntitle: Start\ndescription: Begin here\n---\nWelcome")
    rendered = fetcher.build_page_markdown(page, fetcher.Converter({}, {page["url"]: page}, {}))
    assert rendered.startswith("# Start\n\n*Begin here*")
    assert "*Source: [https://www.authelia.com/overview/start/]" in rendered
    assert rendered.endswith("Welcome\n")


def test_source_fast_path_requires_every_output(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))
    (tmp_path / "overview").mkdir()
    (tmp_path / "overview" / "start.md").write_text("ok")

    assert fetcher.source_outputs_complete({"outputs": ["overview/start.md"]})
    assert not fetcher.source_outputs_complete({"outputs": ["overview/start.md", "missing.md"]})
    assert not fetcher.source_outputs_complete({"outputs": []})


def test_render_helpers_cover_metadata_figures_and_oidc_includes():
    assert "Syntax: [duration]" in fetcher.render_confkey(
        {"type": "string,integer", "syntax": "duration", "default": "5m", "secret": "yes"}
    )
    assert "Structure: [server]" in fetcher.render_confkey(
        {"type": "structure", "structure": "server", "required": "situational"}
    )
    assert fetcher.render_support({"support": "full", "link": "https://example.test"}) == (
        "[Full](https://example.test)"
    )
    assert fetcher.render_roadmap({"stage": "complete", "version": "4.39"}) == ("**Status:** complete (4.39)")
    page = {"link_base": "integration/example"}
    assert fetcher.render_figure("inline-svg", {"alt": "Diagram"}, page) == "*Diagram*"
    assert fetcher.render_figure("figure", {"src": "/img/a.png", "caption": "A"}, page) == (
        "![A](https://www.authelia.com/img/a.png)\n\n*A*"
    )
    assert "Known Bugs" in "\n".join(
        fetcher.render_oidc_common(
            {"bugs": "claims-hydration,client-credentials-encoding,claim-binding", "faq": "/faq/"}
        )
    )
    escape = "\n".join(
        fetcher.render_oidc_escape_hatch(
            {"client_id": "demo", "policy_name": "policy", "claims": "email,groups"}
        )
    )
    assert "client_id: 'demo'" in escape
    assert "claims_policy: 'policy'" in escape


def test_converter_data_blocks_inline_shortcodes_and_link_edge_cases():
    data = {
        "misc": {
            "latest": "4.39.0",
            "csp": {"nonce": "nonce", "default": "default-src 'self'"},
            "support": {"proxy": ["1", "2"]},
            "hashing_algorithms": {
                "pbkdf2": {"variants": {"sha256": {"fips": True, "default_iterations": 1000}}}
            },
        },
        "configkeys": [
            {"path": "server.port", "env": "AUTHELIA_SERVER_PORT", "secret": False},
            {"path": "jwt_secret", "env": "AUTHELIA_JWT_SECRET", "secret": True},
        ],
        "languages": {
            "languages": [
                {"display": "English", "locale": "en", "namespaces": ["portal"], "fallbacks": ["en"]}
            ]
        },
        "support": {
            "totp": [
                {
                    "name": "App",
                    "url": "https://app.test",
                    "algorithms": {"SHA1": True, "SHA256": False, "SHA512": True},
                    "digits": {"six": True, "eight": False},
                }
            ]
        },
    }
    pages = {"overview/start": {}, "configuration/example": {}}
    converter = fetcher.Converter(data, pages, {"old": "overview/start"})
    page = {"url": "configuration/example", "link_base": "configuration"}
    blocks = [
        "{{< config-alert-example >}}",
        "{{< sitevar-preferences >}}",
        "{{< csp >}}",
        '{{< table-config-keys secrets="false" >}}',
        '{{< table-config-keys secrets="true" >}}',
        "{{< table-i18n-locales >}}",
        "{{< table-i18n-overrides >}}",
        "{{< table-totp-support >}}",
        "{{< hashing-pbkdf2-iterations >}}",
        "{{< hashing-pbkdf2-variants >}}",
        '{{< supported-product product="proxy" format="v$version" >}}',
    ]
    for block in blocks:
        assert converter.expand_block(block, page) is not None
    assert converter.expand_block("plain", page) is None

    inline = converter.sub_inline(
        '{{< github-link path="README.md" name="Readme" >}} '
        '{{< support support="partial" >}} '
        '{{< confkey type="string" required="no" >}} '
        '{{< roadmap-status stage="in-progress" >}}',
        page,
    )
    assert "github.com/authelia/authelia/blob/v4.39.0/README.md" in inline
    assert "Partial" in inline and "in progress" in inline

    assert converter.map_target("https://example.test", page) is None
    assert converter.map_target("#anchor", page) is None
    assert converter.map_target("/old", page) == "../overview/start.md"
    assert converter.map_target("/assets/logo.png", page) == f"{fetcher.SITE}/assets/logo.png"
    assert converter.map_target("../unknown/page", page) == f"{fetcher.SITE}/unknown/page/"
    rewritten = converter.rewrite_links(
        "[ref]: /overview/start\n`[literal](/overview/start)` [`key`](/overview/start)\n"
        "```\n[unchanged](/unknown)\n```",
        page,
    )
    assert "[ref]: ../overview/start.md" in rewritten
    assert "`[literal](/overview/start)`" in rewritten
    assert "[`key`](../overview/start.md)" in rewritten
    assert "[unchanged](/unknown)" in rewritten


def test_converter_handles_tabs_details_fences_prints_and_unconverted_shortcodes():
    converter = fetcher.Converter({"misc": {"latest": "4.39"}}, {}, {})
    page = {"url": "overview/example", "link_base": "overview"}
    body = (
        '{{< envTabs >}}\n{{< envTab "Docker" >}}\n{{< details "More" >}}\n'
        '```yaml { title="Config" }\nversion: {{< latest >}}\n```\n'
        '{{< print "{{< literal >}}" >}}\n{{< /details >}}\n{{< /envTab >}}\n{{< /envTabs >}}\n'
        '{{< callout context="warning" >}}Body{{< /callout >}}\n{{< unknown >}}'
    )
    rendered = converter.convert(body, page)
    assert "**Docker**" in rendered and "**More**" in rendered
    assert "**Config**" in rendered and "version: 4.39" in rendered
    assert "`{{< literal >}}`" in rendered
    assert "> [!WARNING]" in rendered
    assert converter.unconverted == [("overview/example", "{{< unknown >}}")]


def test_tree_discovery_filters_sections_and_hashes_data(monkeypatch):
    tree = {
        "truncated": True,
        "tree": [
            {"type": "blob", "path": "docs/content/overview/start.md", "sha": "a"},
            {"type": "blob", "path": "docs/content/policies/skip.md", "sha": "b"},
            {"type": "blob", "path": "docs/data/misc.json", "sha": "c"},
            {"type": "blob", "path": "docs/data/unused.json", "sha": "d"},
        ],
    }
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url, timeout=60: json.dumps(tree))
    rels, fingerprint = fetcher.discover()
    assert rels == ["overview/start.md"]
    assert len(fingerprint) == 64


def test_sync_writes_indexes_removes_stale_and_uses_fast_path(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    rels = ["overview/_index.md", "overview/start.md", "blog/release.md", "roadmap/draft.md"]
    monkeypatch.setattr(fetcher, "discover", lambda: (rels, "fingerprint"))
    data = {
        "misc.json": {"latest": "4.39"},
        "configkeys.json": [],
        "languages.json": {},
        "support.json": {},
    }
    pages = {
        "overview/_index.md": "---\ntitle: Overview\nweight: 1\n---\nIndex",
        "overview/start.md": "---\ntitle: Start\naliases: [/old/]\n---\nWelcome",
        "blog/release.md": "---\ntitle: Release\ndate: 2026-01-02\n---\nNews",
        "roadmap/draft.md": "---\ntitle: Draft\ndraft: true\n---\nHidden",
    }

    def fetch(url):
        name = url.rsplit("/", 1)[-1]
        if f"/{fetcher.DATA_PREFIX}" in url:
            return json.dumps(data[name])
        rel = url.split(fetcher.CONTENT_PREFIX, 1)[1]
        return pages[rel]

    monkeypatch.setattr(fetcher, "fetch_url", fetch)
    (docs / "stale").mkdir(parents=True)
    (docs / "stale" / "old.md").write_text("remove")
    fetcher.save_cache({"stale/old": {"sha256": "stale"}})
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)

    assert (docs / "overview" / "start.md").exists()
    assert (docs / "blog" / "release.md").exists()
    assert not (docs / "roadmap" / "draft.md").exists()
    assert not (docs / "stale" / "old.md").exists()
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: pytest.fail("fast path fetched content"))
    fetcher.sync(args)


def test_sync_aborts_without_mutating_mirror_when_data_or_page_is_missing(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    sentinel = docs / "sentinel.md"
    sentinel.write_text("keep")
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(fetcher, "discover", lambda: (["overview/start.md"], "changed"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: None)
    with pytest.raises(SystemExit):
        fetcher.sync(SimpleNamespace(force=True, dry_run=False, verbose=True))
    assert sentinel.read_text() == "keep"


def test_main_forwards_cli_flags(monkeypatch):
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--verbose"])
    fetcher.main()
    assert called[0].dry_run is True and called[0].verbose is True
