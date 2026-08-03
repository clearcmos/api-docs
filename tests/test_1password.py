import sys
from types import SimpleNamespace

from tests.support import load_fetcher

fetcher = load_fetcher("1password")


def test_discovery_merges_sitemap_and_remaps_stale_overview(monkeypatch):
    responses = {
        fetcher.SITEMAP_URL: (
            "<urlset><url><loc>https://www.1password.dev/sdks/</loc></url>"
            "<url><loc>https://www.1password.dev/sdks/python</loc></url></urlset>"
        ),
        fetcher.LLMS_INDEX_URL: (
            "## SDKs\n- [SDK overview](https://www.1password.dev/sdks/overview.md)\n"
            "- [Python](https://www.1password.dev/sdks/python.md)"
        ),
    }
    monkeypatch.setattr(fetcher, "fetch_url", responses.get)

    urls, titles = fetcher.discover_urls()

    assert urls == ["https://www.1password.dev/sdks", "https://www.1password.dev/sdks/python"]
    assert titles["https://www.1password.dev/sdks"] == "SDK overview"


def test_url_normalization_and_path_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))

    assert fetcher.normalize_docs_url("https://example.com/docs") is None
    assert fetcher.normalize_docs_url("https://www.1password.dev/sdks/python.md/") == (
        "https://www.1password.dev/sdks/python"
    )
    category, subpath, path = fetcher.path_for("https://www.1password.dev/sdks/python")
    assert (category, subpath) == ("sdks", "sdks/python")
    assert path == str(tmp_path / "sdks" / "python.md")


def test_page_and_category_markdown_are_stable():
    rendered = fetcher.build_page_markdown("Body", "Install", "https://www.1password.dev/install")
    assert rendered == (
        "# Install\n\n*Source: [https://www.1password.dev/install]"
        "(https://www.1password.dev/install)*\nBody\n"
    )
    readme = fetcher.build_category_readme(
        "sdks",
        [
            {"subpath": "sdks/python", "title": "Python"},
            {"subpath": "sdks", "title": "Overview"},
        ],
    )
    assert "[Overview](./index.md)" in readme
    assert "[Python](./python.md)" in readme


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    cache = {"sdks/python": {"sha256": "abc"}}
    fetcher.save_cache(cache)
    assert fetcher.load_cache() == cache


def test_sync_updates_preserves_failures_removes_stale_and_hits_cache(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(cache_file))
    urls = [f"{fetcher.SITE}/sdks/python", f"{fetcher.SITE}/sdks/missing"]
    monkeypatch.setattr(fetcher, "discover_urls", lambda: (urls, {urls[0]: "Python", urls[1]: "Missing"}))
    monkeypatch.setattr(
        fetcher, "fetch_url", lambda url: "# Python\nBody" if url == urls[0] + ".md" else None
    )
    (docs / "sdks").mkdir(parents=True)
    (docs / "sdks" / "python.md").write_text("old")
    (docs / "sdks" / "missing.md").write_text("preserve")
    (docs / "stale").mkdir()
    (docs / "stale" / "page.md").write_text("remove")
    fetcher.save_cache(
        {
            "sdks/python": {"sha256": "old", "title": "Python"},
            "sdks/missing": {"sha256": "keep", "title": "Missing"},
            "stale/page": {"sha256": "stale"},
        }
    )
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)

    assert "Body" in (docs / "sdks" / "python.md").read_text()
    assert (docs / "sdks" / "missing.md").read_text() == "preserve"
    assert not (docs / "stale" / "page.md").exists()
    first_cache = fetcher.load_cache()
    fetcher.sync(args)
    assert fetcher.load_cache() == first_cache


def test_main_forwards_cli_flags(monkeypatch):
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert vars(called[0]) == {"dry_run": True, "force": True, "verbose": True}
