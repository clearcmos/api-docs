import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("radarr")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Radarr", "version": "3"},
        "servers": [
            {
                "url": "{protocol}://{hostpath}",
                "variables": {
                    "protocol": {"default": "http"},
                    "hostpath": {"default": "localhost:7878"},
                },
            }
        ],
        "components": {
            "schemas": {
                "Queue": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "integer", "format": "int32"}},
                }
            }
        },
        "paths": {
            "/api/v3/queue/{id}": {
                "get": {
                    "summary": None,
                    "operationId": None,
                    "description": None,
                    "tags": ["Queue"],
                    "responses": {
                        "200": {
                            "description": "Success",
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Queue"}}
                            },
                        }
                    },
                }
            }
        },
    }


def test_servarr_conversion_handles_null_metadata_and_server_template() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/api/v3/queue/{id}"]["get"]

    markdown = fetcher.build_endpoint_markdown("/api/v3/queue/{id}", "get", operation, spec)

    assert markdown.startswith("# GET /api/v3/queue/{id}")
    assert "**Base URL:** `http://localhost:7878`" in markdown
    assert "`id` (integer (int32)) **required**" in markdown
    assert "**Operation ID:**" not in markdown


def test_sync_repairs_missing_cached_endpoint(monkeypatch, tmp_path) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: json.dumps(openapi_spec()))
    args = Namespace(force=False, dry_run=False, verbose=False)

    fetcher.sync(args)
    endpoint = docs / "queue" / "get-api-v3-queue-id.md"
    endpoint.unlink()
    fetcher.sync(args)

    assert endpoint.exists()


def test_schema_parameter_body_response_and_security_helpers() -> None:
    spec = openapi_spec()
    spec["components"]["schemas"]["ExtendedQueue"] = {
        "allOf": [
            {"$ref": "#/components/schemas/Queue"},
            {
                "type": "object",
                "required": ["status"],
                "properties": {
                    "status": {"type": "string", "enum": ["queued", "done"]},
                    "labels": {"type": "array", "items": {"type": "string"}},
                },
            },
        ]
    }
    operation = {
        "summary": "Update queue",
        "tags": ["Queue"],
        "deprecated": True,
        "parameters": [
            {
                "name": "id",
                "in": "path",
                "required": True,
                "description": "Queue | id",
                "schema": {"type": "integer", "format": "int32"},
            }
        ],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ExtendedQueue"}}},
        },
        "responses": {"202": {"description": "Accepted"}},
        "security": [{"apiKey": []}],
    }

    markdown = fetcher.build_endpoint_markdown("/api/v3/queue/{id}", "put", operation, spec)

    assert "**DEPRECATED**" in markdown
    assert "integer (int32)" in markdown
    assert "Queue \\| id" in markdown
    assert "`status` (string -- enum: `queued`, `done`) **required**" in markdown
    assert "array of string" in markdown
    assert "**apiKey**" in markdown
    assert fetcher.schema_to_markdown({"oneOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
    assert fetcher.schema_to_markdown({"type": "object"}, spec) == "object"
    assert fetcher.resolve_ref("external.json", spec) == {}


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
