from __future__ import annotations

import base64
import json
import sys
import zlib
from argparse import Namespace
from pathlib import Path

import pytest

from tests.support import load_fetcher

rippling = load_fetcher("rippling")


def test_bundle_discovery_and_chunk_selection() -> None:
    html = (
        '<script src="/assets/js/runtime~main.abc.js"></script><script src="/assets/js/main.def.js"></script>'
    )
    assert rippling.extract_js_bundle_urls(html) == (
        "https://developer.rippling.com/assets/js/main.def.js",
        "https://developer.rippling.com/assets/js/runtime~main.abc.js",
    )
    entry = {"all_chunks": ["10", "20"]}
    assert rippling.determine_content_chunk(entry, {"20": "assets/js/page.js"}) == "assets/js/page.js"
    assert rippling.determine_content_chunk({"all_chunks": []}, {}) is None


def test_compressed_api_spec_round_trips() -> None:
    spec = {"summary": "List workers", "method": "get", "path": "/workers", "responses": {"200": {}}}
    encoded = base64.b64encode(zlib.compress(json.dumps(spec).encode())).decode()

    assert rippling.extract_api_spec_from_chunk(f'const page={{api:"{encoded}"}};') == spec
    assert rippling.extract_api_spec_from_chunk('const page={api:"not-base64"};') is None


def test_openapi_builders_render_request_schema_and_security() -> None:
    spec = {
        "summary": "Create worker",
        "description": "Creates a worker.",
        "method": "post",
        "path": "/workers",
        "tags": ["Workers"],
        "parameters": [{"name": "dry", "in": "query", "schema": {"type": "boolean"}}],
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {"schema": {"type": "object", "properties": {"name": {"type": "string"}}}}
            },
        },
        "responses": {"201": {"description": "Created"}},
        "security": [{"oauth": ["workers:write"]}],
    }

    markdown = rippling.build_endpoint_markdown(spec, {"title": "Create worker"})

    assert "**Method:** `POST`" in markdown
    assert "| `dry` | query | boolean | No |" in markdown
    assert "workers:write" in markdown
    assert rippling.schema_to_markdown({"oneOf": [{"type": "string"}, {"type": "integer"}]}, {}) == (
        "One of: string | integer"
    )


def test_schema_helpers_cover_refs_composition_objects_arrays_and_enums() -> None:
    spec = {
        "components": {
            "schemas": {
                "Base": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {"id": {"type": "string", "description": "Identifier"}},
                },
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/components/schemas/Node"}},
                },
            },
            "parameters": {
                "Limit": {
                    "name": "limit",
                    "in": "query",
                    "required": True,
                    "description": "Max | count",
                    "schema": {"type": ["integer", "null"], "format": "int32"},
                }
            },
            "requestBodies": {
                "Body": {
                    "description": "Payload",
                    "required": True,
                    "content": {
                        "application/json": {
                            "schema": {"$ref": "#/components/schemas/Base"},
                            "example": {"id": "1"},
                        }
                    },
                }
            },
            "responses": {
                "Ok": {
                    "description": "Success",
                    "content": {
                        "application/json": {"schema": {"type": "array", "items": {"type": "string"}}}
                    },
                }
            },
        }
    }
    assert rippling.resolve_ref("https://example.test", spec) == {}
    assert rippling.resolve_ref("#/components/schemas/Base", spec)["type"] == "object"
    assert rippling.resolve_ref("#/components/schemas/Base/type/x", spec) == {}
    assert "id" in rippling.schema_to_markdown({"$ref": "#/components/schemas/Base"}, spec)
    assert "circular reference" in rippling.schema_to_markdown(
        {"$ref": "#/components/schemas/Node"}, spec, seen={"#/components/schemas/Node"}
    )
    assert rippling.schema_to_markdown({"$ref": "#/missing"}, spec) == "`missing`"
    assert "... and 1 more" in rippling.schema_to_markdown({"anyOf": [{"type": "string"}] * 6}, spec)
    assert rippling.schema_to_markdown(
        {"allOf": [{"$ref": "#/components/schemas/Base"}, {"properties": {"name": {"type": "string"}}}]},
        spec,
    ).startswith("object")
    assert rippling.schema_to_markdown(
        {"type": "object", "additionalProperties": {"type": "integer"}}, spec
    ) == ("object (values: integer)")
    assert (
        rippling.schema_to_markdown(
            {"type": "object", "properties": {"x": {"type": "string"}}}, spec, depth=2
        )
        == "object (1 properties)"
    )
    assert "11 total" in rippling.schema_to_markdown(
        {"type": "string", "format": "uuid", "enum": list(range(11))}, spec
    )
    assert rippling.schema_to_markdown([], spec) == "any"

    params = rippling.format_parameters([{"$ref": "#/components/parameters/Limit"}], spec)
    assert "integer | null (int32)" in params and "Max \\| count" in params
    body = rippling.format_request_body({"$ref": "#/components/requestBodies/Body"}, spec)
    assert "**Required:** Yes" in body and '"id": "1"' in body
    responses = rippling.format_responses({"200": {"$ref": "#/components/responses/Ok"}, "204": {}}, spec)
    assert "#### 200" in responses and "array of string" in responses and "#### 204" in responses
    assert rippling.format_parameters([], spec) == ""
    assert rippling.format_request_body({}, spec) == ""
    assert rippling.format_responses({}, spec) == ""


def test_bundle_registry_parsers_cover_realistic_webpack_shapes() -> None:
    route_a = "/documentation/rest-api/workers/list"
    route_b = "/documentation/rest-api/teams/get"
    main = (
        f'path:"{route_a}",component:p("x","111"),'
        f'path:"{route_b}",component:p("x","222"),'
        f'"{route_a}-111":{{"content":"hashA"}},'
        f'"{route_b}-222":{{"content":"hashB"}},'
        '"hashA":[n.e(10),n.e(20),n.bind(n,30),"@site/docs/list.api.mdx"],'
        'hashB:[n.e(40),n.bind(n,50),"@site/docs/team.mdx"]'
    )
    routes = rippling.parse_route_mappings(main)
    assert routes == [(route_a, "111"), (route_b, "222")]
    hashes = rippling.parse_content_hashes(main, routes + [("/missing", "333")])
    assert hashes == {route_a: "hashA", route_b: "hashB"}
    loaders = rippling.parse_webpack_loaders(main, {**hashes, "/none": "absent"})
    assert loaders[route_a] == {
        "content_hash": "hashA",
        "all_chunks": ["10", "20"],
        "module_id": "30",
        "source": "@site/docs/list.api.mdx",
    }
    assert loaders[route_b]["all_chunks"] == ["40"]

    runtime = (
        'prefix,t.u=e=>"assets/js/"+({10:"page",20:"shared"}[e]||e)+"."+'
        '({10:"abc",30:"def"}[e]||e)+".js",t.x=1'
    )
    assert rippling.parse_chunk_filename_map(runtime) == {
        "10": "assets/js/page.abc.js",
        "20": "assets/js/shared.20.js",
        "30": "assets/js/30.def.js",
    }
    assert rippling.parse_chunk_filename_map("no runtime") == {}
    assert rippling.parse_chunk_filename_map('t.u=e=>"assets/js/"+({1:"x"}[e])') == {}


def test_frontmatter_and_jsx_conversion_cover_content_tags_and_fallbacks() -> None:
    separator = "x" * 160
    tagged = separator.join(
        [
            's.h1,{children:"Heading"}',
            's.p,{children:"Paragraph"}',
            's.li,{children:"Item"}',
            's.th,{children:"Name"}',
            's.th,{children:"Value"}',
            's.td,{children:"One"}',
            's.td,{children:"Two"}',
            's.p,{children:"After"}',
            's.code,{children:"code"}',
            's.strong,{children:"bold"}',
            's.em,{children:"em"}',
            's.pre,{children:"block"}',
            'x,{children:"plain\\ntext"}',
        ]
    )
    chunk = (
        'title:"Guide",description:"A guide",sidebar_label:"Start",function c(e){' + tagged + "}function d("
    )
    assert rippling.extract_frontmatter(chunk) == {
        "title": "Guide",
        "description": "A guide",
        "sidebar_label": "Start",
    }
    markdown = rippling.jsx_to_markdown(chunk)
    assert "# Heading" in markdown
    assert "Paragraph" in markdown and "- Item" in markdown
    assert "| Name | Value |" in markdown and "| One | Two |" in markdown
    assert "`code`" in markdown and "**bold**" in markdown and "*em*" in markdown
    assert "```" in markdown and "plain" in markdown
    assert rippling.jsx_to_markdown("no function") is None
    assert rippling.build_guide_markdown("no function", {"title": "Fallback", "description": "Short"}) == (
        "# Fallback\n\nShort\n"
    )
    built = rippling.build_guide_markdown(chunk, {"title": "Different"})
    assert built.startswith("# Different\n")


def test_write_file_covers_add_update_unchanged_and_dry_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rippling, "DOCS_DIR", str(tmp_path))
    args = Namespace(dry_run=False, verbose=True)
    cache: dict = {}
    new_cache: dict = {}
    counters = {"added": 0, "updated": 0, "unchanged": 0}
    path = tmp_path / "workers" / "page.md"
    rippling.write_file(str(path), "one", "key", cache, new_cache, counters, args, "workers/page.md")
    assert counters["added"] == 1

    cache = new_cache
    new_cache = {}
    rippling.write_file(str(path), "one", "key", cache, new_cache, counters, args, "workers/page.md")
    assert counters["unchanged"] == 1
    rippling.write_file(str(path), "two", "key", cache, new_cache, counters, args, "workers/page.md")
    assert counters["updated"] == 1 and path.read_text() == "two"

    dry_path = tmp_path / "dry" / "page.md"
    rippling.write_file(
        str(dry_path),
        "dry",
        "dry-key",
        cache,
        new_cache,
        counters,
        Namespace(dry_run=True, verbose=False),
        "dry/page.md",
    )
    assert not dry_path.exists()


def test_route_and_readme_helpers_are_deterministic() -> None:
    assert rippling.categorize_route("/documentation/rest-api/workers/list") == "workers"
    readme = rippling.build_category_readme("workers", [("List", "list.md", "All workers")])
    assert "[List](./list.md) -- All workers" in readme
    assert "[Workers](./workers/) (1 pages)" in rippling.build_top_readme({"workers": [object()]})


def test_sync_runs_offline_and_reuses_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(rippling, "DOCS_DIR", str(docs))
    monkeypatch.setattr(rippling, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(rippling, "extract_js_bundle_urls", lambda _html: ("main", "runtime"))
    monkeypatch.setattr(rippling, "parse_route_mappings", lambda _js: {"route": "source"})
    monkeypatch.setattr(rippling, "parse_content_hashes", lambda *_args: {"hash": "content"})
    monkeypatch.setattr(
        rippling,
        "parse_webpack_loaders",
        lambda *_args: {
            "/documentation/rest-api/workers/guide": {"source": "guide.mdx", "all_chunks": ["1"]},
            "/documentation/rest-api/workers/create": {"source": "create.api.mdx", "all_chunks": ["2"]},
            "/documentation/rest-api/workers/missing": {"source": "missing.mdx", "all_chunks": []},
            "/documentation/rest-api/workers/html": {"source": "html.mdx", "all_chunks": ["3"]},
            "/documentation/rest-api/workers/bad": {"source": "bad.api.mdx", "all_chunks": ["4"]},
        },
    )
    monkeypatch.setattr(
        rippling,
        "parse_chunk_filename_map",
        lambda _js: {
            "1": "assets/guide.js",
            "2": "assets/create.js",
            "3": "assets/html.js",
            "4": "assets/bad.js",
        },
    )
    responses = {
        f"{rippling.BASE_URL}/documentation/rest-api": "landing",
        "main": "main-js",
        "runtime": "runtime-js",
        f"{rippling.BASE_URL}/assets/guide.js": "guide chunk",
        f"{rippling.BASE_URL}/assets/create.js": "api chunk",
        f"{rippling.BASE_URL}/assets/html.js": "<html>missing</html>",
        f"{rippling.BASE_URL}/assets/bad.js": "bad api chunk",
    }
    monkeypatch.setattr(rippling, "fetch_url", lambda url, *_args, **_kwargs: responses.get(url))
    monkeypatch.setattr(
        rippling,
        "extract_frontmatter",
        lambda chunk: {"title": chunk.title(), "description": "Description"},
    )
    monkeypatch.setattr(
        rippling,
        "extract_api_spec_from_chunk",
        lambda chunk: (
            {"summary": "Create", "method": "post", "path": "/workers", "responses": {}}
            if chunk == "api chunk"
            else None
        ),
    )
    monkeypatch.setattr(rippling, "build_guide_markdown", lambda chunk, _front: f"# {chunk}\n")
    (docs / "workers").mkdir(parents=True)
    for name in ("missing", "html", "bad"):
        (docs / "workers" / f"{name}.md").write_text(f"# Cached {name}\n")
    (docs / "stale").mkdir()
    (docs / "stale" / "old.md").write_text("remove")
    rippling.save_cache(
        {
            **{
                f"cat:workers:{name}.md": {
                    "sha256": name,
                    "title": f"Cached {name}",
                    "description": "Preserved",
                }
                for name in ("missing", "html", "bad")
            },
            "cat:stale:old.md": {"sha256": "stale"},
        }
    )
    args = Namespace(force=False, dry_run=False, verbose=True)

    rippling.sync(args)
    assert (docs / "workers" / "guide.md").exists()
    assert (docs / "workers" / "create.md").exists()
    assert (docs / "workers" / "missing.md").read_text() == "# Cached missing\n"
    assert not (docs / "stale" / "old.md").exists()
    rippling.sync(args)


@pytest.mark.parametrize(
    ("responses", "message"),
    [({}, "landing"), ({"landing": "page"}, "bundles"), ({"landing": "page", "bundles": True}, "download")],
)
def test_sync_rejects_missing_required_sources(
    responses: dict, message: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        rippling,
        "fetch_url",
        lambda url, *_args, **_kwargs: (
            responses.get("landing") if url.endswith("rest-api") else responses.get("bundle")
        ),
    )
    if message != "landing":
        monkeypatch.setattr(
            rippling,
            "extract_js_bundle_urls",
            lambda _html: ("main", "runtime") if responses.get("bundles") else (None, None),
        )
    with pytest.raises(SystemExit):
        rippling.sync(Namespace(force=True, dry_run=True, verbose=False))


def test_cli_delegates_to_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Namespace] = []
    monkeypatch.setattr(rippling, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    rippling.main()
    assert called[0].dry_run and called[0].force and called[0].verbose
