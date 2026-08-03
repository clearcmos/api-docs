from __future__ import annotations

import gzip
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.support import load_fetcher

sabnzbd = load_fetcher("sabnzbd")


def test_wiki_parser_groups_functions_and_preserves_structured_content() -> None:
    html = """
    <div class="wiki-content">
      <h1 id="queue">Queue functions</h1><p>Queue operations.</p>
      <h2 id="pause">Pause <span class="label">True/False</span></h2>
      <p>Pause the queue using <code>mode=pause</code>.</p>
      <ul><li>Requires an API key</li></ul>
      <pre><code class="language-json">{"status": true}</code></pre>
      <table><tr><th>Name</th><th>Description</th></tr><tr><td>apikey</td><td>API key</td></tr></table>
    </div>
    """
    parser = sabnzbd.WikiParser()
    parser.feed(html)
    groups = sabnzbd.group_sections(parser.sections)

    assert len(groups) == 1
    group = groups[0]
    assert group.title == "Queue functions"
    assert group.functions[0]["anchor"] == "pause"
    assert group.functions[0]["label"] == "True/False"
    assert "`mode=pause`" in group.functions[0]["body"]
    assert "```json" in group.functions[0]["body"]
    assert "| Name | Description |" in group.functions[0]["body"]


def test_grouping_deduplicates_filenames_and_keeps_subheadings() -> None:
    sections = [
        sabnzbd.WikiSection(1, "History", "history", None),
        sabnzbd.WikiSection(2, "Item", "item", None),
        sabnzbd.WikiSection(2, "Item again", "item", None),
        sabnzbd.WikiSection(3, "Details", None, None),
    ]
    sections[-1].emit("More detail")

    group = sabnzbd.group_sections(sections)[0]

    assert [item["filename"] for item in group.functions] == ["item.md", "item-2.md"]
    assert "### Details" in group.functions[-1]["body"]


def test_markdown_builders_include_source_modes_and_counts() -> None:
    group = sabnzbd.Group("Queue", "queue")
    group.functions.append(
        {"title": "Pause", "anchor": "pause", "label": "True/False", "filename": "pause.md", "body": "Body"}
    )

    assert "**API mode:** `pause`" in sabnzbd.build_function_markdown(group, group.functions[0])
    assert "[Pause](./pause.md)" in sabnzbd.build_group_readme(group)
    assert "**Functions documented:** 1" in sabnzbd.build_top_readme([group])


def test_sync_writes_reuses_and_removes_outputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(sabnzbd, "DOCS_DIR", str(docs))
    monkeypatch.setattr(sabnzbd, "CACHE_FILE", str(tmp_path / "cache.json"))
    html = """
    <div class="wiki-content">
      <h1 id="queue">Queue</h1>
      <h2 id="pause">Pause</h2><p>Pause it.</p>
      <h2 id="resume">Resume</h2><p>Resume it.</p>
    </div>
    """
    monkeypatch.setattr(sabnzbd, "fetch_url", lambda *_args, **_kwargs: html)
    args = Namespace(force=False, dry_run=False, verbose=True)

    sabnzbd.sync(args)
    assert (docs / "queue" / "pause.md").exists()
    assert (docs / "queue" / "resume.md").exists()
    sabnzbd.sync(args)

    reduced = html.replace('<h2 id="resume">Resume</h2><p>Resume it.</p>', "")
    monkeypatch.setattr(sabnzbd, "fetch_url", lambda *_args, **_kwargs: reduced)
    sabnzbd.sync(args)
    assert not (docs / "queue" / "resume.md").exists()


@pytest.mark.parametrize("html", [None, "<div class='wiki-content'><h1>Empty</h1></div>"])
def test_sync_rejects_missing_or_empty_source(html: str | None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sabnzbd, "fetch_url", lambda *_args, **_kwargs: html)
    with pytest.raises(SystemExit):
        sabnzbd.sync(Namespace(force=True, dry_run=True, verbose=False))


def test_fetch_cache_and_cli_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return gzip.compress(b"docs")

    monkeypatch.setattr(sabnzbd, "urlopen", lambda *_args, **_kwargs: Response())
    assert sabnzbd.fetch_url("https://example.test") == "docs"
    monkeypatch.setattr(sabnzbd, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("no")))
    assert sabnzbd.fetch_url("https://example.test") is None

    monkeypatch.setattr(sabnzbd, "CACHE_FILE", str(tmp_path / "cache.json"))
    assert sabnzbd.load_cache() == {}
    sabnzbd.save_cache({"file": {"sha256": "x"}})
    assert sabnzbd.load_cache()["file"]["sha256"] == "x"

    called: list[Namespace] = []
    monkeypatch.setattr(sabnzbd, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    sabnzbd.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
