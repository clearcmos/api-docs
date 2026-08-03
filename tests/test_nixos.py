import argparse
import gzip
import json
from urllib.error import HTTPError, URLError

import pytest

from tests.support import load_fetcher

nixos = load_fetcher("nixos")


class Response:
    def __init__(self, body: bytes, headers=None):
        self.body = body
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def test_transport_cache_write_helpers_and_main(tmp_path, monkeypatch):
    body = gzip.compress(b"manual")
    monkeypatch.setattr(
        nixos,
        "urlopen",
        lambda *_args, **_kwargs: Response(body, {"Content-Encoding": "gzip", "ETag": '"v1"'}),
    )
    assert nixos.fetch_url("https://example.test", etag='"old"') == ("manual", '"v1"', False)
    error = HTTPError("https://example.test", 500, "error", {}, None)
    monkeypatch.setattr(nixos, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error))
    assert nixos.fetch_url("https://example.test") == (None, None, False)
    monkeypatch.setattr(
        nixos,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(URLError("down")),
    )
    assert nixos.fetch_url("https://example.test") == (None, None, False)

    cache_file = tmp_path / ".cache.json"
    docs = tmp_path / "docs"
    monkeypatch.setattr(nixos, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(nixos, "DOCS_DIR", str(docs))
    assert nixos.load_cache() == {}
    nixos.save_cache({"page": {"sha256": "abc"}})
    assert nixos.load_cache() == {"page": {"sha256": "abc"}}
    output = docs / "page.md"
    nixos.write_file(str(output), "body", dry_run=False, verbose=True, label="ADD")
    assert output.read_text() == "body"
    dry = docs / "dry.md"
    nixos.write_file(str(dry), "body", dry_run=True, verbose=False, label="ADD")
    assert not dry.exists()
    assert nixos.slugify("!!!") == "unnamed"
    assert nixos.absolutize("//example.test") == "https://example.test"
    assert nixos.absolutize("/manual") == "https://nixos.org/manual"
    assert nixos.absolutize("#anchor").endswith("/#anchor")
    assert nixos.absolutize("relative") == "relative"

    called = []
    monkeypatch.setattr(nixos, "sync", called.append)
    monkeypatch.setattr(nixos.sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    nixos.main()
    assert called[0].dry_run and called[0].force and called[0].verbose


def test_docbook_option_conversion_and_group_mapping():
    html = """<dl class="variablelist"><dt><a id="opt-services.nginx.enable"></a>
<code class="option">services.nginx.enable</code></dt><dd><p>Enable nginx.</p>
<pre><code class="language-nix">true</code></pre></dd></dl>"""

    options = nixos.parse_options(html)
    markdown = nixos.render_option_md(options[0])

    assert options[0]["name"] == "services.nginx.enable"
    assert "## `services.nginx.enable`" in markdown
    assert "Enable nginx." in markdown
    assert "```nix\ntrue\n```" in markdown
    assert nixos.option_group_path("services.nginx.enable") == ("services", "nginx.md")
    assert nixos.option_group_path("boot.loader.grub.enable") == ("", "boot.md")
    assert nixos.option_group_path("_imports_.module") == ("", "_imports.md")


def test_representative_docbook_renderer_and_slicers():
    fragment = """<div class="chapter"><div class="titlepage"><h2 id="chapter">Chapter</h2></div>
<div class="toc"><p>skip toc</p></div><p>Use <strong>bold</strong>, <em>em</em>,
<code>nix</code>, <code class="filename"><a class="filename" href="/file">file</a></code>,
<a href="#anchor">anchor</a>, and <img alt="diagram" src="/img.png">.</p><br><hr>
<ul><li><p>Bullet</p><ul><li>Nested</li></ul></li></ul><ol><li>First</li></ol>
<blockquote><p>Quote.</p></blockquote>
<dl class="variablelist"><dt>Term</dt><dd><p>Description</p></dd></dl>
<pre class="programlisting nix"><code class="language-nix">{ pkgs, ... }: { }</code></pre>
<table><thead><tr><th>Name</th><th>Value</th></tr></thead><tbody>
<tr><td>mode</td><td>prod | dev</td></tr></tbody></table>
<table class="simplelist"><tr><td>one</td><td>two</td></tr></table>
<div class="warning"><h3>Warning</h3><p>Back up first.</p></div>
<div class="section"><h2 id="nested">Nested</h2><p>Nested body.</p></div></div>"""
    markdown = nixos.html_to_markdown(fragment)

    assert "# Chapter" in markdown and "## Nested" in markdown
    assert "**bold**" in markdown and "*em*" in markdown and "`nix`" in markdown
    assert "[file](https://nixos.org/file)" in markdown
    assert "![diagram](https://nixos.org/img.png)" in markdown
    assert "- Bullet" in markdown and "  - Nested" in markdown
    assert "1. First" in markdown
    assert "**Term**" in markdown and "Description" in markdown
    assert "```nix" in markdown
    assert "| Name | Value |" in markdown and "prod \\| dev" in markdown
    assert "> **Warning**" in markdown and "> Back up first." in markdown
    assert "skip toc" not in markdown

    manual_html = """<div class="preface"><h1 id="preface">Preface</h1><p>Welcome.</p></div>
<div class="part"><h1 id="ch-installation">Installation</h1>
<div class="chapter"><h2 id="installing">Installing</h2><p>Install it.</p></div></div>
<div class="chapter"><h1 id="contributing">Contributing</h1><p>Help.</p></div>"""
    manual = nixos.slice_manual(manual_html)
    assert manual["preface"]["title"] == "Preface"
    assert manual["parts"]["ch-installation"]["chapters"][0]["id"] == "installing"
    assert manual["extras"][0]["id"] == "contributing"

    release_html = """<div class="section"><h2 id="sec-release-25.11">Release 25.11</h2>
<div class="section"><h3 id="highlights">Highlights</h3><p>New.</p></div></div>
<div class="section"><h2 id="other">Other</h2></div>"""
    releases = nixos.slice_release_notes(release_html)
    assert [release["id"] for release in releases] == ["sec-release-25.11"]

    no_heading = nixos.build_chapter_md(
        {"id": "plain", "title": "Plain", "html": "<p>Body.</p>"}, "https://example.test"
    )
    assert no_heading.startswith("# Plain")
    assert "Preface" in nixos.build_manual_readme(manual)
    assert "Installing" in nixos.build_part_readme(manual["parts"]["ch-installation"], "installation")
    assert "Release 25.11" in nixos.build_release_readme(releases)
    groups = {"": {"boot.md": 2}, "services": {"nginx.md": 1}}
    assert "Top-level namespaces" in nixos.build_options_readme(groups)
    assert "services.nginx" in nixos.build_options_subdir_readme("services", groups["services"])


def test_conditional_fetch_treats_304_as_unchanged(monkeypatch):
    error = HTTPError("https://nixos.org/manual", 304, "Not Modified", {"ETag": '"new"'}, None)
    monkeypatch.setattr(nixos, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(error))

    content, etag, unchanged = nixos.fetch_url("https://nixos.org/manual", etag='"old"')

    assert content is None
    assert etag == '"new"'
    assert unchanged


def test_sync_end_to_end_fast_path_failure_removal_and_cache_hits(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    stale = docs / "stale.md"
    stale.write_text("stale")
    cache_file = tmp_path / ".cache.json"
    cache_file.write_text(json.dumps({"stale.md": {"sha256": "old", "label": "Stale"}}))
    manual = """<div class="preface"><h1 id="preface">Preface</h1><p>Welcome.</p></div>
<div class="part"><h1 id="ch-installation">Installation</h1>
<div class="chapter"><h2 id="installing">Installing</h2><p>Install it.</p></div></div>
<div class="chapter"><h1 id="contributing">Contributing</h1><p>Help.</p></div>"""
    releases = '<div class="section"><h2 id="sec-release-25.11">Release 25.11</h2><p>New.</p></div>'
    options = """<dl class="variablelist">
<dt><a id="opt-services.nginx.enable"></a><code>services.nginx.enable</code></dt><dd><p>Enable nginx.</p></dd>
<dt><a id="opt-programs.git.enable"></a><code>programs.git.enable</code></dt><dd><p>Enable git.</p></dd>
<dt><a id="opt-boot.loader.enable"></a><code>boot.loader.enable</code></dt><dd><p>Enable boot.</p></dd>
</dl>"""
    bodies = {"manual": manual, "options": options, "release-notes": releases}
    monkeypatch.setattr(nixos, "DOCS_DIR", str(docs))
    monkeypatch.setattr(nixos, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(
        nixos,
        "fetch_url",
        lambda url, timeout=180, etag=None: (
            bodies[next(slug for slug, page_url in nixos.PAGES.items() if page_url == url)],
            '"v1"',
            False,
        ),
    )
    args = argparse.Namespace(force=False, dry_run=False, verbose=True)

    nixos.sync(args)
    first_cache = json.loads(cache_file.read_text())
    assert not stale.exists()
    assert (docs / "manual" / "preface.md").exists()
    assert (docs / "manual" / "installation" / "installing.md").exists()
    assert (docs / "manual" / "contributing.md").exists()
    assert (docs / "release-notes" / "sec-release-25.11.md").exists()
    assert (docs / "options" / "services" / "nginx.md").exists()
    assert (docs / "options" / "programs" / "git.md").exists()
    assert (docs / "options" / "boot.md").exists()

    nixos.sync(args)
    assert set(json.loads(cache_file.read_text())) == set(first_cache)

    def unchanged(url, timeout=180, etag=None):
        return None, etag, True

    monkeypatch.setattr(nixos, "fetch_url", unchanged)
    nixos.sync(args)

    monkeypatch.setattr(nixos, "fetch_url", lambda *_a, **_k: (None, None, False))
    with pytest.raises(SystemExit):
        nixos.sync(args)

    monkeypatch.setattr(nixos, "fetch_url", unchanged)
    missing_output = docs / "README.md"
    missing_output.unlink()
    calls = {"count": 0}

    def refetch(url, timeout=180, etag=None):
        calls["count"] += 1
        if etag is not None:
            return None, etag, True
        slug = next(slug for slug, page_url in nixos.PAGES.items() if page_url == url)
        return bodies[slug], '"v1"', False

    monkeypatch.setattr(nixos, "fetch_url", refetch)
    nixos.sync(argparse.Namespace(force=False, dry_run=True, verbose=False))
    assert calls["count"] == 6
