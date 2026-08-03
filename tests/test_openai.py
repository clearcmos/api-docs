from __future__ import annotations

import gzip
import sys
from argparse import Namespace
from pathlib import Path
from urllib.error import HTTPError

import pytest

from tests.support import load_fetcher

openai = load_fetcher("openai")


def test_index_paths_and_markdown_builders() -> None:
    text = """
## Documentation sets
- [Agents](https://developers.openai.com/api/docs/agents.md): General
## API
- [Agents](https://developers.openai.com/api/docs/agents.md): Build agents
- [Overview](https://developers.openai.com/codex/overview.md)
"""
    entries = openai.parse_llms_index(text)
    assert len(entries) == 3
    assert openai.url_rel_path(entries[0]["url"]) == "api/docs/agents.md"
    assert openai.top_segment(entries[-1]["url"]) == "codex"
    assert openai.file_path_for_url(entries[0]["url"]).endswith("api/docs/agents.md")
    assert openai.build_page_markdown("Body", "Agents", entries[0]["url"]).startswith("# Agents")
    assert openai.build_page_markdown("# Existing\n", "Ignored", entries[0]["url"]).count("# Existing") == 1
    by_top = {"api": entries[:1], "codex": entries[-1:]}
    assert "OpenAI API" in openai.build_top_readme(entries[:2], by_top)
    assert "General" in openai.build_product_readme("api", entries[:1])
    assert openai.display_product("new-product") == "New Product"


def test_sync_fetches_validates_preserves_and_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(openai, "DOCS_DIR", str(docs))
    monkeypatch.setattr(openai, "CACHE_FILE", str(tmp_path / "cache.json"))
    state = {"mode": "fetch", "include_codex": True}

    def index() -> str:
        codex = "- [Codex](https://developers.openai.com/codex/overview.md): Codex overview\n"
        return (
            "## Documentation sets\n"
            "- [Agents old](https://developers.openai.com/api/docs/agents.md): Old\n"
            "- [Full](https://developers.openai.com/api/llms-full.txt)\n"
            "## API\n"
            "- [Agents](https://developers.openai.com/api/docs/agents.md): Build agents\n"
            "## Codex\n" + (codex if state["include_codex"] else "")
        )

    def fetch(url: str, timeout: int = 60, etag: str | None = None):
        del timeout
        if url == openai.LLMS_INDEX_URL:
            return index(), None, False
        if state["mode"] == "validate":
            return None, etag or '"same"', True
        if state["mode"] == "missing" and "agents" in url:
            return None, None, False
        return f"# {url.rsplit('/', 1)[-1]}\n\nBody\n", '"etag"', False

    monkeypatch.setattr(openai, "fetch_url", fetch)
    args = Namespace(force=False, dry_run=False, verbose=True)
    openai.sync(args)
    agents = docs / "api" / "docs" / "agents.md"
    codex = docs / "codex" / "overview.md"
    assert agents.exists() and codex.exists()

    state["mode"] = "validate"
    openai.sync(args)
    state["mode"] = "missing"
    openai.sync(args)
    assert agents.exists()

    state["mode"] = "fetch"
    state["include_codex"] = False
    openai.sync(args)
    assert not codex.exists()


def test_sync_failure_dry_run_and_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai, "fetch_url", lambda *_args, **_kwargs: (None, None, False))
    with pytest.raises(SystemExit):
        openai.sync(Namespace(force=True, dry_run=True, verbose=False))

    called: list[Namespace] = []
    monkeypatch.setattr(openai, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    openai.main()
    assert called[0].dry_run and called[0].force and called[0].verbose


def test_transport_and_cache_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Encoding": "gzip", "ETag": '"e"'}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return gzip.compress(b"body")

    monkeypatch.setattr(openai, "urlopen", lambda *_args, **_kwargs: Response())
    assert openai.fetch_url("https://example.test", etag='"old"') == ("body", '"e"', False)

    not_modified = HTTPError("url", 304, "same", {"ETag": '"new"'}, None)
    monkeypatch.setattr(openai, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(not_modified))
    assert openai.fetch_url("https://example.test", etag='"old"') == (None, '"new"', True)
    missing = HTTPError("url", 404, "missing", {}, None)
    monkeypatch.setattr(openai, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(missing))
    assert openai.fetch_url("https://example.test") == (None, None, False)

    monkeypatch.setattr(openai, "CACHE_FILE", str(tmp_path / "cache.json"))
    assert openai.load_cache() == {}
    openai.save_cache({"x": {"sha256": "y"}})
    assert openai.load_cache()["x"]["sha256"] == "y"
