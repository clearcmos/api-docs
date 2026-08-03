from __future__ import annotations

import json
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.support import load_fetcher

spotify = load_fetcher("spotify")


def test_compiled_mdx_parser_and_renderer_preserve_structure() -> None:
    compiled = (
        'function _createMdxContent(){return _jsxs("div",{children:['
        '_jsx("h1",{children:"Guide"}),_jsx("p",{children:["Use ",_jsx("code",{children:"token"})]})]})}'
    )
    root = spotify.parse_compiled_mdx(compiled)
    markdown = spotify.MdxRenderer(lambda target: target, {}).document(root)

    assert "# Guide" in markdown
    assert "Use `token`" in markdown


def test_schema_helpers_flatten_refs_and_server_variables() -> None:
    spec = {
        "servers": [{"url": "https://{region}.example.test", "variables": {"region": {"default": "ca"}}}],
        "components": {
            "schemas": {
                "Base": {"type": "object", "required": ["id"], "properties": {"id": {"type": "string"}}}
            }
        },
    }
    schema = {
        "allOf": [
            {"$ref": "#/components/schemas/Base"},
            {"type": "object", "properties": {"name": {"type": "string"}}},
        ]
    }

    flattened = spotify.flatten_all_of(schema, spec)

    assert set(flattened["properties"]) == {"id", "name"}
    assert flattened["required"] == ["id"]
    assert spotify.resolve_server_url(spec) == "https://ca.example.test"


def test_endpoint_collection_and_paths_are_deterministic() -> None:
    spec = {
        "paths": {
            "/albums/{id}": {
                "get": {"operationId": "get-an-album", "summary": "Get album", "tags": ["Albums"]}
            }
        }
    }
    by_tag, groups, total = spotify.collect_endpoints(spec)

    assert total == 1
    assert by_tag["Albums"][0]["key"] == "reference/albums/get-an-album.md"
    assert groups["Albums"]["count"] == 1
    assert spotify.guide_output("/documentation/web-api/concepts/scopes") == "concepts/scopes.md"
    assert spotify.is_reference_path("/documentation/web-api/reference/get-an-album") is True


def test_writer_adds_updates_and_keeps_cached_output(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spotify, "DOCS_DIR", str(tmp_path))
    args = Namespace(force=False, dry_run=False, verbose=False)
    writer = spotify.Writer({}, args)

    writer.write("guides/page.md", "first")
    assert writer.added == 1
    assert (tmp_path / "guides" / "page.md").read_text() == "first"

    cached = writer.new_cache
    second = spotify.Writer(cached, args)
    second.write("guides/page.md", "first")
    assert second.unchanged == 1
    second.keep("guides/page.md")
    assert second.new_cache["guides/page.md"]["sha256"] == spotify.sha256("first")


def test_js_expression_parser_handles_literals_elements_and_errors() -> None:
    value = spotify.JsExpr(
        r"""{text:"line\n\u263a\x21", unicode:"\u{1f600}", nums:[1,-2.5e2], yes:true,
        no:false, nil:null, ident:thing, el:_jsx(_components.p,{children:["Hi",undefined]})}"""
    ).value()

    assert value["text"] == "line\n☺!"
    assert value["unicode"] == "😀"
    assert value["nums"] == [1, -250.0]
    assert value["yes"] is True and value["no"] is False and value["nil"] is None
    assert value["ident"].name == "thing"
    assert value["el"].tag == "p" and value["el"].children == ["Hi", None]
    fragment = spotify.JsExpr("_jsxs(_Fragment,{children:[]})").value()
    assert fragment.tag == "#fragment"

    for source in ('"unterminated', "[1 2]", "{a 1}", "?", "1x"):
        with pytest.raises(spotify.MdxParseError):
            parser = spotify.JsExpr(source)
            parser.value()
            parser.ws()
            if parser.i != len(source):
                parser.fail("trailing input")
    with pytest.raises(spotify.MdxParseError):
        spotify.parse_compiled_mdx("const value = 1")


def test_renderer_covers_block_inline_table_code_and_vendor_components() -> None:
    el = spotify.El
    code_files = [
        {
            "name": "example.sh",
            "code": {
                "lang": "text",
                "lines": [
                    {"tokens": [{"content": "echo "}, {"content": "ok"}]},
                    {"tokens": [{"content": "done"}]},
                ],
            },
        },
        "skip",
        {"code": {"lang": "python", "lines": [{"tokens": []}]}},
    ]
    inline = [
        el("a", {"href": "/local"}, ["Link"]),
        " ",
        el("code", {}, ["a`b"]),
        el("strong", {}, [" bold "]),
        el("em", {}, ["em"]),
        el("del", {}, ["gone"]),
        el("br", {}, []),
        el("img", {"src": "/img", "alt": "Logo"}, []),
        el("input", {"type": "checkbox", "checked": True}, []),
        el("span", {}, [" tail"]),
        el("style", {}, ["hidden"]),
    ]
    root = el(
        "#fragment",
        {},
        [
            el("style", {}, ["drop"]),
            el("h2", {}, ["Heading"]),
            el("p", {}, inline),
            el(
                "ul",
                {},
                [
                    el("li", {}, ["[X] task", el("p", {}, ["details"])]),
                    el("li", {}, []),
                ],
            ),
            el("ol", {}, [el("li", {}, ["first"]), el("li", {}, ["second"])]),
            el(
                "table",
                {},
                [
                    el(
                        "thead",
                        {},
                        [el("tr", {}, [el("th", {}, ["Name"]), el("th", {}, ["A|B"])])],
                    ),
                    el("tbody", {}, [el("tr", {}, [el("td", {}, ["one"])])]),
                ],
            ),
            el("blockquote", {}, [el("p", {}, ["Quote"])]),
            el("hr", {}, []),
            el("pre", {}, [el("code", {"className": "language-json"}, ['{"a":1}'])]),
            el("details", {}, [el("summary", {}, ["More"]), el("p", {}, ["Body"])]),
            el("Banner", {"color": "danger"}, [el("p", {}, ["Careful"])]),
            el("CH.Code", {"files": code_files}, []),
            el(
                "CH.Section",
                {"files": code_files},
                [el("p", {}, ["Section"]), el("CH.SectionCode", {}, [])],
            ),
            el("UnknownWidget", {}, [el("p", {}, ["Fallback"])]),
        ],
    )
    unknown: dict[str, int] = {}
    rendered = spotify.MdxRenderer(
        lambda target: {"/local": "./local.md", "/img": "./img"}[target], unknown
    ).document(root)

    assert "## Heading" in rendered
    assert "[Link](./local.md)" in rendered and "``a`b``" in rendered
    assert "- [x] task" in rendered and "1. first" in rendered
    assert "| Name | A\\|B |" in rendered
    assert "> Quote" in rendered and "> [!CAUTION]" in rendered
    assert "```json" in rendered and "**example.sh**" in rendered
    assert "Fallback" in rendered and unknown == {"UnknownWidget": 1}


def test_link_text_and_schema_formatters_cover_nested_shapes() -> None:
    local = {
        "/documentation/web-api/concepts/scopes": "concepts/scopes.md",
        spotify.REFERENCE_PATH: "reference/README.md",
    }
    resolve = spotify.make_resolver("concepts", local)
    assert resolve(None) == ""
    assert resolve("#here") == "#here"
    assert resolve("relative.md") == "relative.md"
    assert resolve("/documentation/web-api/concepts/scopes#x") == "./scopes.md#x"
    assert resolve("/outside path") == "https://developer.spotify.com/outside%20path"
    assert (
        spotify.clean_spec_text(
            '<p>See <a href="/documentation/web-api/concepts/scopes">Scopes</a><br>Next | row</p>',
            resolve,
            inline=True,
        )
        == "See [Scopes](./scopes.md) Next \\| row"
    )

    spec = {
        "components": {
            "schemas": {
                "Node": {
                    "type": "object",
                    "required": ["id"],
                    "properties": {
                        "id": {"type": "string", "format": "uri", "description": "Identifier"},
                        "children": {"type": "array", "items": {"$ref": "#/components/schemas/Node"}},
                        "meta": {"type": "object", "additionalProperties": {"type": "integer"}},
                    },
                },
                "PolicyList": [{"$ref": "#/components/policies/PolicyOne"}, "PolicyTwo"],
            },
            "parameters": {"Limit": {"name": "limit", "in": "query", "schema": {"type": "integer"}}},
        }
    }
    label, lines = spotify.schema_parts({"$ref": "#/components/schemas/Node"}, spec, resolve)
    assert label == "object" and any("children" in line for line in lines)
    assert spotify.schema_parts({"$ref": "#/missing"}, spec, resolve)[0] == "`missing`"
    assert "... and 1 more" in spotify.schema_parts({"anyOf": [{"type": "string"}] * 6}, spec, resolve)[0]
    assert spotify.schema_parts({"type": "string", "enum": list(range(11))}, spec, resolve)[0].endswith(
        "... (11 total)"
    )
    assert spotify.schema_parts(
        {"type": "object", "properties": {"x": {"type": "string"}}}, spec, resolve, 2
    )[0] == ("object (1 properties)")
    assert spotify.format_parameters([{"$ref": "#/components/parameters/Limit"}], spec, resolve)
    assert spotify.format_request_body(
        {"required": True, "content": {"application/json": {"schema": {"type": "string"}, "example": "x"}}},
        spec,
        resolve,
    )
    assert spotify.format_responses(
        {"200": {"description": "OK", "content": {"application/json": {"schema": {"type": "string"}}}}},
        spec,
        resolve,
    )


def _compiled_page(title: str, link: str | None = None) -> str:
    children = [f'_jsx("h1",{{children:"{title}"}})', '_jsx("p",{children:"Body"})']
    if link:
        children.append(f'_jsx("a",{{href: "{link}",children:"Next"}})')
    return 'function _createMdxContent(){return _jsxs("div",{children:[' + ",".join(children) + "]})}"


def test_sync_runs_end_to_end_then_uses_conditional_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(spotify, "DOCS_DIR", str(docs))
    monkeypatch.setattr(spotify, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(spotify, "SPEC_FILE", str(tmp_path / "openapi.yaml"))
    landing = (
        '"buildId":"build-1" href="/documentation/web-api" '
        'href="/documentation/web-api/concepts/scopes" '
        'href="/documentation/web-api/reference/get-track"'
    )
    page_sources = {
        spotify.ENTRY_PATH: _compiled_page("Overview", "/documentation/web-api/howtos/next"),
        "/documentation/web-api/concepts/scopes": _compiled_page("Scopes"),
        "/documentation/web-api/howtos/next": _compiled_page("Next"),
    }
    policy_refs = {"PolicyOne": {"title": "Policy one", "description": "Use carefully", "url": "/policy one"}}
    spec = {
        "openapi": "3.0.3",
        "info": {"version": "1.0"},
        "servers": [{"url": "https://api.spotify.test/v1"}],
        "tags": [{"name": "Tracks", "description": "Track operations"}],
        "components": {
            "securitySchemes": {
                "oauth_2_0": {"flows": {"authorizationCode": {"scopes": {"track-read": "Read"}}}}
            }
        },
        "paths": {
            "/tracks/{id}": {
                "get": {
                    "operationId": "get-track",
                    "summary": "Get track",
                    "description": "Gets a track.",
                    "deprecated": True,
                    "tags": ["Tracks"],
                    "security": [{"oauth": ["track-read"]}],
                    "parameters": [
                        {"name": "id", "in": "path", "required": True, "schema": {"type": "string"}}
                    ],
                    "requestBody": {
                        "content": {"application/json": {"schema": {"type": "object"}, "example": {"x": 1}}}
                    },
                    "responses": {"200": {"description": "OK"}},
                    "x-spotify-policy-list": ["PolicyOne"],
                }
            }
        },
    }
    raw_spec = __import__("yaml").safe_dump(spec)

    def fake_http(url, etag=None, **_kwargs):
        if url == spotify.ENTRY_URL:
            return 200, landing, {}
        if url == spotify.SPEC_URL:
            return (
                (304, None, {"etag": "spec-v1"})
                if etag
                else (
                    200,
                    raw_spec,
                    {"etag": "spec-v1", "last-modified": "today"},
                )
            )
        for path, compiled in page_sources.items():
            if url == spotify.page_data_url("build-1", path):
                if etag:
                    return 304, None, {"etag": f"etag-{path}"}
                body = json.dumps(
                    {
                        "pageProps": {
                            "pageTitle": path,
                            "source": {
                                "frontmatter": {
                                    "title": path.rsplit("/", 1)[-1] or "Overview",
                                    "description": "Desc",
                                },
                                "compiledSource": compiled,
                            },
                            "policyReferences": policy_refs,
                        }
                    }
                )
                return 200, body, {"etag": f"etag-{path}"}
        raise AssertionError(url)

    monkeypatch.setattr(spotify, "http_get", fake_http)
    (docs / "stale").mkdir(parents=True)
    (docs / "stale" / "old.md").write_text("remove")
    spotify.save_cache({"stale/old.md": {"sha256": "old"}})
    args = Namespace(force=False, dry_run=False, verbose=True)

    assert spotify.sync(args) == 0
    assert (docs / "index.md").exists()
    assert (docs / "howtos" / "next.md").exists()
    assert (docs / "reference" / "tracks" / "get-track.md").exists()
    assert not (docs / "stale" / "old.md").exists()
    assert spotify.sync(args) == 0


def test_sync_preserves_cached_outputs_on_page_conversion_and_spec_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(spotify, "DOCS_DIR", str(docs))
    monkeypatch.setattr(spotify, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(spotify, "SPEC_FILE", str(tmp_path / "openapi.yaml"))
    docs.mkdir()
    (docs / "index.md").write_text("preserve guide")
    (docs / "reference").mkdir()
    (docs / "reference" / "README.md").write_text("preserve spec")
    spotify.save_cache(
        {
            "index.md": {
                "sha256": "keep",
                "title": "Cached",
                "section": "",
                "fetcher_sha": spotify.FETCHER_SHA,
                "etag": "guide-etag",
            },
            spotify.SPEC_KEY: {
                "outputs": ["reference/README.md"],
                "fetcher_sha": spotify.FETCHER_SHA,
                "etag": "spec-etag",
                "groups": {},
                "info": {},
                "scopes": [],
            },
        }
    )
    landing = '"buildId":"build-1" href="/documentation/web-api"'

    def fake_http(url, **_kwargs):
        if url == spotify.ENTRY_URL:
            return 200, landing, {}
        if url == spotify.SPEC_URL:
            return 0, None, {}
        return 200, json.dumps({"pageProps": {"source": {"compiledSource": "invalid"}}}), {"etag": "new"}

    monkeypatch.setattr(spotify, "http_get", fake_http)
    assert spotify.sync(Namespace(force=False, dry_run=False, verbose=True)) == 1
    assert (docs / "index.md").read_text() == "preserve guide"
    assert (docs / "reference" / "README.md").read_text() == "preserve spec"


def test_main_exits_with_sync_status(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(spotify, "sync", lambda _args: 7)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    with pytest.raises(SystemExit) as exc:
        spotify.main()
    assert exc.value.code == 7
