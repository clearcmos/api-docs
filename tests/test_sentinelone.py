from __future__ import annotations

import gzip
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.support import load_fetcher

sentinelone = load_fetcher("sentinelone")


def test_cookie_header_prefers_direct_value_then_file(tmp_path: Path) -> None:
    assert sentinelone.build_cookie_header(Namespace(cookie=" token=abc ", cookie_file=None)) == "token=abc"
    cookie_file = tmp_path / "cookies.txt"
    cookie_file.write_text(" session=xyz \n")
    assert (
        sentinelone.build_cookie_header(Namespace(cookie=None, cookie_file=str(cookie_file))) == "session=xyz"
    )


def test_discovery_falls_back_and_accepts_custom_spec(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter([None, '{"apiList":[{"name":"Agents"}],"content":{}}'])
    monkeypatch.setattr(sentinelone, "fetch_url", lambda *_args, **_kwargs: next(responses))
    monkeypatch.setattr(sentinelone.time, "sleep", lambda _seconds: None)

    spec, url = sentinelone.discover_api_spec("https://console.example", "cookie")

    assert spec is not None
    assert spec["apiList"][0]["name"] == "Agents"
    assert url == "https://console.example/apidoc/formatted_swagger_2_0.json"


def test_custom_endpoint_renderer_covers_parameters_samples_and_responses() -> None:
    endpoint = {
        "name": "List agents",
        "description": "Returns agents.",
        "isDeprecated": True,
        "isDownload": True,
        "requestType": "GET",
        "url": "/agents",
        "requiredPermissions": ["Agents.View"],
        "optionalPermissions": "Agents.Export",
        "parameters": {
            "path": [{"name": "siteId", "type": "string", "required": True, "description": "Site"}],
            "query": [
                {
                    "name": "state",
                    "type": "array",
                    "items": {"type": "string"},
                    "enum": ["active", "inactive"],
                    "required": False,
                    "description": "Agent | state",
                }
            ],
            "body": [{"name": "filter", "type": "object", "required": False, "description": "Filter"}],
        },
        "bodySample": '{"limit":10}',
        "responses": [{"code": 200, "description": "OK", "schema": {"type": "object"}}],
        "responseSample": {"data": []},
    }

    markdown = sentinelone.s1_endpoint_to_markdown("Agents", "list", endpoint)

    assert "**Method:** `GET`" in markdown
    assert "array[string]" in markdown
    assert "Agent \\| state" in markdown
    assert '{"limit":10}' in markdown
    assert "### 200" in markdown


def test_openapi_renderer_and_indexes_are_stable() -> None:
    endpoint = {
        "method": "POST",
        "path": "/agents",
        "summary": "Create agent",
        "parameters": [{"name": "site", "in": "query", "required": True, "schema": {"type": "string"}}],
        "responses": {"201": {"description": "Created"}},
    }
    assert "| `site` | query | string | Yes |" in sentinelone.openapi_endpoint_to_markdown(endpoint)
    category = sentinelone.build_s1_category_readme(
        "Agents", [{"method": "GET", "url": "/agents", "name": "List", "filename": "list.md"}]
    )
    assert "[GET /agents](./list.md)" in category
    assert "**Total Endpoints:** 1" in sentinelone.build_s1_main_readme(
        "https://console.example", [("agents", "Agents", 1)], 1
    )


def test_custom_and_openapi_sync_cover_cache_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(sentinelone, "DOCS_DIR", str(docs))
    monkeypatch.setattr(sentinelone, "CACHE_FILE", str(tmp_path / "cache.json"))
    args = Namespace(force=False, dry_run=False, verbose=True)
    custom = {
        "apiList": [
            {
                "key": "agents",
                "name": "Agents",
                "operations": [{"key": "list", "name": "List agents"}],
            }
        ],
        "content": {
            "agents": {
                "list": {
                    "requestType": "GET",
                    "url": "/agents",
                    "parameters": {},
                    "responses": {"200": {"description": "OK"}},
                }
            }
        },
    }
    sentinelone.sync_s1(custom, "https://console.example", args)
    sentinelone.sync_s1(custom, "https://console.example", args)
    assert (docs / "agents" / "get-list.md").exists()

    openapi = {
        "info": {"title": "Console", "version": "1"},
        "paths": {
            "/sites": {
                "get": {
                    "summary": "List sites",
                    "tags": ["Sites"],
                    "responses": {"200": {"description": "OK"}},
                },
                "parameters": {},
            }
        },
    }
    sentinelone.sync_openapi(openapi, "https://console.example", args)
    assert (docs / "sites" / "get-sites.md").exists()
    assert not (docs / "agents" / "get-list.md").exists()
    sentinelone.sync_openapi(openapi, "https://console.example", args)


def test_sync_routes_specs_and_rejects_unknown_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sentinelone, "SPEC_FILE", str(tmp_path / "spec.json"))
    monkeypatch.setattr(sentinelone, "verify_auth", lambda *_args: None)
    custom = {"apiList": [], "content": {}}
    monkeypatch.setattr(sentinelone, "discover_api_spec", lambda *_args: (custom, "spec-url"))
    called: list[str] = []
    monkeypatch.setattr(sentinelone, "sync_s1", lambda *_args: called.append("custom"))
    args = Namespace(
        base_url="https://console.example/",
        cookie="session=x",
        cookie_file=None,
        dry_run=False,
        force=False,
        verbose=False,
    )
    sentinelone.sync(args)
    assert called == ["custom"]
    assert (tmp_path / "spec.json").exists()

    monkeypatch.setattr(sentinelone, "discover_api_spec", lambda *_args: ({"openapi": "3.0"}, "url"))
    monkeypatch.setattr(sentinelone, "sync_openapi", lambda *_args: called.append("openapi"))
    args.dry_run = True
    sentinelone.sync(args)
    assert called[-1] == "openapi"

    monkeypatch.setattr(sentinelone, "discover_api_spec", lambda *_args: ({"unknown": True}, "url"))
    with pytest.raises(SystemExit):
        sentinelone.sync(args)


def test_transport_auth_cache_and_cli_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return gzip.compress(b'{"data":{"build":"24.1"}}')

    monkeypatch.setattr(sentinelone, "urlopen", lambda *_args, **_kwargs: Response())
    assert '"build"' in (sentinelone.fetch_url("https://example.test", "session=x") or "")
    sentinelone.verify_auth("https://example.test/", "session=x")

    monkeypatch.setattr(
        sentinelone, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("offline"))
    )
    assert sentinelone.fetch_url("https://example.test") is None
    sentinelone.verify_auth("https://example.test", "session=x")
    monkeypatch.setattr(sentinelone, "fetch_url", lambda *_args, **_kwargs: "not json")
    sentinelone.verify_auth("https://example.test", "session=x")

    monkeypatch.setattr(sentinelone, "CACHE_FILE", str(tmp_path / "cache.json"))
    assert sentinelone.load_cache() == {}
    sentinelone.save_cache({"x": {"sha256": "y"}})
    assert sentinelone.load_cache()["x"]["sha256"] == "y"
    assert sentinelone.clean_desc("<b>A</b> | B\nC") == "A \\| B C"
    assert sentinelone.clean_desc("") == ""

    with pytest.raises(SystemExit):
        sentinelone.build_cookie_header(Namespace(cookie=None, cookie_file=None))

    called: list[Namespace] = []
    monkeypatch.setattr(sentinelone, "sync", called.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch.py", "--base-url", "https://console.test", "--cookie", "x", "--dry-run"],
    )
    sentinelone.main()
    assert called[0].dry_run is True
