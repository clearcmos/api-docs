import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("langfuse")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"version": "1"},
        "components": {
            "schemas": {
                "Trace": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string"},
                        "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
                    },
                }
            }
        },
        "paths": {
            "/traces": {
                "post": {
                    "summary": "Create trace",
                    "tags": ["Traces"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Trace"}}},
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }


def test_openapi_and_mdx_conversion_cover_both_sources() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/traces"]["post"]
    endpoint = fetcher.build_endpoint_markdown("/traces", "post", operation, spec)
    mdx = fetcher.mdx_to_markdown(
        'import Widget from "./Widget"\n<Callout type="info">\nKeep this text.\n</Callout>\n'
        "```tsx\n<Callout>literal</Callout>\n```\n"
    )

    assert "`id` (string) **required**" in endpoint
    assert "object (values: string)" in endpoint
    assert "#### 201" in endpoint
    assert "import Widget" not in mdx
    assert "Keep this text." in mdx
    assert "<Callout>literal</Callout>" in mdx


def test_sync_api_repairs_missing_cached_endpoint(monkeypatch, tmp_path) -> None:
    api_dir = tmp_path / "docs" / "api"
    monkeypatch.setattr(fetcher, "API_DOCS_DIR", str(api_dir))
    args = Namespace(dry_run=False, verbose=False)
    first_cache = {}

    fetcher.sync_api(openapi_spec(), {}, first_cache, args)
    endpoint = api_dir / "traces" / "post-traces.md"
    endpoint.unlink()
    second_cache = {}
    result = fetcher.sync_api(openapi_spec(), first_cache, second_cache, args)

    assert endpoint.exists()
    assert result[0] == 1
    assert result[2] == 1


def test_mdx_converter_strips_module_and_component_chrome() -> None:
    markdown = fetcher.mdx_to_markdown(
        'import Widget from "./Widget"\nexport const metadata = {}\n<Widget />\n'
        '<Frame><CloudflareVideo id="fixture" /></Frame>\n'
        '<Tabs><Tab title="One">Content</Tab></Tabs>\n\n\n\n'
        "```mdx\n<Widget />\n```\n"
    )

    assert "import Widget" not in markdown and "export const" not in markdown
    assert "<Widget />" not in markdown.split("```mdx", 1)[0]
    assert "[Video]" in markdown and "Content" in markdown
    assert "```mdx\n<Widget />\n```" in markdown
    assert "\n\n\n" not in markdown


def test_full_sync_preserves_failed_self_hosted_pages_then_removes_stale_pages(monkeypatch, tmp_path) -> None:
    docs = tmp_path / "docs"
    prefix = fetcher.SELF_HOSTING_PREFIX
    top_source = f"{prefix}/index.mdx"
    nested_source = f"{prefix}/deployment/setup.mdx"
    meta_source = f"{prefix}/deployment/meta.json"
    tree_entries = [
        {"path": top_source, "type": "blob"},
        {"path": nested_source, "type": "blob"},
        {"path": meta_source, "type": "blob"},
        {"path": "other/file.mdx", "type": "blob"},
    ]
    sources = {
        fetcher.OPENAPI_SPEC_URL: json.dumps(openapi_spec()),
        fetcher.GITHUB_TREE_URL: json.dumps({"tree": tree_entries}),
        f"{fetcher.GITHUB_RAW_BASE}/{top_source}": "---\ntitle: Overview\n---\n# Overview\n",
        f"{fetcher.GITHUB_RAW_BASE}/{nested_source}": "---\ntitle: Setup\n---\n# Setup\n",
        f"{fetcher.GITHUB_RAW_BASE}/{meta_source}": '{"title": "Deployment"}',
    }
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "API_DOCS_DIR", str(docs / "api"))
    monkeypatch.setattr(fetcher, "SELF_HOSTING_DOCS_DIR", str(docs / "self-hosting"))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.yaml"))
    monkeypatch.setattr(fetcher, "fetch_url", sources.get)
    args = Namespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)
    nested = docs / "self-hosting" / "deployment" / "setup.md"
    original_cache = json.loads((tmp_path / ".cache.json").read_text())
    assert nested.exists()

    sources[f"{fetcher.GITHUB_RAW_BASE}/{nested_source}"] = None
    fetcher.sync(args)
    assert nested.exists()
    assert (
        json.loads((tmp_path / ".cache.json").read_text())["selfhost:deployment/setup.md"]
        == (original_cache["selfhost:deployment/setup.md"])
    )

    sources[fetcher.GITHUB_TREE_URL] = None
    fetcher.sync(args)
    assert nested.exists()

    sources[fetcher.GITHUB_TREE_URL] = json.dumps(
        {"tree": [entry for entry in tree_entries if entry["path"] != nested_source]}
    )
    fetcher.sync(args)
    assert not nested.exists()


def test_transport_cache_schema_dry_run_and_cli_boundaries(monkeypatch, tmp_path) -> None:
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
    assert fetcher.load_cache() == {}
    fetcher.save_cache({"entry": {"sha256": "abc"}})
    assert fetcher.load_cache()["entry"]["sha256"] == "abc"

    spec = openapi_spec()
    assert "id" in fetcher.schema_to_markdown(
        {
            "allOf": [
                {"$ref": "#/components/schemas/Trace"},
                {"type": "object", "properties": {"name": {"type": "string"}}},
            ]
        },
        spec,
    )
    assert fetcher.schema_to_markdown({"oneOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
    assert fetcher.schema_to_markdown({"type": "array", "items": {"type": "boolean"}}, spec) == (
        "array of boolean"
    )
    assert fetcher.schema_to_markdown({"$ref": "#/missing"}, spec) == "`missing`"
    operation = {
        "summary": "List traces",
        "deprecated": True,
        "tags": ["Traces"],
        "parameters": [
            {
                "name": "limit",
                "in": "query",
                "required": True,
                "description": "Page | limit",
                "schema": {"type": "integer", "format": "int32"},
            }
        ],
        "responses": {
            "200": {
                "description": "Traces",
                "content": {
                    "application/json": {
                        "schema": {"type": "array", "items": {"$ref": "#/components/schemas/Trace"}}
                    }
                },
            }
        },
        "security": [{"token": ["read"]}],
    }
    markdown = fetcher.build_endpoint_markdown("/traces", "get", operation, spec)
    assert "**DEPRECATED**" in markdown
    assert "integer (int32)" in markdown and "Page \\| limit" in markdown
    assert "array of object" in markdown and "**token**: read" in markdown

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
