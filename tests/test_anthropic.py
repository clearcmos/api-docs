import sys
from types import SimpleNamespace

from tests.support import load_fetcher

fetcher = load_fetcher("anthropic")


def test_discover_pages_deduplicates_and_adds_markdown_suffix(monkeypatch):
    index = (
        "- https://code.claude.com/docs/en/overview\n"
        "- https://code.claude.com/docs/en/agent-sdk/overview.md\n"
        "- https://code.claude.com/docs/en/overview"
    )
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: index)

    assert fetcher.discover_pages() == [
        "https://code.claude.com/docs/en/overview.md",
        "https://code.claude.com/docs/en/agent-sdk/overview.md",
    ]


def test_path_mapping_handles_nested_slugs():
    assert fetcher.url_to_category_and_slug("https://code.claude.com/docs/en/whats-new/2026/w13.md") == (
        "whats-new",
        "2026/w13",
    )
    assert fetcher.url_to_category_and_slug("https://code.claude.com/docs/en/overview.md") == (
        "general",
        "overview",
    )


def test_clean_markdown_preserves_content_and_removes_vendor_chrome():
    raw = (
        "> Documentation index\n> More chrome\n# Configure\n\n"
        '<Tabs>\n<Tab title="CLI">\nRun it\n</Tab>\n</Tabs>'
    )
    cleaned = fetcher.clean_markdown(raw)

    assert cleaned == "# Configure\n\nRun it\n\n"
    assert fetcher.extract_title(cleaned) == "Configure"


def test_category_rendering_and_cache_round_trip(tmp_path, monkeypatch):
    readme = fetcher.build_category_readme(
        "agent-sdk", [{"slug": "overview", "title": "SDK Overview", "filename": "overview.md"}]
    )
    assert readme.startswith("# Agent SDK")
    assert "[SDK Overview](./overview.md)" in readme

    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    cache = {"agent-sdk:overview.md": {"sha256": "abc", "title": "SDK Overview"}}
    fetcher.save_cache(cache)
    assert fetcher.load_cache() == cache


def test_sync_updates_preserves_failed_page_and_removes_stale(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    cache_file = tmp_path / "cache.json"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(cache_file))
    urls = [f"{fetcher.BASE_URL}/overview.md", f"{fetcher.BASE_URL}/agent-sdk/missing.md"]
    monkeypatch.setattr(fetcher, "discover_pages", lambda: urls)
    monkeypatch.setattr(
        fetcher,
        "fetch_page_content",
        lambda url, _verbose=False: "# Overview\nBody" if url == urls[0] else None,
    )
    (docs / "general").mkdir(parents=True)
    (docs / "general" / "overview.md").write_text("old")
    (docs / "agent-sdk").mkdir()
    (docs / "agent-sdk" / "missing.md").write_text("preserve")
    (docs / "stale").mkdir()
    (docs / "stale" / "old.md").write_text("remove")
    fetcher.save_cache(
        {
            "general:overview.md": {"sha256": "old", "title": "Overview"},
            "agent-sdk:missing.md": {"sha256": "keep", "title": "Missing"},
            "stale:old.md": {"sha256": "stale", "title": "Old"},
        }
    )

    fetcher.sync(SimpleNamespace(force=False, dry_run=False, verbose=True))

    assert "Body" in (docs / "general" / "overview.md").read_text()
    assert (docs / "agent-sdk" / "missing.md").read_text() == "preserve"
    assert not (docs / "stale" / "old.md").exists()
    assert "agent-sdk:missing.md" in fetcher.load_cache()


def test_fetch_page_content_rejects_empty_and_main_parses_cli(monkeypatch):
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: "> chrome only")
    assert fetcher.fetch_page_content("https://example.test", verbose=True) is None
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run"])
    fetcher.main()
    assert called[0].dry_run is True
