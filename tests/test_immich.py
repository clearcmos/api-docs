import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("immich")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"version": "1"},
        "components": {
            "schemas": {
                "Asset": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "format": "uuid"},
                        "type": {"type": "string", "enum": ["IMAGE", "VIDEO"]},
                    },
                }
            }
        },
        "paths": {
            "/assets/{id}": {
                "get": {
                    "summary": "Get asset",
                    "tags": ["Assets"],
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "responses": {
                        "200": {
                            "description": "Asset",
                            "content": {
                                "application/json": {"schema": {"$ref": "#/components/schemas/Asset"}}
                            },
                        }
                    },
                }
            }
        },
    }


def test_api_and_html_conversion_cover_both_sources() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/assets/{id}"]["get"]
    endpoint = fetcher.build_endpoint_markdown("/assets/{id}", "get", operation, spec)
    title, page = fetcher.html_to_markdown(
        "<html><title>Install | Immich</title><article><h1>Install</h1>"
        '<p>Read the <a href="/overview/introduction">overview</a>.</p>'
        '<pre class="language-shell">immich-server</pre></article></html>'
    )

    assert "`id` (string (uuid)) **required**" in endpoint
    assert "enum: `IMAGE`, `VIDEO`" in endpoint
    assert title == "Install | Immich"
    assert "# Install" in page
    assert "[overview](/overview/introduction)" in page
    assert "```shell" in page


def test_sync_api_repairs_missing_cached_endpoint(monkeypatch, tmp_path) -> None:
    api_dir = tmp_path / "docs" / "api"
    monkeypatch.setattr(fetcher, "API_DOCS_DIR", str(api_dir))
    args = Namespace(dry_run=False, verbose=False)
    first_cache = {}

    fetcher.sync_api(openapi_spec(), {}, first_cache, args)
    endpoint = api_dir / "assets" / "get-assets-id.md"
    endpoint.unlink()
    second_cache = {}
    result = fetcher.sync_api(openapi_spec(), first_cache, second_cache, args)

    assert endpoint.exists()
    assert result[0] == 1
    assert result[2] == 1


def test_sitemap_filter_and_path_mapping() -> None:
    sitemap = "<urlset><url><loc>https://docs.immich.app/install/docker-compose</loc></url></urlset>"

    assert fetcher.parse_sitemap(sitemap) == ["https://docs.immich.app/install/docker-compose"]
    assert fetcher.should_include_url("https://docs.immich.app/install/docker-compose")
    assert not fetcher.should_include_url("https://docs.immich.app/privacy-policy")
    assert fetcher.url_to_filepath("https://docs.immich.app/install/docker-compose") == (
        "install",
        "docker-compose.md",
    )


def test_rich_html_conversion_handles_structural_markdown() -> None:
    title, markdown = fetcher.html_to_markdown(
        "<html><title>Rich | Immich</title><nav><p>Hidden</p></nav><article>"
        '<div class="breadcrumbs"><p>Skip</p></div><h1>Rich</h1>'
        "<p><strong>Bold</strong> <em>emphasis</em> <code>inline</code> "
        '<a href="/install">link</a><br>next <img alt="Diagram" src="/img.png"></p><hr>'
        "<blockquote>Important &amp; safe</blockquote>"
        "<ul><li>One</li><li>Two<ol><li>Nested</li></ol></li></ul>"
        '<pre><code class="language-json">{&quot;ok&quot;: true}\n</code></pre>'
        "<table><thead><tr><th>Name</th><th>Value</th></tr></thead>"
        "<tbody><tr><td>A</td><td>1</td></tr></tbody></table>"
        "<details><summary>More</summary><p>Details</p></details>"
        "<script>hidden()</script></article></html>"
    )

    assert title == "Rich | Immich"
    assert "Hidden" not in markdown and "Skip" not in markdown and "hidden()" not in markdown
    assert "**Bold**" in markdown and "*emphasis*" in markdown and "`inline`" in markdown
    assert "> Important & safe" in markdown and "1. Nested" in markdown
    assert "```json" in markdown
    assert "| Name | Value |" in markdown and "| --- | --- |" in markdown
    assert "**More**" in markdown and "![Diagram](/img.png)" in markdown


def test_full_sync_preserves_failed_pages_then_removes_authoritative_stale_pages(
    monkeypatch, tmp_path
) -> None:
    docs = tmp_path / "docs"
    page_url = "https://docs.immich.app/install/docker-compose"
    sitemap = f"<urlset><url><loc>{page_url}</loc></url></urlset>"
    page = "<html><title>Docker | Immich</title><article><h1>Docker</h1><p>Install.</p></article></html>"
    raw_spec = json.dumps(openapi_spec())
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "API_DOCS_DIR", str(docs / "api"))
    monkeypatch.setattr(fetcher, "GENERAL_DOCS_DIR", str(docs / "general"))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "SPEC_FILE", str(tmp_path / "openapi.json"))
    sources = {fetcher.OPENAPI_URL: raw_spec, fetcher.SITEMAP_URL: sitemap, page_url: page}
    monkeypatch.setattr(fetcher, "fetch_url", sources.get)
    args = Namespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)
    output = docs / "general" / "install" / "docker-compose.md"
    original_cache = json.loads((tmp_path / ".cache.json").read_text())
    assert output.exists()

    sources[page_url] = None
    fetcher.sync(args)
    assert output.exists()
    assert (
        json.loads((tmp_path / ".cache.json").read_text())["docs:install:docker-compose.md"]
        == (original_cache["docs:install:docker-compose.md"])
    )

    sources[fetcher.SITEMAP_URL] = "<urlset></urlset>"
    fetcher.sync(args)
    assert not output.exists()


def test_transport_cache_schema_and_cli_boundaries(monkeypatch, tmp_path) -> None:
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
                {"$ref": "#/components/schemas/Asset"},
                {"type": "object", "properties": {"owner": {"type": "string"}}},
            ]
        },
        spec,
    )
    assert fetcher.schema_to_markdown({"oneOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
    assert (
        fetcher.schema_to_markdown({"type": "object", "additionalProperties": {"type": "integer"}}, spec)
        == "object (values: integer)"
    )
    assert fetcher.schema_to_markdown({"$ref": "#/missing"}, spec) == "`missing`"

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
