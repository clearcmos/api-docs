import argparse
import gzip
import json
from urllib.error import URLError

import pytest

from tests.support import load_fetcher

kandji = load_fetcher("kandji")


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


def test_transport_cache_and_html_boundaries(tmp_path, monkeypatch):
    compressed = gzip.compress(b"collection")
    monkeypatch.setattr(kandji, "urlopen", lambda *_args, **_kwargs: Response(compressed, encoding="gzip"))
    assert kandji.fetch_url("https://example.test") == "collection"
    monkeypatch.setattr(
        kandji,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("down")),
    )
    assert kandji.fetch_url("https://example.test") is None

    cache_file = tmp_path / ".cache.json"
    monkeypatch.setattr(kandji, "CACHE_FILE", str(cache_file))
    assert kandji.load_cache() == {}
    kandji.save_cache({"key": {"sha256": "abc"}})
    assert kandji.load_cache() == {"key": {"sha256": "abc"}}

    table = "<table><tr><th>Name</th><th>Value</th></tr><tr><td>A</td><td>B</td></tr></table>"
    html = (
        "<h1>Title</h1><h2>Two</h2><h3>Three</h3><h4>Four</h4>"
        "<p><strong>Bold</strong> <em>italic</em> <a href='https://example.test'>link</a></p>"
        "<ul><li>One</li></ul><ol><li>Two</li></ol><pre>code</pre>" + table
    )
    markdown = kandji.clean_html(html)
    assert "| Name | Value |" in markdown
    assert "[link](https://example.test)" in markdown
    assert kandji.clean_html("") == ""
    assert kandji.convert_html_table("<table><td>no header</td></table>") == ""
    assert kandji.extract_url("https://example.test") == "https://example.test"
    assert kandji.extract_url({"raw": 3}) == ""
    assert kandji.extract_url(None) == ""


def test_postman_endpoint_conversion_and_mapping():
    item = {
        "name": "List Devices",
        "request": {
            "method": "GET",
            "url": {
                "raw": "https://api.kandji.io/api/v1/devices?limit=10",
                "query": [{"key": "limit", "description": "Page size", "value": "10"}],
            },
            "description": "<p>Returns <strong>devices</strong>.</p>",
            "header": [{"key": "Authorization", "value": "Bearer token"}],
        },
    }

    markdown = kandji.build_endpoint_markdown(item)

    assert "Returns **devices**." in markdown
    assert "**Method:** `GET`" in markdown
    assert "**limit**: `10`" in markdown
    assert kandji.build_method_filename(item) == "get-list-devices.md"
    assert kandji.extract_url(item["request"]["url"]).endswith("limit=10")


def test_endpoint_body_auth_and_response_variants():
    base = {
        "name": "Create Device",
        "request": {
            "method": "POST",
            "url": "https://example.test/devices",
            "auth": {"type": "bearer"},
        },
        "response": [
            {"name": "Created", "status": "Created", "code": 201, "body": '{"id": 1}'},
            {"name": "Text", "body": "not-json"},
        ],
    }
    raw = {**base, "request": {**base["request"], "body": {"mode": "raw", "raw": '{"name":"A"}'}}}
    form = {
        **base,
        "request": {
            **base["request"],
            "body": {
                "mode": "formdata",
                "formdata": [{"key": "file", "type": "file", "description": {"content": "Upload"}}],
            },
        },
    }
    encoded = {
        **base,
        "request": {
            **base["request"],
            "body": {
                "mode": "urlencoded",
                "urlencoded": [{"key": "name", "value": "A", "description": {"content": "Device name"}}],
            },
        },
    }

    assert "Type: `bearer`" in kandji.build_endpoint_markdown(raw)
    assert '"id": 1' in kandji.build_endpoint_markdown(raw)
    assert "**file** (file): Upload" in kandji.build_endpoint_markdown(form)
    assert "**name**: `A` -- Device name" in kandji.build_endpoint_markdown(encoded)


def test_cache_hit_requires_existing_output(tmp_path):
    output = tmp_path / "endpoint.md"
    content = "body"
    digest = kandji.sha256(content)
    cache = {"key": {"sha256": digest, "last_updated": "then"}}
    counters = {"added": 0, "updated": 0, "unchanged": 0}
    new_cache = {}
    args = argparse.Namespace(dry_run=False, verbose=False)
    output.write_text(content)

    kandji._write_file(str(output), content, "key", digest, cache, new_cache, args, counters, "endpoint.md")
    assert counters == {"added": 0, "updated": 0, "unchanged": 1}
    assert new_cache["key"] == cache["key"]

    output.unlink()
    counters = {"added": 0, "updated": 0, "unchanged": 0}
    kandji._write_file(str(output), content, "key", digest, cache, {}, args, counters, "endpoint.md")
    assert output.read_text() == content
    assert counters["added"] == 1


def test_sync_end_to_end_cache_hits_and_authoritative_removal(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    stale_folder = docs / "old"
    stale_folder.mkdir()
    (stale_folder / "stale.md").write_text("stale")
    stale_root = docs / "stale-root.md"
    stale_root.write_text("stale")
    cache_file = tmp_path / ".cache.json"
    cache_file.write_text(
        json.dumps(
            {
                "folder:old:stale.md": {"sha256": "old"},
                "root:stale-root.md": {"sha256": "old"},
            }
        )
    )
    collection_file = tmp_path / "collection.json"
    endpoint = {
        "name": "List Devices",
        "request": {"method": "GET", "url": "https://example.test/devices"},
    }
    collection = {
        "info": {"name": "Kandji API", "description": "<p>API docs.</p>"},
        "item": [
            {
                "name": "Devices",
                "description": "Device operations",
                "item": [
                    endpoint,
                    {"name": "Nested", "item": [{**endpoint, "name": "Nested Device"}]},
                ],
            },
            {**endpoint, "name": "Health"},
        ],
    }
    raw = json.dumps(collection)
    monkeypatch.setattr(kandji, "DOCS_DIR", str(docs))
    monkeypatch.setattr(kandji, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(kandji, "COLLECTION_FILE", str(collection_file))
    monkeypatch.setattr(kandji, "fetch_url", lambda _url: raw)
    args = argparse.Namespace(force=False, dry_run=False, verbose=True)

    kandji.sync(args)
    first_cache = json.loads(cache_file.read_text())
    assert not (stale_folder / "stale.md").exists()
    assert not stale_root.exists()
    assert (docs / "devices" / "get-list-devices.md").exists()
    assert (docs / "devices" / "nested" / "get-nested-device.md").exists()
    assert (docs / "get-health.md").exists()
    assert collection_file.exists()

    kandji.sync(args)
    assert json.loads(cache_file.read_text()) == first_cache

    monkeypatch.setattr(kandji, "fetch_url", lambda _url: None)
    with pytest.raises(SystemExit):
        kandji.sync(args)


def test_dry_run_cleanup_and_main_boundaries(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    empty = docs / "empty"
    empty.mkdir(parents=True)
    kandji._clean_empty_dirs(str(docs))
    assert not empty.exists()

    collection = json.dumps({"info": {}, "item": []})
    monkeypatch.setattr(kandji, "DOCS_DIR", str(docs))
    monkeypatch.setattr(kandji, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(kandji, "fetch_url", lambda _url: collection)
    kandji.sync(argparse.Namespace(force=True, dry_run=True, verbose=False))

    called = []
    monkeypatch.setattr(kandji, "sync", called.append)
    monkeypatch.setattr(kandji.sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    kandji.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
