import sys
from types import SimpleNamespace

import pytest

from tests.support import load_fetcher

fetcher = load_fetcher("filebrowser")


def test_nav_parser_preserves_nested_order():
    nav = fetcher.parse_nav(
        "site_name: File Browser\nnav:\n  - Home: index.md\n  - CLI:\n    - Commands: cli/commands.md\n"
        "    - cli/options.md\ntheme:\n  name: material\n"
    )

    assert nav == [
        {"title": "Home", "page": "index.md"},
        {
            "title": "CLI",
            "children": [
                {"title": "Commands", "page": "cli/commands.md"},
                {"title": None, "page": "cli/options.md"},
            ],
        },
    ]
    assert fetcher.nav_pages(nav) == ["index.md", "cli/commands.md", "cli/options.md"]


def test_tree_discovery_includes_docs_and_mapped_root_pages(monkeypatch):
    tree = {
        "truncated": True,
        "tree": [
            {"type": "blob", "path": "www/docs/index.md", "sha": "a"},
            {"type": "blob", "path": "www/docs/assets/logo.png", "sha": "b"},
            {"type": "blob", "path": "CHANGELOG.md", "sha": "c"},
            {"type": "blob", "path": "www/mkdocs.yml", "sha": "d"},
        ],
    }
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url, timeout=60: __import__("json").dumps(tree))

    pages, fingerprint = fetcher.discover_pages()

    assert pages["index.md"] == "www/docs/index.md"
    assert pages["changelog.md"] == "CHANGELOG.md"
    assert set(fetcher.ROOT_PAGES).issubset(pages)
    assert len(fingerprint) == 64


def test_tab_conversion_and_link_rewriting_respect_code_fences():
    tabs = '=== "Linux"\n\n    ```sh\n    filebrowser -r .\n    ```\n\nAfter'
    assert fetcher.convert_tabs(tabs) == "**Linux**\n\n```sh\nfilebrowser -r .\n```\n\nAfter"

    text = "[Local](other.md) ![Logo](../static/logo.svg) [License](../../../LICENSE)\n```\n[Keep](x)\n```"
    rewritten = fetcher.rewrite_links(text, "guide", "www/docs/guide", {"guide/other.md"})
    assert "[Local](other.md)" in rewritten
    assert "![Logo](https://filebrowser.org/static/logo.svg)" in rewritten
    assert "https://github.com/filebrowser/filebrowser/blob/master/LICENSE" in rewritten
    assert "[Keep](x)" in rewritten


def test_page_rendering_drops_html_wrappers_and_keeps_navigation_title():
    fetcher.PAGE_SET = {"guide/other.md"}
    raw = '<div class="grid cards">\n# Original\n\n[Other](other.md)\n</div>'
    rendered, title = fetcher.build_page(raw, "guide/start.md", "www/docs/guide/start.md", "Start")
    assert title == "Start"
    assert rendered.startswith("# Start\n\n*Source:")
    assert "# Original" in rendered
    assert "<div" not in rendered
    assert "[Other](other.md)" in rendered


def test_source_fast_path_requires_every_output(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))
    (tmp_path / "index.md").write_text("ok")
    assert fetcher.source_outputs_complete({"outputs": ["index.md"]})
    assert not fetcher.source_outputs_complete({"outputs": ["index.md", "missing.md"]})


def test_sync_writes_nav_indexes_removes_stale_and_uses_fast_path(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    pages = {"index.md": "www/docs/index.md", "cli/commands.md": "www/docs/cli/commands.md"}
    monkeypatch.setattr(fetcher, "discover_pages", lambda: (pages, "fingerprint"))
    mkdocs = "nav:\n  - Home: index.md\n  - CLI:\n    - Commands: cli/commands.md\n"
    bodies = {
        fetcher.MKDOCS_URL: mkdocs,
        f"{fetcher.RAW_BASE}/www/docs/index.md": "# Home\nWelcome",
        f"{fetcher.RAW_BASE}/www/docs/cli/commands.md": "# Commands\nRun it",
    }
    monkeypatch.setattr(fetcher, "fetch_url", bodies.get)
    (docs / "stale").mkdir(parents=True)
    (docs / "stale" / "old.md").write_text("remove")
    fetcher.save_cache({"stale/old.md": {"sha256": "stale"}})
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)

    assert (docs / "index.md").exists()
    assert (docs / "README.md").exists()
    assert (docs / "cli" / "README.md").exists()
    assert not (docs / "stale" / "old.md").exists()
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: pytest.fail("fast path fetched content"))
    fetcher.sync(args)


def test_sync_aborts_before_writes_on_required_source_failures(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    sentinel = docs / "sentinel.md"
    sentinel.write_text("keep")
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(fetcher, "discover_pages", lambda: ({"index.md": "www/docs/index.md"}, "changed"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: None)
    with pytest.raises(SystemExit):
        fetcher.sync(SimpleNamespace(force=False, dry_run=False, verbose=True))
    assert sentinel.read_text() == "keep"

    monkeypatch.setattr(
        fetcher, "fetch_url", lambda url: "nav:\n  - Home: index.md" if url == fetcher.MKDOCS_URL else None
    )
    with pytest.raises(SystemExit):
        fetcher.sync(SimpleNamespace(force=True, dry_run=False, verbose=False))
    assert sentinel.read_text() == "keep"


def test_main_forwards_cli_flags(monkeypatch):
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--verbose"])
    fetcher.main()
    assert called[0].verbose is True
