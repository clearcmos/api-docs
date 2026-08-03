import gzip
import json
import sys
from argparse import Namespace
from urllib.error import URLError

from tests.support import load_fetcher

fetcher = load_fetcher("clickup")


def openapi_spec() -> dict:
    return {
        "openapi": "3.0.0",
        "info": {"version": "2"},
        "components": {
            "schemas": {
                "Task": {
                    "type": "object",
                    "required": ["name"],
                    "properties": {
                        "name": {"type": "string"},
                        "priority": {"anyOf": [{"type": "integer"}, {"type": "string"}]},
                    },
                }
            }
        },
        "paths": {
            "/tasks": {
                "post": {
                    "summary": "Create task",
                    "tags": ["Tasks"],
                    "requestBody": {
                        "required": True,
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Task"}}},
                    },
                    "responses": {"201": {"description": "Created"}},
                }
            }
        },
    }


def test_openapi_and_guide_converters_preserve_meaning() -> None:
    spec = openapi_spec()
    operation = spec["paths"]["/tasks"]["post"]
    endpoint = fetcher.build_endpoint_markdown("/tasks", "post", operation, spec)
    title, guide = fetcher.html_to_markdown(
        "<html><title>Auth | ClickUp</title><main><h1>Auth</h1>"
        '<p>Use <a href="/docs/tasks">tasks</a>.</p><pre class="language-json">{}</pre></main></html>'
    )

    assert "`name` (string) **required**" in endpoint
    assert "Any of: integer | string" in endpoint
    assert "#### 201" in endpoint
    assert title == "Auth | ClickUp"
    assert "# Auth" in guide
    assert "[tasks](/docs/tasks)" in guide
    assert "```json" in guide


def test_sync_api_repairs_missing_cached_endpoint(monkeypatch, tmp_path) -> None:
    api_dir = tmp_path / "docs" / "api-v2"
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(tmp_path / "docs"))
    args = Namespace(dry_run=False, verbose=False)
    first_cache = {}

    fetcher.sync_api(openapi_spec(), str(api_dir), "v2", "V2", {}, first_cache, args)
    endpoint = api_dir / "tasks" / "post-tasks.md"
    endpoint.unlink()
    second_cache = {}
    result = fetcher.sync_api(openapi_spec(), str(api_dir), "v2", "V2", first_cache, second_cache, args)

    assert endpoint.exists()
    assert result[0] == 1
    assert result[2] == 1


def test_sitemap_classification_is_deterministic() -> None:
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        "<url><loc>https://developer.clickup.com/docs/authentication</loc></url>"
        "<url><loc>https://developer.clickup.com/reference/tasks</loc></url></urlset>"
    )

    assert fetcher.parse_sitemap(sitemap) == [
        "https://developer.clickup.com/docs/authentication",
        "https://developer.clickup.com/reference/tasks",
    ]
    assert fetcher.classify_docs_url("https://developer.clickup.com/docs/authentication") == (
        "getting-started",
        "authentication",
    )
    assert fetcher.classify_docs_url("https://developer.clickup.com/reference/tasks") is None


def test_rich_html_conversion_handles_structural_markdown() -> None:
    title, markdown = fetcher.html_to_markdown(
        "<html><title>Rich Guide | ClickUp</title><nav><p>Hidden</p></nav>"
        '<main><div class="breadcrumbs"><p>Skip me</p></div><h1>Rich Guide</h1>'
        "<p><strong>Bold</strong> and <em>emphasis</em> with <code>inline</code> "
        '<a href="/docs/tasks">link</a><br>next.</p><hr>'
        "<blockquote>Important &amp; safe</blockquote>"
        "<ul><li>One</li><li>Two<ol><li>Nested</li></ol></li></ul>"
        '<pre><code class="language-json">{&quot;ok&quot;: true}\n</code></pre>'
        "<table><thead><tr><th>Name</th><th>Value</th></tr></thead>"
        "<tbody><tr><td>A</td><td>1</td></tr></tbody></table>"
        "<details><summary>More</summary><p>Details</p></details>"
        '<p><img alt="Diagram" src="/image.png"></p><script>hidden()</script></main></html>'
    )

    assert title == "Rich Guide | ClickUp"
    assert "Hidden" not in markdown and "Skip me" not in markdown and "hidden()" not in markdown
    assert "**Bold**" in markdown and "*emphasis*" in markdown and "`inline`" in markdown
    assert "> Important & safe" in markdown
    assert "1. Nested" in markdown
    assert "```json" in markdown
    assert "| Name | Value |" in markdown and "| --- | --- |" in markdown
    assert "**More**" in markdown and "![Diagram](/image.png)" in markdown


def test_full_sync_preserves_failed_guides_then_removes_stale_guides(monkeypatch, tmp_path) -> None:
    docs = tmp_path / "docs"
    guide_url = "https://developer.clickup.com/docs/authentication"
    sitemap = (
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{guide_url}</loc></url></urlset>"
    )
    page = "<html><title>Auth | ClickUp</title><main><h1>Auth</h1><p>Authenticate.</p></main></html>"
    raw_spec = json.dumps(openapi_spec())
    monkeypatch.setattr(fetcher, "DOCS_DIR", str(docs))
    monkeypatch.setattr(fetcher, "API_V2_DIR", str(docs / "api-v2"))
    monkeypatch.setattr(fetcher, "API_V3_DIR", str(docs / "api-v3"))
    monkeypatch.setattr(fetcher, "GUIDES_DIR", str(docs / "guides"))
    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    monkeypatch.setattr(fetcher, "V2_SPEC_FILE", str(tmp_path / "openapi-v2.json"))
    monkeypatch.setattr(fetcher, "V3_SPEC_FILE", str(tmp_path / "openapi-v3.yaml"))
    monkeypatch.setattr(fetcher, "fetch_bytes", lambda _url: raw_spec.encode())
    sources = {fetcher.V2_SPEC_URL: raw_spec, fetcher.SITEMAP_URL: sitemap, guide_url: page}
    monkeypatch.setattr(fetcher, "fetch_url", sources.get)
    args = Namespace(force=False, dry_run=False, verbose=True)

    fetcher.sync(args)
    guide = docs / "guides" / "getting-started" / "authentication.md"
    original_cache = json.loads((tmp_path / ".cache.json").read_text())
    assert guide.exists()

    sources[guide_url] = None
    fetcher.sync(args)
    assert guide.exists()
    assert (
        json.loads((tmp_path / ".cache.json").read_text())["guides:getting-started:authentication.md"]
        == (original_cache["guides:getting-started:authentication.md"])
    )

    sources[fetcher.SITEMAP_URL] = "<urlset></urlset>"
    fetcher.sync(args)
    assert not guide.exists()


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
    assert fetcher.fetch_bytes("https://example.test") == b"fixture"

    def fail(_request, timeout):
        raise URLError("offline")

    monkeypatch.setattr(fetcher, "urlopen", fail)
    assert fetcher.fetch_url("https://example.test") is None
    assert fetcher.fetch_bytes("https://example.test") is None

    monkeypatch.setattr(fetcher, "CACHE_FILE", str(tmp_path / ".cache.json"))
    fetcher.save_cache({"entry": {"sha256": "abc"}})
    assert fetcher.load_cache()["entry"]["sha256"] == "abc"

    spec = openapi_spec()
    assert "name" in fetcher.schema_to_markdown(
        {
            "allOf": [
                {"$ref": "#/components/schemas/Task"},
                {"type": "object", "properties": {"done": {"type": "boolean"}}},
            ]
        },
        spec,
    )
    assert fetcher.schema_to_markdown({"oneOf": [{"type": "string"}] * 6}, spec).endswith("... and 1 more")
    assert fetcher.schema_to_markdown({"$ref": "#/missing"}, spec) == "`missing`"

    called = []
    monkeypatch.setattr(fetcher, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    fetcher.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
