import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("cloudflare")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"title": "Fixture", "version": "1"},
        "servers": [{"url": "https://api.example.test"}],
        "tags": [{"name": "Pets", "description": "Pet operations"}],
        "components": {
            "schemas": {
                "Pet": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "format": "uuid"},
                        "state": {"type": "string", "enum": ["ready", "sleeping"]},
                    },
                }
            }
        },
        "paths": {
            "/pets/{petId}": {
                "get": {
                    "summary": "Get pet",
                    "tags": ["Pets"],
                    "parameters": [
                        {
                            "name": "petId",
                            "in": "path",
                            "required": True,
                            "description": "Pet | identifier",
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Found",
                            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Pet"}}},
                        }
                    },
                }
            }
        },
    }


def test_openapi_conversion_includes_schema_parameters_and_response() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/pets/{petId}"]["get"]

    markdown = fetcher.build_endpoint_markdown("/pets/{petId}", "get", operation, spec)

    assert "# Get pet" in markdown
    assert "| `petId` | path | string | Yes | Pet \\| identifier |" in markdown
    assert "`id` (string (uuid)) **required**" in markdown
    assert "enum: `ready`, `sleeping`" in markdown


def test_sync_repairs_a_missing_cached_output(monkeypatch, tmp_path) -> None:
    docs_dir = tmp_path / "docs"
    cache_file = tmp_path / ".cache.json"
    raw = json.dumps(openapi_spec())
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs_dir))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: raw)
    args = Namespace(force=False, dry_run=False, verbose=False)

    fetcher.sync(args)
    endpoint = docs_dir / "pets" / "get-pets-petid.md"
    assert endpoint.exists()
    endpoint.unlink()

    fetcher.sync(args)

    assert endpoint.exists()
    assert "# Get pet" in endpoint.read_text()


def test_schema_helpers_cover_composition_body_and_security() -> None:
    spec = openapi_spec()
    spec["components"]["schemas"]["Envelope"] = {
        "allOf": [
            {"$ref": "#/components/schemas/Pet"},
            {"type": "object", "properties": {"meta": {"type": "object", "additionalProperties": True}}},
        ]
    }
    operation = {
        "tags": ["Pets"],
        "deprecated": True,
        "parameters": [{"name": "limit", "in": "query", "schema": {"type": "integer"}}],
        "requestBody": {
            "required": True,
            "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Envelope"}}},
        },
        "responses": {"204": {"description": "Done"}},
        "security": [{"token": ["read"]}, {"key": []}],
    }

    markdown = fetcher.build_endpoint_markdown("/pets", "post", operation, spec)

    assert "**DEPRECATED**" in markdown
    assert "`meta` (object)" in markdown
    assert "- **token**: read" in markdown
    assert "- **key**" in markdown
    assert fetcher.schema_to_markdown({"oneOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
    assert fetcher.schema_to_markdown({"type": "object"}, spec) == "object"
    assert fetcher.schema_to_markdown({}, spec) == "any"
    assert fetcher.resolve_ref("#/missing", spec) == {}


def test_transport_cache_and_cli_boundaries(monkeypatch, tmp_path) -> None:
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

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
