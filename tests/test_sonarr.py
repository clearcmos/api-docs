import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("sonarr")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Sonarr", "version": "3"},
        "servers": [
            {
                "url": "{protocol}://{hostpath}",
                "variables": {
                    "protocol": {"default": "http"},
                    "hostpath": {"default": "localhost:8989"},
                },
            }
        ],
        "components": {
            "schemas": {
                "Series": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {"title": {"type": "string"}},
                }
            }
        },
        "paths": {
            "/api/v3/series": {
                "post": {
                    "summary": None,
                    "operationId": None,
                    "description": None,
                    "tags": ["Series"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Series"}}},
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }


def test_servarr_conversion_handles_null_metadata_and_request_schema() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/api/v3/series"]["post"]

    markdown = fetcher.build_endpoint_markdown("/api/v3/series", "post", operation, spec)

    assert markdown.startswith("# POST /api/v3/series")
    assert "**Base URL:** `http://localhost:8989`" in markdown
    assert "`title` (string) **required**" in markdown
    assert "#### 201" in markdown


def test_sync_removes_stale_endpoint_after_complete_discovery(monkeypatch, tmp_path) -> None:
    spec = openapi_spec()
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: json.dumps(spec))
    args = Namespace(force=False, dry_run=False, verbose=False)

    fetcher.sync(args)
    endpoint = docs / "series" / "post-api-v3-series.md"
    assert endpoint.exists()
    spec["paths"] = {}
    fetcher.sync(args)

    assert not endpoint.exists()


def test_schema_variants_parameters_responses_and_security() -> None:
    spec = openapi_spec()
    operation = {
        "summary": "Find series",
        "tags": ["Series"],
        "parameters": [
            {
                "name": "term",
                "in": "query",
                "required": False,
                "description": "Search | term",
                "schema": {"type": "string"},
            }
        ],
        "responses": {
            "200": {
                "description": "Matches",
                "content": {
                    "application/json": {
                        "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Series"}}
                    }
                },
            }
        },
        "security": [{"apiKey": ["read"]}],
    }

    markdown = fetcher.build_endpoint_markdown("/api/v3/series/lookup", "get", operation, spec)

    assert "Search \\| term" in markdown
    assert "array of object" in markdown
    assert "`title` (string) **required**" in markdown
    assert "**apiKey**: read" in markdown
    assert (
        fetcher.schema_to_markdown({"type": "object", "additionalProperties": {"type": "boolean"}}, spec)
        == "object (values: boolean)"
    )
    assert fetcher.schema_to_markdown({"anyOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
    assert fetcher.schema_to_markdown({"$ref": "#/missing"}, spec) == "`missing`"
    assert "title" in fetcher.schema_to_markdown(
        {
            "allOf": [
                {"$ref": "#/components/schemas/Series"},
                {"type": "object", "properties": {"year": {"type": "integer"}}},
            ]
        },
        spec,
    )


def test_transport_cache_dry_run_and_cli_boundaries(monkeypatch, tmp_path) -> None:
    class Response:
        headers = {"Content-Encoding": "gzip"}

        def read(self) -> bytes:
            return gzip.compress(b"fixture")

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    monkeypatch.setattr(fetcher, "urlopen", lambda _request, timeout: Response())
    assert fetcher.fetch_url("https://example.test") == "fixture"

    def fail(_request, timeout):
        raise URLError("offline")

    monkeypatch.setattr(fetcher, "urlopen", fail)
    assert fetcher.fetch_url("https://example.test") is None

    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    fetcher.save_cache({"entry": {"sha256": "abc"}})
    assert fetcher.load_cache()["entry"]["sha256"] == "abc"

    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path / "dry-docs"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: json.dumps(openapi_spec()))
    fetcher.sync(Namespace(force=True, dry_run=True, verbose=True))

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
