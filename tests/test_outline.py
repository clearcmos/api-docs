import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("outline")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"version": "1"},
        "components": {
            "schemas": {
                "Document": {
                    "type": "object",
                    "required": ["title"],
                    "properties": {
                        "title": {"type": "string", "description": "Document title"},
                        "archived": {"type": "boolean"},
                    },
                }
            }
        },
        "paths": {
            "/documents.info": {
                "post": {
                    "summary": "Read document",
                    "tags": ["Documents"],
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {"schema": {"$ref": "#/components/schemas/Document"}}
                        },
                    },
                    "responses": {"200": {"description": "Success"}},
                }
            }
        },
    }


def test_openapi_conversion_renders_request_schema_and_response() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/documents.info"]["post"]

    markdown = fetcher.build_endpoint_markdown("/documents.info", "post", operation, spec)

    assert "**Required:** Yes" in markdown
    assert "`title` (string) **required**: Document title" in markdown
    assert "#### 200" in markdown


def test_sync_uses_cached_output_only_while_file_exists(monkeypatch, tmp_path) -> None:
    raw = json.dumps(openapi_spec())
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: raw)
    args = Namespace(force=False, dry_run=False, verbose=False)

    fetcher.sync(args)
    endpoint = docs / "documents" / "post-documentsinfo.md"
    endpoint.unlink()
    fetcher.sync(args)

    assert endpoint.exists()


def test_schema_helpers_cover_nested_variants_parameters_and_security() -> None:
    spec = openapi_spec()
    operation = {
        "summary": "Update",
        "deprecated": True,
        "tags": ["Documents"],
        "parameters": [
            {
                "name": "id",
                "in": "query",
                "required": True,
                "description": "A | B",
                "schema": {"type": "string", "format": "uuid"},
            }
        ],
        "requestBody": {
            "content": {
                "application/json": {
                    "schema": {
                        "type": "object",
                        "properties": {"choice": {"anyOf": [{"type": "string"}, {"type": "integer"}]}},
                    }
                }
            }
        },
        "responses": {"400": {"description": "Bad request"}},
        "security": [{"apiKey": []}],
    }

    markdown = fetcher.build_endpoint_markdown("/documents.update", "post", operation, spec)

    assert "**DEPRECATED**" in markdown
    assert "string (uuid)" in markdown
    assert "A \\| B" in markdown
    assert "Any of: string | integer" in markdown
    assert "**apiKey**" in markdown
    assert (
        fetcher.schema_to_markdown({"type": "array", "items": {"type": "string"}}, spec) == "array of string"
    )
    assert fetcher.schema_to_markdown({"$ref": "#/missing"}, spec) == "`missing`"
    assert "title" in fetcher.schema_to_markdown(
        {
            "allOf": [
                {"$ref": "#/components/schemas/Document"},
                {"type": "object", "properties": {"revision": {"type": "integer"}}},
            ]
        },
        spec,
    )
    assert fetcher.schema_to_markdown({"anyOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")


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

    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path / "dry-docs"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: json.dumps(openapi_spec()))
    fetcher.sync(Namespace(force=True, dry_run=True, verbose=True))

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
