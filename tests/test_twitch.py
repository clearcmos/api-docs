from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.support import load_fetcher

twitch = load_fetcher("twitch")


def test_paths_links_and_sidebar_are_normalized() -> None:
    assert twitch.normalize_doc_path("/docs/api/get-started/?x=1#top") == "/docs/api/get-started"
    assert twitch.page_slug("/docs/api") == "index"
    assert twitch.page_slug("/docs/api/get-started") == "get-started"
    links = twitch.extract_api_links('<a href="/docs/api/get-started">Start</a><a href="/outside">No</a>')
    assert links == {"/docs/api/get-started"}
    sidebar = twitch.parse_sidebar(
        '<dt><a href="/docs/api/">Twitch API</a></dt><dd>'
        '<a class="sub-page" href="/docs/api/guide">Guide</a></dd></dl>'
    )
    assert ("/docs/api/guide", "Guide") in sidebar


def test_fragment_converter_handles_code_lists_and_rewritten_links() -> None:
    fragment = """
    <h2>Example</h2><p>Read <a href="/docs/api/reference#get-users">users</a>.</p>
    <ul><li>First</li><li>Second</li></ul>
    <figure class="highlight"><pre><code data-lang="json">{"ok": true}</code></pre></figure>
    """
    rewrite = twitch.make_rewriter(
        "guide.md",
        "/docs/api/guide",
        {"/docs/api/guide": "guide.md", "/docs/api/reference": "reference/README.md"},
        {"get-users": "reference/users/get-users.md"},
        False,
    )
    markdown = twitch.convert_fragment(fragment, rewrite)

    assert "## Example" in markdown
    assert "[users](reference/users/get-users.md)" in markdown
    assert "- First" in markdown
    assert "```json" in markdown


def test_reference_parser_splits_index_and_endpoints() -> None:
    html = """
    <section class="doc-content">
      <table><tr><td>Users</td><td><a href="#get-users">Get Users</a></td><td>List</td></tr></table>
    </section>
    <section class="doc-content">
      <section class="left-docs"><h2 id="get-users">Get Users</h2><p>Lists users.</p></section>
      <section class="right-code"><pre><code data-lang="json">{}</code></pre></section>
    </section>
    """
    index_rows, endpoints = twitch.parse_reference(html)

    assert index_rows[0][:3] == ("Users", "get-users", "Get Users")
    assert endpoints[0]["anchor"] == "get-users"
    assert "Lists users" in endpoints[0]["left_html"]


def test_readme_builders_report_counts() -> None:
    entries = [{"title": "Get Users", "filename": "get-users.md", "description": "List"}]
    assert "[Get Users](./get-users.md)" in twitch.build_resource_readme("Users", entries)
    resources = [("Users", "users", entries)]
    assert "1 endpoints" in twitch.build_reference_readme(resources)


def test_sync_generates_reference_and_guide_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(twitch, "DOCS_DIR", str(docs))
    monkeypatch.setattr(twitch, "CACHE_FILE", str(tmp_path / "cache.json"))
    pages = {
        twitch.API_ROOT: '<a class="sub-page" href="/docs/api/guide">Guide</a>',
        "/docs/api/guide": "guide page",
        twitch.REFERENCE_PATH: "reference page",
    }
    monkeypatch.setattr(twitch, "crawl_pages", lambda _verbose: pages)
    rows = [("Users", f"get-users-{i}", f"Get Users {i}", "List users") for i in range(100)]
    endpoints = [
        {
            "anchor": anchor,
            "title": title,
            "left_html": f"<p>{desc}</p>",
            "right_html": "<pre><code>{}</code></pre>",
        }
        for _resource, anchor, title, desc in rows
    ]
    monkeypatch.setattr(twitch, "parse_reference", lambda _html: (rows, endpoints))
    monkeypatch.setattr(twitch, "extract_text_content", lambda _html: "<h1>Guide</h1><p>Body</p>")
    monkeypatch.setattr(twitch, "page_title", lambda *_args: "Guide")
    args = Namespace(force=False, dry_run=False, verbose=True)

    twitch.sync(args)
    assert (docs / "guide.md").exists()
    assert (docs / "reference" / "users" / "get-users-0.md").exists()
    twitch.sync(args)


def test_sync_rejects_bad_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(twitch, "crawl_pages", lambda _verbose: {})
    with pytest.raises(SystemExit):
        twitch.sync(Namespace(force=True, dry_run=True, verbose=False))

    monkeypatch.setattr(twitch, "crawl_pages", lambda _verbose: {twitch.REFERENCE_PATH: "page"})
    monkeypatch.setattr(twitch, "parse_reference", lambda _html: ([], []))
    with pytest.raises(SystemExit):
        twitch.sync(Namespace(force=True, dry_run=True, verbose=False))


def test_cli_delegates_to_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Namespace] = []
    monkeypatch.setattr(twitch, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    twitch.main()
    assert called[0].dry_run and called[0].force and called[0].verbose


def test_converter_and_rewriter_cover_rich_reference_markup() -> None:
    fragment = """
    <script>ignored</script><h1><span>Rich page</span></h1><hr>
    <blockquote><p><strong>Bold</strong> and <em>italic</em> with
      <span class="pill">pill</span>, <code><b>literal</b></code>, and<br>line.</p></blockquote>
    <ol><li>First<ul><li>Nested</li></ul></li><li>Second</li></ol>
    <div class="language-python highlighter-rouge"><pre><code>print('```')</code></pre></div>
    <p><a href="#get-users">known</a> <a href="#unknown">unknown</a>
       <a href="//cdn.example/a">cdn</a> <a href="mailto:x@example.test">mail</a>
       <a href="relative">relative</a></p>
    <img src="/image.png" alt="Diagram">
    <table><tr><th>Name</th><th>Value</th></tr>
      <tr><td>&nbsp;&nbsp;child<br>next</td><td>A | B<ul><li>x</li></ul></td></tr></table>
    """
    rewrite = twitch.make_rewriter(
        "reference/users/get-users.md",
        twitch.REFERENCE_PATH,
        {twitch.REFERENCE_PATH: "reference/README.md", twitch.API_ROOT: "README.md"},
        {"get-users": "reference/users/get-users.md"},
        True,
    )
    markdown = twitch.convert_fragment(fragment, rewrite, heading_offset=-1)
    assert markdown.startswith("# Rich page")
    assert "> **Bold**" in markdown
    assert "1. First" in markdown and "- Nested" in markdown
    assert "````python" in markdown
    assert "| Name | Value |" in markdown
    assert "A \\| B" in markdown
    assert "![Diagram]" in markdown
    assert rewrite("") == ""
    assert rewrite("#get-users").endswith("get-users.md")
    assert "dev.twitch.tv" in rewrite("#unknown")
    assert rewrite("//cdn.example/a").startswith("https://")
    assert rewrite("mailto:x@example.test").startswith("mailto:")
    assert "relative" in rewrite("relative")
    assert "README.md" in rewrite(twitch.API_ROOT)


def test_crawl_pages_follows_links_and_aborts_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    pages = {
        twitch.page_source_url(twitch.API_ROOT): '<div class="main"><a href="/docs/api/guide">G</a></div>',
        twitch.page_source_url("/docs/api/guide"): '<div class="main">Guide</div>',
    }
    monkeypatch.setattr(twitch, "fetch_url", lambda url: pages.get(url))
    crawled = twitch.crawl_pages(verbose=True)
    assert set(crawled) == {twitch.API_ROOT, "/docs/api/guide"}
    monkeypatch.setattr(twitch, "fetch_url", lambda _url: None)
    with pytest.raises(SystemExit):
        twitch.crawl_pages(verbose=False)
