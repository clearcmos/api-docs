import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("circleci")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"version": "2"},
        "servers": [{"url": "https://circle.example.test/api/v2"}],
        "components": {
            "schemas": {
                "Pipeline": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "format": "uuid"},
                        "status": {"oneOf": [{"type": "string"}, {"type": "integer"}]},
                    },
                }
            }
        },
        "paths": {
            "/pipeline/{id}": {
                "get": {
                    "summary": "Get pipeline",
                    "tags": ["Pipeline"],
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {
                        "200": {
                            "description": "Pipeline",
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Pipeline"}}
                            },
                        }
                    },
                }
            }
        },
    }


def test_openapi_conversion_resolves_schema_and_parameter() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/pipeline/{id}"]["get"]

    markdown = fetcher.build_endpoint_markdown("/pipeline/{id}", "get", operation, spec)

    assert "| `id` | path | string | Yes |" in markdown
    assert "`id` (string (uuid)) **required**" in markdown
    assert "One of: string | integer" in markdown


def test_sync_removes_outputs_absent_from_complete_spec(monkeypatch, tmp_path) -> None:
    spec = openapi_spec()
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path / "docs"))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: json.dumps(spec))
    args = Namespace(force=False, dry_run=False, verbose=False)

    fetcher.sync(args)
    endpoint = tmp_path / "docs" / "pipeline" / "get-pipeline-id.md"
    assert endpoint.exists()
    spec["paths"] = {}

    fetcher.sync(args)

    assert not endpoint.exists()


def test_schema_helpers_cover_composition_refs_and_body_formats() -> None:
    spec = openapi_spec()
    spec["components"]["parameters"] = {
        "Limit": {"name": "limit", "in": "query", "schema": {"type": "integer", "format": "int32"}}
    }
    spec["components"]["requestBodies"] = {
        "Body": {
            "description": "Payload",
            "required": True,
            "content": {"application/json": {"schema": {"type": "array", "items": {"type": "string"}}}},
        }
    }
    spec["components"]["responses"] = {"Missing": {"description": "Not found"}}

    assert "id" in fetcher.resolve_ref("#/components/schemas/Pipeline", spec)["properties"]
    assert fetcher.resolve_ref("external.json", spec) == {}
    assert fetcher.schema_to_markdown({"$ref": "#/components/schemas/Unknown"}, spec) == "`Unknown`"
    assert "circular reference" in fetcher.schema_to_markdown(
        {"$ref": "#/components/schemas/Pipeline"}, spec, seen={"#/components/schemas/Pipeline"}
    )
    assert (
        fetcher.schema_to_markdown({"type": "array", "items": {"type": "boolean"}}, spec)
        == "array of boolean"
    )
    assert (
        fetcher.schema_to_markdown({"type": "object", "additionalProperties": {"type": "integer"}}, spec)
        == "object (values: integer)"
    )
    assert "id" in fetcher.schema_to_markdown(
        {
            "allOf": [
                {"$ref": "#/components/schemas/Pipeline"},
                {"type": "object", "properties": {"number": {"type": "integer"}}},
            ]
        },
        spec,
    )
    assert "`limit` | query | integer (int32)" in fetcher.format_parameters(
        [{"$ref": "#/components/parameters/Limit"}], spec
    )
    assert "array of string" in fetcher.format_request_body({"$ref": "#/components/requestBodies/Body"}, spec)
    assert "#### 404" in fetcher.format_responses({"404": {"$ref": "#/components/responses/Missing"}}, spec)


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

    cache_file = tmp_path / ".cache.json"
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(cache_file))
    fetcher.save_cache({"page": {"sha256": "abc"}})
    assert fetcher.load_cache()["page"]["sha256"] == "abc"

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
