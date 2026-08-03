import gzip
import html
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("bitwarden")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"version": "1"},
        "components": {
            "schemas": {
                "Vault": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string", "format": "uuid"}},
                }
            }
        },
        "paths": {
            "/vaults/{id}": {
                "get": {
                    "summary": "Get vault",
                    "tags": ["Vaults"],
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {
                        "200": {
                            "description": "Vault",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/Vault"},
                                    "example": {"id": "fixture"},
                                }
                            },
                        }
                    },
                }
            }
        },
    }


def test_extracts_embedded_spec_and_converts_endpoint() -> None:
    spec = openapi_spec()
    payload = {"props": {"page": {"slug": "vault-management-api", "body": spec}}}
    page = f'<div data-page="{html.escape(json.dumps(payload), quote=True)}"></div>'

    extracted = fetcher.extract_openapi_from_page(page)
    operation = extracted["paths"]["/vaults/{id}"]["get"]
    markdown = fetcher.build_endpoint_markdown("/vaults/{id}", "get", operation, extracted)

    assert extracted == spec
    assert "| `id` | path | string | Yes |" in markdown
    assert "`id` (string (uuid)) **required**" in markdown
    assert '"id": "fixture"' in markdown


def test_process_openapi_repairs_missing_cached_endpoint(monkeypatch, tmp_path) -> None:
    api_dir = tmp_path / "docs" / "vault-management-api"
    monkeypatch.setattr(fetcher, "API_DOCS_DIR", str(api_dir))
    args = Namespace(dry_run=False, verbose=False)
    first_cache = {}

    fetcher.process_openapi(openapi_spec(), args, {}, first_cache)
    endpoint = api_dir / "vaults" / "get-vaults-id.md"
    endpoint.unlink()
    second_cache = {}
    fetcher.process_openapi(openapi_spec(), args, first_cache, second_cache)

    assert endpoint.exists()
    assert second_cache.keys() == first_cache.keys()


def test_cli_conversion_strips_frontmatter_and_jekyll_callout() -> None:
    raw = "---\ntitle: CLI\n---\n{% callout warning %}\nKeep the token safe.\n{% endcallout %}\n"

    markdown = fetcher.convert_cli(raw)

    assert markdown.startswith("# CLI")
    assert "> **Warning:**" in markdown
    assert "{%" not in markdown


def test_schema_helpers_cover_composition_parameters_body_and_variants() -> None:
    spec = openapi_spec()
    spec["components"]["schemas"]["ExtendedVault"] = {
        "allOf": [
            {"$ref": "#/components/schemas/Vault"},
            {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string", "enum": ["one", "two"]},
                    "metadata": {"type": "object", "additionalProperties": {"type": "string"}},
                },
            },
        ]
    }
    request = {
        "description": "Vault payload",
        "required": True,
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/ExtendedVault"},
                "example": {"name": "one"},
            }
        },
    }

    assert "`name` (string - enum: `one`, `two`) **required**" in fetcher.format_request_body(request, spec)
    assert "object (values: string)" in fetcher.format_request_body(request, spec)
    assert '"name": "one"' in fetcher.format_request_body(request, spec)
    assert fetcher.schema_to_markdown({"oneOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
    assert fetcher.schema_to_markdown({"type": "array", "items": {"type": "boolean"}}, spec) == (
        "array of boolean"
    )
    assert fetcher.schema_to_markdown({"$ref": "#/unknown"}, spec) == "`unknown`"
    assert "circular reference" in fetcher.schema_to_markdown(
        {"$ref": "#/components/schemas/Vault"}, spec, seen={"#/components/schemas/Vault"}
    )
    assert fetcher.resolve_ref("external.json", spec) == {}
    assert fetcher.endpoint_filename("GET", "/vaults/{id}") == "get-vaults-id.md"


def test_transport_and_cache_boundaries(monkeypatch, tmp_path) -> None:
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


def test_main_preserves_failures_and_removes_authoritative_stale_outputs(monkeypatch, tmp_path) -> None:
    docs = tmp_path / "docs"
    api_docs = docs / "vault-management-api"
    cache_file = tmp_path / ".cache.json"
    spec_file = tmp_path / "openapi.json"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "API_DOCS_DIR", str(api_docs))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(spec_file))
    monkeypatch.setattr(fetcher, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--verbose"])

    spec = openapi_spec()

    def page_for(value: dict) -> str:
        payload = {"slug": "vault-management-api", "body": value}
        return f'<div data-page="{html.escape(json.dumps(payload), quote=True)}"></div>'

    cli = "---\ntitle: CLI\n---\nUse the CLI.\n"
    monkeypatch.setattr(
        fetcher,
        "fetch_url",
        lambda url: cli if url == fetcher.CLI_SOURCE_URL else page_for(spec),
    )
    fetcher.main()

    endpoint = api_docs / "vaults" / "get-vaults-id.md"
    original_cache = json.loads(cache_file.read_text())
    assert (docs / "cli.md").exists()
    assert endpoint.exists()
    assert spec_file.exists()

    monkeypatch.setattr(fetcher, "fetch_url", lambda _url: None)
    fetcher.main()
    assert json.loads(cache_file.read_text()) == original_cache
    assert endpoint.exists()

    spec["paths"] = {}
    monkeypatch.setattr(
        fetcher,
        "fetch_url",
        lambda url: cli if url == fetcher.CLI_SOURCE_URL else page_for(spec),
    )
    fetcher.main()

    assert not endpoint.exists()
