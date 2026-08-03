import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("vikunja")


def swagger_spec() -> dict:
    return {
        "swagger": "2.0",
        "info": {"version": "1"},
        "schemes": ["https"],
        "host": "vikunja.example.test",
        "basePath": "/api/v1",
        "definitions": {
            "Task": {
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
            }
        },
        "paths": {
            "/tasks": {
                "post": {
                    "summary": "Create task",
                    "tags": ["Tasks"],
                    "consumes": ["application/json"],
                    "produces": ["application/json"],
                    "parameters": [
                        {
                            "name": "task",
                            "in": "body",
                            "required": True,
                            "schema": {"$ref": "#/definitions/Task"},
                        },
                        {"name": "notify", "in": "query", "type": "boolean", "required": False},
                    ],
                    "responses": {
                        "200": {"description": "Created", "schema": {"$ref": "#/definitions/Task"}}
                    },
                }
            }
        },
    }


def test_swagger_conversion_separates_body_and_query_parameters() -> None:
    spec = swagger_spec()
    operation = spec["paths"]["/tasks"]["post"]

    markdown = fetcher.build_endpoint_markdown("/tasks", "post", operation, spec, "https://host/api/v1")

    assert "| `notify` | query | boolean | No |" in markdown
    assert "### Request Body" in markdown
    assert "`title` (string) **required**" in markdown
    assert "#### 200" in markdown


def test_sync_repairs_missing_cached_swagger_output(monkeypatch, tmp_path) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: json.dumps(swagger_spec()))
    args = Namespace(
        spec_url="https://example.test/swagger.json",
        host=None,
        force=False,
        dry_run=False,
        verbose=False,
    )

    fetcher.sync(args)
    endpoint = docs / "tasks" / "post-tasks.md"
    endpoint.unlink()
    fetcher.sync(args)

    assert endpoint.exists()


def test_swagger_schema_response_base_url_and_security_variants() -> None:
    spec = swagger_spec()
    spec["security"] = [{"jwt": []}]
    operation = {
        "summary": "List tasks",
        "tags": ["Tasks"],
        "deprecated": True,
        "parameters": [{"name": "ids", "in": "query", "type": "array", "items": {"type": "integer"}}],
        "produces": ["application/json"],
        "responses": {
            "200": {
                "description": "Tasks",
                "schema": {"type": "array", "items": {"$ref": "#/definitions/Task"}},
            }
        },
    }

    markdown = fetcher.build_endpoint_markdown("/tasks", "get", operation, spec, "https://host/api/v1")

    assert "**DEPRECATED**" in markdown
    assert "| `ids` | query | array of integer | No |" in markdown
    assert "array of object" in markdown
    assert "**jwt**" in markdown
    assert fetcher.build_base_url(spec, "override.test") == "https://override.test/api/v1"
    assert fetcher.schema_to_markdown(
        {"allOf": [{"$ref": "#/definitions/Task"}, {"properties": {"done": {"type": "boolean"}}}]},
        spec,
    ).startswith("object")
    assert fetcher.schema_to_markdown({"oneOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
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
    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: json.dumps(swagger_spec()))
    fetcher.sync(
        Namespace(
            spec_url="fixture",
            host=None,
            force=True,
            dry_run=True,
            verbose=True,
        )
    )

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(
        sys,
        "argv",
        ["fetch.py", "--spec-url", "fixture", "--host", "host", "--dry-run", "--force", "--verbose"],
    )
    fetcher.main()
    assert called[0].host == "host" and called[0].dry_run and called[0].force and called[0].verbose
