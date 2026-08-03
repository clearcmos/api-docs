import sys
from types import SimpleNamespace

from tests.support import load_fetcher

fetcher = load_fetcher("discord")


def test_discovery_merges_and_deduplicates_authoritative_sources(monkeypatch):
    responses = {
        fetcher.SITEMAP_URL: (
            "<urlset><loc>https://docs.discord.com/developers/intro</loc>"
            "<loc>https://docs.discord.com/developers/resources/channel</loc></urlset>"
        ),
        fetcher.LLMS_INDEX_URL: (
            "## Resources\n- [Channel](https://docs.discord.com/developers/resources/channel.md)"
        ),
    }
    monkeypatch.setattr(fetcher, "fetch_url", responses.get)

    urls, titles = fetcher.discover_urls()

    assert urls == [
        "https://docs.discord.com/developers/intro",
        "https://docs.discord.com/developers/resources/channel",
    ]
    assert titles[urls[1]] == "Channel"


def test_url_normalization_and_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))
    assert fetcher.normalize_docs_url("https://docs.discord.com/users/x") is None
    assert fetcher.path_for("https://docs.discord.com/developers/intro") == (
        "_root",
        "intro",
        str(tmp_path / "_root" / "intro.md"),
    )
    assert fetcher.path_for("https://docs.discord.com/developers/resources/channel") == (
        "resources",
        "resources/channel",
        str(tmp_path / "resources" / "channel.md"),
    )


def test_page_and_index_markdown():
    rendered = fetcher.build_page_markdown("# Existing\nBody", None, "https://example.test/page")
    assert rendered.startswith("*Source: [https://example.test/page](https://example.test/page)*\n# Existing")
    readme = fetcher.build_category_readme("_root", [{"subpath": "intro", "title": "Introduction"}])
    assert readme.startswith("# Top-Level Pages")
    assert "[Introduction](./intro.md)" in readme


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    cache = {"intro": {"sha256": "abc"}}
    fetcher.save_cache(cache)
    assert fetcher.load_cache() == cache


def test_sync_updates_preserves_failures_removes_stale_and_hits_cache(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    urls = [f"{fetcher.SITE}/developers/intro", f"{fetcher.SITE}/developers/resources/missing"]
    monkeypatch.setattr(fetcher, "discover_urls", lambda: (urls, {urls[0]: "Intro", urls[1]: "Missing"}))
    monkeypatch.setattr(fetcher, "fetch_url", lambda url: "# Intro\nBody" if url == urls[0] + ".md" else None)
    (docs / "_root").mkdir(parents=True)
    (docs / "_root" / "intro.md").write_text("old")
    (docs / "resources").mkdir()
    (docs / "resources" / "missing.md").write_text("preserve")
    (docs / "stale").mkdir()
    (docs / "stale" / "page.md").write_text("remove")
    fetcher.save_cache(
        {
            "_root/intro": {"sha256": "old", "title": "Intro"},
            "resources/missing": {"sha256": "keep", "title": "Missing"},
            "stale/page": {"sha256": "stale"},
        }
    )
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)
    fetcher.sync(args)
    assert (docs / "resources" / "missing.md").read_text() == "preserve"
    assert not (docs / "stale" / "page.md").exists()
    fetcher.sync(args)
    assert "resources/missing" in fetcher.load_cache()


def test_main_forwards_cli_flags(monkeypatch):
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--verbose"])
    fetcher.main()
    assert called[0].verbose is True
