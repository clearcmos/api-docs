import sys
from types import SimpleNamespace

from tests.support import load_fetcher

fetcher = load_fetcher("ollama")


def test_llms_index_parses_sections_and_openapi_entries():
    entries = fetcher.parse_llms_index(
        "## API\n- [Chat](https://docs.ollama.com/api/chat.md): Generate a response\n"
        "## Specs\n- [OpenAPI](https://docs.ollama.com/openapi.yaml)"
    )
    assert entries == [
        {
            "section": "API",
            "title": "Chat",
            "url": "https://docs.ollama.com/api/chat.md",
            "summary": "Generate a response",
        },
        {
            "section": "Specs",
            "title": "OpenAPI",
            "url": "https://docs.ollama.com/openapi.yaml",
            "summary": "",
        },
    ]


def test_markdown_url_path_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))
    assert fetcher.path_for_md("https://docs.ollama.com/index.md") == (
        "",
        "index",
        str(tmp_path / "index.md"),
    )
    assert fetcher.path_for_md("https://docs.ollama.com/api/chat.md") == (
        "api",
        "chat",
        str(tmp_path / "api" / "chat.md"),
    )


def test_page_cleaning_removes_mintlify_banner_and_preserves_existing_h1():
    raw = (
        "> ## Documentation Index\n"
        "> Fetch the complete documentation index at: https://docs.ollama.com/llms.txt\n"
        "> Use this file to discover all available pages before exploring further.\n\n"
        "# Chat\nBody"
    )
    rendered = fetcher.build_page_markdown(raw, "Ignored", "https://docs.ollama.com/api/chat")
    assert "Documentation Index" not in rendered
    assert rendered.count("# Chat") == 1
    assert rendered.startswith("*Source: [https://docs.ollama.com/api/chat]")


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    cache = {"api/chat": {"sha256": "abc"}}
    fetcher.save_cache(cache)
    assert fetcher.load_cache() == cache


def test_sync_preserves_failed_page_removes_stale_and_hits_cache(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    index = (
        "## Guides\n- [Quickstart](https://docs.ollama.com/quickstart.md): Begin\n"
        "## API\n- [Missing](https://docs.ollama.com/api/missing.md)\n"
        "## OpenAPI Specs\n- [Spec](https://docs.ollama.com/openapi.yaml)"
    )
    bodies = {
        fetcher.LLMS_INDEX_URL: index,
        "https://docs.ollama.com/quickstart.md": "# Quickstart\nBody",
    }
    monkeypatch.setattr(fetcher, "fetch_url", bodies.get)
    (docs / "api").mkdir(parents=True)
    (docs / "api" / "missing.md").write_text("preserve")
    (docs / "stale").mkdir()
    (docs / "stale" / "old.md").write_text("remove")
    fetcher.save_cache(
        {
            "api/missing.md": {"sha256": "keep"},
            "stale/old.md": {"sha256": "stale"},
        }
    )
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)
    fetcher.sync(args)
    assert (docs / "api" / "missing.md").read_text() == "preserve"
    assert not (docs / "stale" / "old.md").exists()
    fetcher.sync(args)
    assert "api/missing.md" in fetcher.load_cache()


def test_main_forwards_cli_flags(monkeypatch):
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run"])
    fetcher.main()
    assert called[0].dry_run is True
