import sys
from types import SimpleNamespace

import pytest

from tests.support import load_fetcher

fetcher = load_fetcher("qwen-code")


def test_frontmatter_and_path_mapping(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))
    meta, body = fetcher.parse_frontmatter(
        "---\ntitle: Quickstart\ndescription: Begin here\nnested:\n  value: ignored\n---\n# Body"
    )
    assert meta == {"title": "Quickstart", "description": "Begin here", "nested": ""}
    assert body == "# Body"
    assert fetcher.site_url_for("users/index.mdx") == f"{fetcher.SITE_BASE}/users/"
    assert fetcher.output_path_for("users/index.mdx") == str(tmp_path / "users" / "_index.md")
    assert fetcher.output_path_for("users/start.md") == str(tmp_path / "users" / "start.md")


def test_tree_discovery_filters_non_english_and_non_markdown_blobs(monkeypatch):
    tree = {
        "truncated": True,
        "tree": [
            {"type": "blob", "path": f"{fetcher.CONTENT_PREFIX}index.md", "sha": "a"},
            {"type": "blob", "path": f"{fetcher.CONTENT_PREFIX}guide.mdx", "sha": "b"},
            {"type": "blob", "path": f"{fetcher.CONTENT_PREFIX}image.png", "sha": "c"},
            {"type": "blob", "path": "website/content/zh/index.md", "sha": "d"},
        ],
    }
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url, timeout=60: __import__("json").dumps(tree))

    paths, fingerprint = fetcher.discover_paths()

    assert paths == [f"{fetcher.CONTENT_PREFIX}guide.mdx", f"{fetcher.CONTENT_PREFIX}index.md"]
    assert len(fingerprint) == 64


def test_page_rendering_prefers_frontmatter_title_and_metadata():
    raw = "---\ntitle: Quickstart\ndescription: Begin here\nauthor: Qwen\n---\nBody"
    rendered, title = fetcher.build_page_markdown(
        raw, "https://example.test/start", "https://repo.test/start"
    )
    assert title == "Quickstart"
    assert rendered.startswith("# Quickstart\n\n*Source:")
    assert "*Description: Begin here*" in rendered
    assert "*Author: Qwen*" in rendered
    assert rendered.endswith("Body\n")


def test_readmes_preserve_nested_sections():
    pages = [
        {"section": "", "link": "index.md", "title": "Home"},
        {"section": "users", "link": "users/start.md", "title": "Start"},
        {"section": "users", "link": "users/auth/login.md", "title": "Login"},
    ]
    top = fetcher.build_top_readme(pages)
    section = fetcher.build_section_readme("users", pages[1:])
    assert "[Home](./index.md)" in top
    assert "[Users](./users/) (2 pages)" in top
    assert "[Start](./start.md)" in section
    assert "### Auth" in section
    assert "[Login](./auth/login.md)" in section


def test_source_fast_path_requires_every_output(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path))
    (tmp_path / "index.md").write_text("ok")
    assert fetcher.source_outputs_complete({"outputs": ["index.md"]})
    assert not fetcher.source_outputs_complete({"outputs": ["index.md", "missing.md"]})


def test_sync_writes_indexes_removes_stale_and_uses_source_fast_path(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    repo_paths = [
        f"{fetcher.CONTENT_PREFIX}index.md",
        f"{fetcher.CONTENT_PREFIX}users/start.md",
        f"{fetcher.CONTENT_PREFIX}users/auth/login.mdx",
    ]
    monkeypatch.setattr(fetcher, "discover_paths", lambda: (repo_paths, "fingerprint"))
    bodies = {
        f"{fetcher.RAW_BASE}/{repo_paths[0]}": "---\ntitle: Home\n---\nWelcome",
        f"{fetcher.RAW_BASE}/{repo_paths[1]}": "# Start\nBody",
        f"{fetcher.RAW_BASE}/{repo_paths[2]}": "---\ntitle: Login\n---\nAuthenticate",
    }
    monkeypatch.setattr(fetcher, "fetch_url", bodies.get)
    (docs / "stale").mkdir(parents=True)
    (docs / "stale" / "old.md").write_text("remove")
    fetcher.save_cache({"stale/old.md": {"sha256": "stale"}})
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)

    assert (docs / "index.md").exists()
    assert (docs / "users" / "start.md").exists()
    assert (docs / "users" / "auth" / "login.md").exists()
    assert (docs / "users" / "README.md").exists()
    assert not (docs / "stale" / "old.md").exists()
    cache = fetcher.load_cache()
    assert cache[fetcher.SOURCE_CACHE_KEY]["fingerprint"] == "fingerprint"
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: pytest.fail("fast path fetched content"))
    fetcher.sync(args)


def test_sync_aborts_without_mutating_mirror_on_source_failure(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    sentinel = docs / "sentinel.md"
    sentinel.write_text("keep")
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    repo_paths = [f"{fetcher.CONTENT_PREFIX}index.md", f"{fetcher.CONTENT_PREFIX}missing.md"]
    monkeypatch.setattr(fetcher, "discover_paths", lambda: (repo_paths, "changed"))
    monkeypatch.setattr(
        fetcher,
        "fetch_url",
        lambda url: "# Home" if url.endswith("/website/content/en/index.md") else None,
    )

    with pytest.raises(SystemExit):
        fetcher.sync(SimpleNamespace(force=False, dry_run=False, verbose=True))
    assert sentinel.read_text() == "keep"


def test_main_forwards_cli_flags(monkeypatch):
    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--force", "--dry-run"])
    fetcher.main()
    assert called[0].force is True and called[0].dry_run is True
