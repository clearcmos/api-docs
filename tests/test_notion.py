import sys
from types import SimpleNamespace

from tests.support import load_fetcher

fetcher = load_fetcher("notion")


def test_index_parser_extracts_optional_descriptions():
    entries = fetcher.parse_index(
        "- [Intro](https://developers.notion.com/reference/intro.md): Start here\n"
        "- [Changelog](https://developers.notion.com/page/changelog.md)"
    )
    assert entries == [
        {
            "title": "Intro",
            "url": "https://developers.notion.com/reference/intro.md",
            "description": "Start here",
        },
        {
            "title": "Changelog",
            "url": "https://developers.notion.com/page/changelog.md",
            "description": "",
        },
    ]


def test_url_classification_handles_nested_and_deep_paths():
    assert fetcher.classify("https://developers.notion.com/reference/intro.md") == (
        "reference",
        "",
        "intro",
    )
    assert fetcher.classify("https://developers.notion.com/guides/mcp/overview.md") == (
        "guides",
        "mcp",
        "overview",
    )
    assert fetcher.classify("https://developers.notion.com/guides/a/b/c.md") == (
        "guides",
        "",
        "a-b-c",
    )


def test_page_rendering_strips_documentation_index_banner():
    entry = {
        "title": "Intro",
        "url": "https://developers.notion.com/reference/intro.md",
        "description": "Start here",
    }
    raw = "> ## Documentation Index\n> Fetch the complete documentation index\n\n# Upstream\nBody"
    rendered = fetcher.build_page_markdown(raw, entry)
    assert rendered.startswith("# Intro\n\n*Source:")
    assert "Documentation Index" not in rendered
    assert rendered.endswith("# Upstream\nBody\n")


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    cache = {"reference/intro": {"sha256": "abc"}}
    fetcher.save_cache(cache)
    assert fetcher.load_cache() == cache


def test_sync_preserves_failed_pages_and_removes_authoritatively_stale_files(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    index = (
        "- [Intro](https://developers.notion.com/reference/intro.md): Start\n"
        "- [MCP](https://developers.notion.com/guides/mcp/start.md)\n"
        "- [Missing](https://developers.notion.com/page/missing.md)"
    )
    bodies = {
        fetcher.LLMS_INDEX_URL: index,
        "https://developers.notion.com/reference/intro.md": "# Intro\nBody",
        "https://developers.notion.com/guides/mcp/start.md": "# MCP\nGuide",
    }
    monkeypatch.setattr(fetcher, "fetch_url", bodies.get)
    (docs / "page").mkdir(parents=True)
    (docs / "page" / "missing.md").write_text("preserve")
    (docs / "stale").mkdir()
    (docs / "stale" / "old.md").write_text("remove")
    fetcher.save_cache(
        {
            "page::missing": {"sha256": "keep"},
            "stale::old": {"sha256": "stale"},
        }
    )

    fetcher.sync(SimpleNamespace(force=False, dry_run=False, verbose=True))

    assert (docs / "reference" / "intro.md").exists()
    assert (docs / "guides" / "mcp" / "README.md").exists()
    assert (docs / "page" / "missing.md").read_text() == "preserve"
    assert not (docs / "stale" / "old.md").exists()
    assert "page::missing" in fetcher.load_cache()


def test_sync_handles_worker_exception_and_main_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    index = "- [Boom](https://developers.notion.com/reference/boom.md)"

    def failing_fetch(url):
        if url == fetcher.LLMS_INDEX_URL:
            return index
        raise RuntimeError("offline failure")

    monkeypatch.setattr(fetcher, "fetch_url", failing_fetch)
    fetcher.sync(SimpleNamespace(force=True, dry_run=True, verbose=False))
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--force"])
    fetcher.main()
    assert called[0].force is True
