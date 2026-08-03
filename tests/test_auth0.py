import sys
from types import SimpleNamespace

from tests.support import load_fetcher

fetcher = load_fetcher("auth0")


def test_index_and_full_text_parsing():
    index = (
        "- [Get users](https://auth0.com/docs/api/management/v2/users/get-users.md)\n"
        "- [Guide](https://auth0.com/docs/get-started.md)"
    )
    assert fetcher.discover_api_urls(index) == ["https://auth0.com/docs/api/management/v2/users/get-users.md"]

    full = (
        "# Get users\nSource: https://auth0.com/docs/api/management/v2/users/get-users.md\nBody\n"
        "# Create user\nSource: https://auth0.com/docs/api/management/v2/users/create-user.md\nMore"
    )
    pages = fetcher.parse_full_text(full)
    assert set(pages) == {
        "https://auth0.com/docs/api/management/v2/users/get-users.md",
        "https://auth0.com/docs/api/management/v2/users/create-user.md",
    }


def test_url_classification_and_filename_sanitizing():
    assert fetcher.classify_url("https://auth0.com/docs/api/management/v2/users/get-users.md") == (
        "management-v2",
        "users",
        "get-users",
    )
    assert fetcher.classify_url("https://auth0.com/docs/guides/start.md") is None
    assert fetcher.sanitize_filename("Create / User!") == "create-user"


def test_page_rendering_removes_pipeline_components():
    content = (
        "# Get users\nSource: https://auth0.com/docs/api/management/v2/users/get-users.md\n"
        "<Scopes />\nActual body"
    )
    rendered = fetcher.build_page_markdown(content, "unused")
    assert "<Scopes" not in rendered
    assert "*Source: [https://auth0.com/docs/api/management/v2/users/get-users.md]" in rendered
    assert rendered.endswith("Actual body\n")


def test_cache_round_trip(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    cache = {"management-v2/users/get-users": {"sha256": "abc"}}
    fetcher.save_cache(cache)
    assert fetcher.load_cache() == cache


def test_sync_preserves_unmatched_page_and_removes_stale_output(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    present_url = "https://auth0.com/docs/api/management/v2/users/get-users.md"
    missing_url = "https://auth0.com/docs/api/authentication/login/missing.md"
    index = f"- [Users]({present_url})\n- [Missing]({missing_url})"
    full = "# Get users\nSource: https://auth0.com/docs/api/management/v2/users/get-users\nBody"
    responses = {fetcher.LLMS_INDEX_URL: index, fetcher.LLMS_FULL_URL: full}
    monkeypatch.setattr(fetcher, "fetch_url", lambda url, timeout=120: responses.get(url))
    (docs / "authentication" / "login").mkdir(parents=True)
    (docs / "authentication" / "login" / "missing.md").write_text("preserve")
    (docs / "stale" / "old").mkdir(parents=True)
    (docs / "stale" / "old" / "page.md").write_text("remove")
    fetcher.save_cache(
        {
            "authentication:login:missing": {"sha256": "keep", "title": "Missing"},
            "stale:old:page": {"sha256": "stale"},
        }
    )
    args = SimpleNamespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)

    assert (docs / "authentication" / "login" / "missing.md").read_text() == "preserve"
    assert not (docs / "stale" / "old" / "page.md").exists()
    assert "authentication:login:missing" in fetcher.load_cache()
    fetcher.sync(args)


def test_sync_dry_run_and_main_cli(tmp_path, monkeypatch):
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / "cache.json"))
    url = "https://auth0.com/docs/api/myaccount/profile/index.md"
    responses = {
        fetcher.LLMS_INDEX_URL: f"- [Profile]({url})",
        fetcher.LLMS_FULL_URL: "# Profile\nSource: https://auth0.com/docs/api/myaccount/profile/index\nBody",
    }
    monkeypatch.setattr(fetcher, "fetch_url", lambda request_url, timeout=120: responses.get(request_url))
    fetcher.sync(SimpleNamespace(force=True, dry_run=True, verbose=False))
    assert not (tmp_path / "docs").exists()

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--force"])
    fetcher.main()
    assert called[0].force is True
