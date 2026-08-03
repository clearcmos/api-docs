import sys
from types import SimpleNamespace

from tests.support import load_fetcher

fetcher = load_fetcher("opencode")


def test_sidebar_and_sitemap_discovery_parsers():
    html = (
        '<ul class="top-level">'
        '<a href="/docs/"><span>Intro</span></a>'
        '<div class="group-label"><span>Usage</span></div>'
        '<a href="/docs/cli"><span>CLI</span></a>'
        '<a href="/docs/cli"><span>Duplicate</span></a></ul>'
    )
    assert fetcher.parse_sidebar(html) == [
        {"group": None, "slug": "", "title": "Intro"},
        {"group": "Usage", "slug": "cli", "title": "CLI"},
    ]

    sitemap = (
        "<urlset><loc>https://opencode.ai/docs/</loc><loc>https://opencode.ai/docs/cli</loc>"
        "<loc>https://opencode.ai/docs/de/</loc><loc>https://opencode.ai/docs/de/cli/</loc></urlset>"
    )
    assert fetcher.parse_sitemap_english(sitemap) == {"", "cli"}


def test_path_helpers_keep_nested_slugs(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))
    assert fetcher.md_url("") == "https://opencode.ai/docs/index.md"
    assert fetcher.file_path("guides/start") == str(tmp_path / "guides" / "start.md")
    assert fetcher.path_from_cache_key("guides/start") == str(tmp_path / "guides" / "start.md")
    assert fetcher.derive_title("mcp-servers") == "MCP servers"


def test_mdx_and_aside_conversion_preserve_code():
    raw = (
        'import Widget from "./Widget"\n<Tabs>\n<TabItem label="Node">\n'
        ":::tip[Use this]\nRun <code>opencode</code>\n```sh\necho :::\n```\n:::\n</TabItem>\n</Tabs>"
    )
    rendered = fetcher.build_page_markdown(raw, "CLI", "https://opencode.ai/docs/cli")
    assert "import Widget" not in rendered
    assert "**Node**" in rendered
    assert "> [!TIP]" in rendered
    assert "> **Use this**" in rendered
    assert "> ```sh\n> echo :::\n> ```" in rendered
    assert "`opencode`" in rendered


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    cache = {"cli": {"sha256": "abc"}}
    fetcher.save_cache(cache)
    assert fetcher.load_cache() == cache


def test_sync_preserves_failed_page_removes_stale_and_hits_cache(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    entries = [
        {"group": None, "slug": "", "title": "Intro"},
        {"group": "Usage", "slug": "cli", "title": "CLI"},
        {"group": "Usage", "slug": "missing", "title": "Missing"},
    ]
    monkeypatch.setattr(fetcher, "discover", lambda: entries)
    bodies = {
        fetcher.md_url(""): "Welcome",
        fetcher.md_url("cli"): ":::tip\nUse it\n:::",
    }
    monkeypatch.setattr(fetcher, "fetch_url", bodies.get)
    docs.mkdir()
    (docs / "missing.md").write_text("preserve")
    (docs / "stale").mkdir()
    (docs / "stale" / "old.md").write_text("remove")
    fetcher.save_cache({"missing": {"sha256": "keep"}, "stale/old": {"sha256": "stale"}})
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)
    fetcher.sync(args)
    assert (docs / "missing.md").read_text() == "preserve"
    assert not (docs / "stale" / "old.md").exists()
    fetcher.sync(args)
    assert "missing" in fetcher.load_cache()


def test_discover_falls_back_to_sitemap_and_main_cli(monkeypatch):
    responses = {
        fetcher.DOCS_INDEX_URL: None,
        fetcher.SITEMAP_URL: "<loc>https://opencode.ai/docs/</loc><loc>https://opencode.ai/docs/new</loc>",
    }
    monkeypatch.setattr(fetcher, "fetch_url", responses.get)
    assert [entry["slug"] for entry in fetcher.discover()] == ["", "new"]
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--verbose"])
    fetcher.main()
    assert called[0].verbose is True
