import argparse
import gzip
import json
from urllib.error import HTTPError, URLError

import pytest

from tests.support import load_fetcher

obs_websocket = load_fetcher("obs-websocket")


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


def test_transport_cache_and_write_boundaries(tmp_path, monkeypatch):
    compressed = gzip.compress(b"protocol")
    monkeypatch.setattr(
        obs_websocket,
        "urlopen",
        lambda *_args, **_kwargs: Response(compressed, encoding="gzip"),
    )
    assert obs_websocket.fetch_url("https://example.test") == "protocol"
    http_error = HTTPError("https://example.test", 500, "error", {}, None)
    monkeypatch.setattr(
        obs_websocket,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(http_error),
    )
    assert obs_websocket.fetch_url("https://example.test") is None
    monkeypatch.setattr(
        obs_websocket,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("down")),
    )
    assert obs_websocket.fetch_url("https://example.test") is None

    cache_file = tmp_path / ".cache.json"
    monkeypatch.setattr(obs_websocket, "CACHE_FILE", str(cache_file))
    assert obs_websocket.load_cache() == {}
    obs_websocket.save_cache({"README.md": {"sha256": "abc"}})
    assert obs_websocket.load_cache() == {"README.md": {"sha256": "abc"}}

    docs = tmp_path / "docs"
    monkeypatch.setattr(obs_websocket, "DOCS_DIR", str(docs))
    output = docs / "nested" / "page.md"
    obs_websocket.write_file(str(output), "body", dry_run=False, verbose=True, label="ADD")
    assert output.read_text() == "body"
    dry_output = docs / "dry.md"
    obs_websocket.write_file(str(dry_output), "body", dry_run=True, verbose=False, label="ADD")
    assert not dry_output.exists()


def test_protocol_conversion_and_output_mapping():
    spec = {
        "enums": [
            {
                "enumType": "OpCode",
                "enumIdentifiers": [{"enumIdentifier": "Hello", "enumValue": 0, "description": "Greeting"}],
            }
        ],
        "requests": [
            {
                "requestType": "GetVersion",
                "category": "General",
                "description": "Gets the version.",
                "complexity": 1,
                "rpcVersion": 1,
                "initialVersion": "5.0.0",
                "deprecated": True,
                "requestFields": [
                    {
                        "valueName": "detail",
                        "valueType": "Boolean",
                        "valueOptional": True,
                        "valueDescription": "Include details | metadata",
                        "valueRestrictions": "true or false",
                        "valueOptionalBehavior": "Defaults to false",
                    }
                ],
                "responseFields": [
                    {"valueName": "obsVersion", "valueType": "String", "valueDescription": "Version"}
                ],
            }
        ],
        "events": [
            {
                "eventType": "CurrentProgramSceneChanged",
                "category": "Scenes",
                "description": "The scene changed.",
                "eventSubscription": "Scenes",
                "dataFields": [
                    {"valueName": "sceneName", "valueType": "String", "valueDescription": "Scene"}
                ],
            }
        ],
    }
    protocol = "preamble\n## General Intro\nConnect with WebSocket.\n## Enumerations\nignored"

    files = obs_websocket.build_files(spec, protocol)

    assert "requests/general/GetVersion.md" in files
    assert "events/scenes/CurrentProgramSceneChanged.md" in files
    assert "Connect with WebSocket." in files["index.md"]
    assert "`obsVersion`" in files["requests/general/GetVersion.md"]
    assert "restrictions: true or false" in files["requests/general/GetVersion.md"]
    assert "[!WARNING]" in files["requests/general/GetVersion.md"]
    assert "`sceneName`" in files["events/scenes/CurrentProgramSceneChanged.md"]
    assert obs_websocket.slugify("!!!") == "misc"
    assert obs_websocket.esc(None) == ""
    assert "unstructured" in obs_websocket.extract_intro("unstructured")


def test_source_failure_stops_before_touching_existing_output(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    existing = docs / "README.md"
    existing.write_text("last known good")
    monkeypatch.setattr(obs_websocket, "DOCS_DIR", str(docs))
    monkeypatch.setattr(obs_websocket, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(obs_websocket, "fetch_url", lambda _url: None)

    with pytest.raises(SystemExit):
        obs_websocket.sync(argparse.Namespace(force=False, dry_run=False, verbose=False))

    assert existing.read_text() == "last known good"


def test_sync_writes_cache_hits_and_removes_authoritative_stale_files(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    stale = docs / "stale.md"
    stale.write_text("stale")
    cache_file = tmp_path / ".cache.json"
    cache_file.write_text(json.dumps({"stale.md": {"sha256": "old"}}))
    spec = {
        "enums": [],
        "requests": [
            {
                "requestType": "GetVersion",
                "category": "General",
                "requestFields": [],
                "responseFields": [],
            }
        ],
        "events": [],
    }
    protocol = "## General Intro\nConnect.\n## Enumerations"
    monkeypatch.setattr(obs_websocket, "DOCS_DIR", str(docs))
    monkeypatch.setattr(obs_websocket, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(
        obs_websocket,
        "fetch_url",
        lambda url: json.dumps(spec) if url == obs_websocket.PROTOCOL_JSON_URL else protocol,
    )

    args = argparse.Namespace(force=False, dry_run=False, verbose=True)
    obs_websocket.sync(args)
    first_cache = json.loads(cache_file.read_text())
    assert not stale.exists()
    assert "requests/general/GetVersion.md" in first_cache
    assert (docs / "requests" / "general" / "GetVersion.md").exists()

    obs_websocket.sync(args)
    assert json.loads(cache_file.read_text()) == first_cache

    monkeypatch.setattr(obs_websocket, "build_files", lambda _spec, _md: {})
    obs_websocket.sync(argparse.Namespace(force=False, dry_run=True, verbose=False))
    assert (docs / "README.md").exists()


def test_main_parses_standard_flags(monkeypatch):
    called = []
    monkeypatch.setattr(obs_websocket, "sync", called.append)
    monkeypatch.setattr(obs_websocket.sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])

    obs_websocket.main()

    assert called[0].dry_run and called[0].force and called[0].verbose
