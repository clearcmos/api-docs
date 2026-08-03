from __future__ import annotations

import sys
from argparse import Namespace
from pathlib import Path

import pytest

from tests.support import load_fetcher

youtube = load_fetcher("youtube")


def test_discovery_extracts_methods_and_derives_resources() -> None:
    html = """
    <div class="devsite-article-body">
      <a href="/youtube/v3/docs/videos/list">List</a>
      <a href="/youtube/v3/docs/videos/insert">Insert</a>
      <a href="/other">Other</a>
    </div>
    """
    paths = youtube.extract_doc_urls(html)
    assert paths == {"/youtube/v3/docs/videos/list", "/youtube/v3/docs/videos/insert"}
    assert youtube.derive_resource_paths(paths) == {"/youtube/v3/docs/videos"}


def test_plan_pages_is_deterministic_and_includes_unlinked_resources(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        youtube,
        "fetch_url",
        lambda _url: (
            '<div class="devsite-article-body"><a href="/youtube/v3/docs/videos/list">List</a></div>'
        ),
    )
    plan = youtube.plan_pages()

    assert (youtube.INDEX_URL, "index.md") in plan
    assert (youtube.SITE + "/youtube/v3/docs/videos/list", "videos/list.md") in plan
    assert (youtube.SITE + "/youtube/v3/docs/errors", "errors.md") in plan
    assert (youtube.SITE + "/youtube/v3/docs/captions", "captions/index.md") in plan


def test_devsite_html_converts_headings_links_code_and_notes() -> None:
    html = """
    <html><head><title>Fallback | Google for Developers</title></head><body>
    <div class="devsite-article-body"><h1>Videos: list</h1>
    <p>Returns <code>video</code> resources.</p>
    <div class="note"><b>Note:</b> Quota applies.</div>
    <pre><code class="language-json">{"items": []}</code></pre>
    </div></body></html>
    """
    markdown = youtube.html_to_markdown(html, "https://developers.google.com/youtube/v3/docs/videos/list")

    assert markdown is not None
    assert markdown.startswith("# Videos: list")
    assert "`video`" in markdown
    assert "Quota applies" in markdown
    assert "```json" in markdown.lower()


def test_readmes_list_resources_and_methods() -> None:
    resource = youtube.build_resource_readme("videos", ["list", "insert"])
    assert "[insert](./insert.md)" in resource
    top = youtube.build_top_readme({"videos": ["list"]}, ["errors"])
    assert "[errors](./errors.md)" in top
    assert "[videos](./videos/README.md) - list" in top


def test_sync_writes_reuses_preserves_failures_and_removes_stale(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs = tmp_path / "docs"
    monkeypatch.setattr(youtube, "DOCS_DIR", str(docs))
    monkeypatch.setattr(youtube, "CACHE_FILE", str(tmp_path / "cache.json"))
    plan = [
        ("https://example.test/index", "index.md"),
        ("https://example.test/videos/list", "videos/list.md"),
        ("https://example.test/errors", "errors.md"),
    ]
    monkeypatch.setattr(youtube, "plan_pages", lambda: plan)
    content = {
        "index.md": "# Index\n",
        "videos/list.md": "# Videos list\n",
        "errors.md": "# Errors\n",
    }
    monkeypatch.setattr(
        youtube,
        "fetch_page",
        lambda url, rel, _verbose: (url, rel, content.get(rel)),
    )
    args = Namespace(force=False, dry_run=False, verbose=True)

    youtube.sync(args)
    assert (docs / "videos" / "list.md").exists()
    youtube.sync(args)

    content["videos/list.md"] = None
    plan.pop()
    youtube.sync(args)
    assert (docs / "videos" / "list.md").exists()
    assert not (docs / "errors.md").exists()


def test_fetch_page_and_cli_boundaries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(youtube, "fetch_url", lambda _url: None)
    assert youtube.fetch_page("url", "out.md", True) == ("url", "out.md", None)
    monkeypatch.setattr(youtube, "fetch_url", lambda _url: "<html></html>")
    assert youtube.fetch_page("url", "out.md", True) == ("url", "out.md", None)

    called: list[Namespace] = []
    monkeypatch.setattr(youtube, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    youtube.main()
    assert called[0].dry_run and called[0].force and called[0].verbose


def test_devsite_converter_handles_rich_article_structure() -> None:
    html = """
    <html><head><title>Ignored title | Google for Developers</title></head><body>
    <div class="devsite-article-body">
      <div class="devsite-article-meta">skip me</div><script>skip</script>
      <h1>Rich page</h1><hr>
      <aside class="warning"><p><b>Warning:</b> <em>careful</em>.</p></aside>
      <p class="special">Quota impact.</p>
      <p>Use <code><strong>videos.list</strong></code>,
        <a href="/youtube/v3/docs/videos/list">relative</a>,
        <a href="https://example.test">absolute</a>, and <a>plain</a>.
        <img src="/image.png" alt="diagram"></p>
      <ol><li><p>First</p><ul><li>Nested</li></ul></li><li>Second</li></ol>
      <dl><dt>Name</dt><dd>Definition</dd></dl>
      <blockquote><p>Quoted</p></blockquote>
      <devsite-code><pre data-lang="bash"><code class="language-shell">echo ok</code></pre></devsite-code>
      <table><tbody>
        <tr><td colspan="2"><b>Required parameters</b></td></tr>
        <tr><td><code>part</code></td><td>Fields <ul><li>snippet</li></ul></td></tr>
      </tbody></table>
      <table><thead><tr><th>Name</th><th>Type</th><th>Description</th></tr></thead>
        <tbody><tr><td>id</td><td>string</td><td>A | B</td></tr></tbody></table>
    </div></body></html>
    """
    markdown = youtube.html_to_markdown(html, "https://developers.google.com/youtube/v3/docs/videos/list")
    assert markdown is not None
    assert markdown.startswith("# Rich page")
    assert "> **Warning:**" in markdown
    assert "> Quota impact." in markdown
    assert "[`videos.list`]" not in markdown
    assert "1. First" in markdown and "- Nested" in markdown
    assert "**Name**" in markdown and ": Definition" in markdown
    assert "```shell" in markdown
    assert "**Required parameters**" in markdown
    assert "- **`part`**" in markdown
    assert "| Name | Type | Description |" in markdown
    assert "A \\| B" in markdown
    assert "![diagram](https://developers.google.com/image.png)" in markdown
    assert "skip me" not in markdown


def test_devsite_title_fallbacks_and_empty_body() -> None:
    titled = youtube.html_to_markdown(
        "<title>Fallback | Google for Developers</title><div class='devsite-article-body'><p>Body</p></div>",
        "https://example.test/page",
    )
    assert titled is not None and titled.startswith("# Fallback")
    slugged = youtube.html_to_markdown(
        "<div class='devsite-article-body'><p>Body</p></div>", "https://example.test/slug"
    )
    assert slugged is not None and slugged.startswith("# slug")
    assert youtube.html_to_markdown("<html><body>No article</body></html>", "url") is None
