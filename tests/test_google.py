import argparse
import gzip
import json
from urllib.error import URLError

import pytest

from tests.support import load_fetcher

google = load_fetcher("google")


class Response:
    def __init__(self, body: bytes, *, encoding: str = ""):
        self.body = body
        self.headers = {"Content-Encoding": encoding}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def test_transport_cache_and_discovery_boundaries(tmp_path, monkeypatch):
    compressed = gzip.compress(b'{"items": [{"id": "calendar:v3"}]}')
    monkeypatch.setattr(google, "urlopen", lambda *_args, **_kwargs: Response(compressed, encoding="gzip"))
    assert json.loads(google.fetch_url("https://example.test") or "") == {"items": [{"id": "calendar:v3"}]}
    assert google.fetch_directory() == [{"id": "calendar:v3"}]

    monkeypatch.setattr(google, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("down")))
    assert google.fetch_url("https://example.test") is None
    monkeypatch.setattr(google, "fetch_url", lambda _url: None)
    with pytest.raises(SystemExit):
        google.fetch_directory()

    cache_file = tmp_path / ".cache.json"
    monkeypatch.setattr(google, "CACHE_FILE", str(cache_file))
    assert google.load_cache() == {}
    google.save_cache({"api": {"sha256": "abc"}})
    assert google.load_cache() == {"api": {"sha256": "abc"}}
    assert google.sha256("body") == google.sha256("body")


def test_discovery_document_validation(monkeypatch):
    item = {"id": "calendar:v3", "discoveryRestUrl": "https://example.test/calendar"}
    monkeypatch.setattr(google, "fetch_url", lambda _url: None)
    assert google.fetch_discovery_doc(item, True)[1] is None
    monkeypatch.setattr(google, "fetch_url", lambda _url: "not json")
    assert google.fetch_discovery_doc(item, True)[1] is None
    monkeypatch.setattr(google, "fetch_url", lambda _url: '{"kind": "other"}')
    assert google.fetch_discovery_doc(item, True)[1] is None
    monkeypatch.setattr(
        google,
        "fetch_url",
        lambda _url: '{"kind": "discovery#restDescription", "revision": "20260803"}',
    )
    api_id, content, returned_item = google.fetch_discovery_doc(item, True)
    assert api_id == "calendar:v3"
    assert content is not None and '"revision": "20260803"' in content
    assert returned_item is item


def test_discovery_mapping_and_index():
    items = [
        {
            "id": "admin:directory_v1",
            "discoveryRestUrl": "https://admin.googleapis.com/$discovery/rest",
            "version": "directory_v1",
            "title": "Admin SDK",
        }
    ]

    selected = google.select_apis(items)
    index = json.loads(google.build_index(selected))

    assert google.api_id_to_filename("admin:directory_v1") == "admin-directory_v1.md"
    assert index == [
        {
            "subdomain": "admin",
            "url": "https://admin.googleapis.com/$discovery/rest",
            "version": "directory_v1",
            "title": "Admin SDK",
            "description": "",
        }
    ]


def test_sync_preserves_failed_current_doc_and_deprecates_removed_doc(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    deprecated = docs / "deprecated"
    docs.mkdir()
    (docs / "current-v1.md").write_text("last known good")
    (docs / "removed-v1.md").write_text("old")
    cache_file = tmp_path / ".cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "current:v1": {"sha256": "current"},
                "removed:v1": {"sha256": "removed"},
            }
        )
    )
    item = {
        "id": "current:v1",
        "discoveryRestUrl": "https://example.test/current",
        "version": "v1",
    }
    monkeypatch.setattr(google, "DOCS_DIR", str(docs))
    monkeypatch.setattr(google, "DEPRECATED_DIR", str(deprecated))
    monkeypatch.setattr(google, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(google, "INDEX_FILE", str(tmp_path / "index.json"))
    monkeypatch.setattr(google, "fetch_directory", lambda: [item])
    monkeypatch.setattr(google, "fetch_discovery_doc", lambda value, _verbose: (value["id"], None, value))

    google.sync(argparse.Namespace(force=False, verbose=False, dry_run=False))

    assert (docs / "current-v1.md").read_text() == "last known good"
    assert json.loads(cache_file.read_text()) == {"current:v1": {"sha256": "current"}}
    assert not (docs / "removed-v1.md").exists()
    assert (deprecated / "removed-v1.md").read_text() == "old"


def test_sync_add_update_cache_hit_dry_run_and_main(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    cache_file = tmp_path / ".cache.json"
    index_file = tmp_path / "index.json"
    items = [
        {"id": "new:v1", "discoveryRestUrl": "https://example.test/new", "version": "v1"},
        {"id": "updated:v1", "discoveryRestUrl": "https://example.test/updated", "version": "v1"},
        {"id": "same:v1", "discoveryRestUrl": "https://example.test/same", "version": "v1"},
    ]
    docs_by_id = {
        item["id"]: json.dumps(
            {"kind": "discovery#restDescription", "revision": item["id"]},
            indent=2,
            sort_keys=True,
        )
        for item in items
    }
    (docs / "updated-v1.md").write_text("old")
    (docs / "same-v1.md").write_text(docs_by_id["same:v1"] + "\n")
    old_cache = {
        "updated:v1": {"sha256": "old"},
        "same:v1": {"sha256": google.sha256(docs_by_id["same:v1"]), "marker": "keep"},
    }
    cache_file.write_text(json.dumps(old_cache))
    monkeypatch.setattr(google, "DOCS_DIR", str(docs))
    monkeypatch.setattr(google, "DEPRECATED_DIR", str(docs / "deprecated"))
    monkeypatch.setattr(google, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(google, "INDEX_FILE", str(index_file))
    monkeypatch.setattr(google, "fetch_directory", lambda: items)
    monkeypatch.setattr(
        google,
        "fetch_discovery_doc",
        lambda item, _verbose: (item["id"], docs_by_id[item["id"]], item),
    )

    args = argparse.Namespace(force=False, verbose=True, dry_run=False)
    google.sync(args)
    saved = json.loads(cache_file.read_text())
    assert set(saved) == {"new:v1", "updated:v1", "same:v1"}
    assert saved["same:v1"] == old_cache["same:v1"]
    assert (docs / "new-v1.md").exists()
    assert (docs / "updated-v1.md").read_text().startswith("{")
    assert index_file.exists()

    removed = docs / "removed-v1.md"
    removed.write_text("old")
    cache_file.write_text(json.dumps({"removed:v1": {"sha256": "old"}}))
    monkeypatch.setattr(google, "fetch_directory", lambda: [])
    google.sync(argparse.Namespace(force=False, verbose=False, dry_run=True))
    assert removed.exists()

    called = []
    monkeypatch.setattr(google, "sync", called.append)
    monkeypatch.setattr(google.sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    google.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
