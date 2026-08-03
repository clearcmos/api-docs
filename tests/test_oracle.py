from __future__ import annotations

import gzip
import sys
from pathlib import Path

import pytest

from tests.support import load_fetcher

oracle = load_fetcher("oracle")


def sample_spec() -> dict:
    return {
        "info": {"title": "Widgets", "version": "1", "description": "Widget API"},
        "servers": [{"url": "https://example.test"}],
        "tags": [{"name": "Widgets", "description": "Widget operations"}],
        "paths": {
            "/widgets/{id}": {
                "parameters": [{"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}],
                "get": {
                    "summary": "Get widget",
                    "tags": ["Widgets"],
                    "responses": {"200": {"description": "Found"}},
                },
            }
        },
    }


def test_schema_rendering_resolves_refs_and_marks_required_fields() -> None:
    spec = {
        "components": {
            "schemas": {
                "Widget": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string", "description": "Display name"},
                        "state": {"type": "string", "enum": ["on", "off"]},
                    },
                }
            }
        }
    }

    rendered = oracle.schema_to_markdown({"$ref": "#/components/schemas/Widget"}, spec)

    assert "`name` (string) **required**: Display name" in rendered
    assert "enum: `on`, `off`" in rendered
    assert (
        oracle.schema_to_markdown({"type": "array", "items": {"type": "integer"}}, spec) == "array of integer"
    )


def test_process_spec_writes_then_uses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle, "DOCS_DIR", str(tmp_path))
    first_cache: dict = {}

    written, skipped, entries, outputs = oracle.process_spec(
        "widgets", "Widgets", sample_spec(), {}, first_cache, False, False, False
    )

    assert written == 1
    assert skipped == 0
    assert entries == [("widgets", "Widgets", 1)]
    assert set(outputs) == {"widgets/README.md", "widgets/get-widgets-id.md"}
    endpoint = (tmp_path / "widgets" / "get-widgets-id.md").read_text()
    assert "**Method:** `GET`" in endpoint
    assert "| `id` | path | string | Yes |" in endpoint

    second_cache: dict = {}
    written, skipped, _, _ = oracle.process_spec(
        "widgets", "Widgets", sample_spec(), first_cache, second_cache, False, False, False
    )
    assert written == 0
    assert skipped == 1


def test_source_cache_requires_matching_manifest_and_all_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(oracle, "DOCS_DIR", str(tmp_path))
    info = {"specs": ["b.yaml", "a.yaml"], "toc_title": "Widgets"}
    output = tmp_path / "widgets" / "README.md"
    output.parent.mkdir()
    output.write_text("# Widgets\n")
    cache = {
        "source:widgets": {
            "fingerprint": oracle.spec_fingerprint(info),
            "outputs": ["widgets/README.md"],
        },
        "api:widgets:get:/widgets": {"sha256": "x"},
    }

    assert oracle.cached_api_is_complete("widgets", info, cache) is True
    output.unlink()
    assert oracle.cached_api_is_complete("widgets", info, cache) is False

    carried: dict = {}
    oracle.carry_api_cache("widgets", cache, carried)
    assert set(carried) == set(cache)


def test_yaml_parser_rejects_non_mapping_documents() -> None:
    parsed = oracle._parse_yaml_task(("widgets", "Widgets", "spec.yaml", b"- not\n- a mapping\n"))
    assert parsed[3] is None
    assert parsed[4] == "not a mapping"


def test_main_preserves_failed_api_and_removes_stale_endpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    stale_dir = docs / "retired"
    stale_dir.mkdir()
    stale_file = stale_dir / "get-old.md"
    stale_file.write_text("old")
    monkeypatch.setattr(oracle, "DOCS_DIR", str(docs))
    monkeypatch.setattr(oracle, "INDEX_FILE", str(tmp_path / "index.json"))
    index = {
        "cached": {"specs": ["cached.yaml"], "toc_title": "Cached"},
        "changed": {"specs": ["changed.yaml"], "toc_title": "Changed"},
        "failed": {"specs": ["failed.yaml"], "toc_title": "Failed"},
    }
    cached_source = {
        "fingerprint": oracle.spec_fingerprint(index["cached"]),
        "toc_title": "Cached",
        "endpoint_count": 2,
        "outputs": ["cached/README.md"],
    }
    (docs / "cached").mkdir()
    (docs / "cached" / "README.md").write_text("cached")
    cache = {
        "source:cached": cached_source,
        "api:cached:get:/cached": {"sha256": "x"},
        "source:failed": {
            "fingerprint": "old",
            "toc_title": "Failed old",
            "endpoint_count": 1,
            "outputs": [],
        },
        "api:retired:get:/old": {"sha256": "z"},
    }
    monkeypatch.setattr(oracle, "load_cache", lambda: cache)
    monkeypatch.setattr(oracle, "fetch_spec_index", lambda: index)
    monkeypatch.setattr(
        oracle,
        "download_specs",
        lambda *_args, **_kwargs: ({"changed": ("Changed", sample_spec())}, {"failed"}),
    )
    monkeypatch.setattr(
        oracle,
        "process_spec",
        lambda *_args, **_kwargs: (1, 0, [("widgets", "Changed", 1)], ["widgets/README.md"]),
    )
    summaries: list[list[tuple[str, str, int]]] = []
    monkeypatch.setattr(oracle, "write_top_readme", lambda value, _dry: summaries.append(value))
    saved: list[dict] = []
    monkeypatch.setattr(oracle, "save_cache", saved.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--verbose"])

    oracle.main()

    assert not stale_file.exists()
    assert {item[1] for item in summaries[0]} == {"Cached", "Changed", "Failed old"}
    assert "source:cached" in saved[0]
    assert "source:changed" in saved[0]


def test_main_force_dry_run_skips_persistence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oracle, "load_cache", lambda: pytest.fail("force must bypass cache"))
    monkeypatch.setattr(oracle, "fetch_spec_index", lambda: {})
    monkeypatch.setattr(oracle, "download_specs", lambda *_args, **_kwargs: ({}, set()))
    monkeypatch.setattr(oracle, "write_top_readme", lambda *_args: None)
    monkeypatch.setattr(oracle, "save_cache", lambda *_args: pytest.fail("dry run must not save"))
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--force", "--dry-run"])
    oracle.main()


def test_transport_cache_and_spec_index_boundaries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return gzip.compress(b'{"widgets": {}}')

    monkeypatch.setattr(oracle, "urlopen", lambda *_args, **_kwargs: Response())
    assert oracle.fetch_url("https://example.test") == b'{"widgets": {}}'
    assert oracle.fetch_spec_index() == {"widgets": {}}
    monkeypatch.setattr(oracle, "fetch_url", lambda *_args, **_kwargs: b"[]")
    with pytest.raises(SystemExit):
        oracle.fetch_spec_index()
    monkeypatch.setattr(oracle, "fetch_url", lambda *_args, **_kwargs: None)
    with pytest.raises(SystemExit):
        oracle.fetch_spec_index()

    monkeypatch.setattr(oracle, "CACHE_FILE", str(tmp_path / "cache.json"))
    assert oracle.load_cache() == {}
    oracle.save_cache({"x": {"sha256": "y"}})
    assert oracle.load_cache()["x"]["sha256"] == "y"


def test_rich_schema_request_response_and_security_rendering() -> None:
    spec = {
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "properties": {"id": {"type": "string", "format": "uuid"}},
                },
                "Extended": {
                    "allOf": [
                        {"$ref": "#/components/schemas/Base"},
                        {
                            "type": "object",
                            "required": ["state"],
                            "properties": {"state": {"type": "string"}},
                        },
                    ]
                },
            },
            "parameters": {
                "Trace": {
                    "name": "trace",
                    "in": "header",
                    "schema": {"type": "string", "format": "uuid"},
                    "description": "A | trace",
                }
            },
            "requestBodies": {
                "Body": {
                    "description": "Payload",
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Extended"}}},
                }
            },
            "responses": {
                "Found": {
                    "description": "Found",
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
            },
        }
    }
    operation = {
        "summary": "Create",
        "description": "Creates one.",
        "deprecated": True,
        "operationId": "createWidget",
        "tags": ["Widgets"],
        "parameters": [{"$ref": "#/components/parameters/Trace"}],
        "requestBody": {"$ref": "#/components/requestBodies/Body"},
        "responses": {"200": {"$ref": "#/components/responses/Found"}, "204": {}},
        "security": [{"oauth": ["write"]}, {"key": []}],
    }
    markdown = oracle.build_endpoint_markdown("/widgets", "post", operation, spec, "Widgets")
    assert "**DEPRECATED**" in markdown
    assert "**Required:** Yes" in markdown
    assert "**oauth**: write" in markdown
    assert "**key**" in markdown
    assert "A \\| trace" in markdown
    assert "uuid" in markdown
    assert oracle.schema_to_markdown({"$ref": "#/missing/Thing"}, spec) == "`Thing`"
    assert "circular reference" in oracle.schema_to_markdown(
        {"$ref": "#/components/schemas/Base"}, spec, seen={"#/components/schemas/Base"}
    )
    assert oracle.schema_to_markdown(
        {"type": "object", "additionalProperties": {"type": "integer"}}, spec
    ) == ("object (values: integer)")
    assert oracle.schema_to_markdown({"anyOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")


def test_download_specs_selects_newest_and_reports_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    payloads = {
        "old.yaml": b"info:\n  version: '1'\npaths: {}\n",
        "new.yaml": b"info:\n  version: '2'\npaths: {}\n",
        "missing.yaml": None,
    }
    monkeypatch.setattr(oracle, "fetch_spec_file", lambda path: payloads[path])
    index = {
        "widgets": {"toc_title": "Widgets", "specs": ["old.yaml", "new.yaml"]},
        "failed": {"toc_title": "Failed", "specs": ["missing.yaml"]},
    }

    results, failed = oracle.download_specs(index, verbose=True)

    assert results["widgets"][1]["info"]["version"] == "2"
    assert failed == {"failed"}
    filtered, filtered_failed = oracle.download_specs(index, verbose=False, api_keys={"failed"})
    assert filtered == {}
    assert filtered_failed == {"failed"}
