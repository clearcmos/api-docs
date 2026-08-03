import argparse
import gzip
import json
from urllib.error import HTTPError, URLError

import pytest

from tests.support import load_fetcher

keycloak = load_fetcher("keycloak")


class Response:
    def __init__(self, body: bytes, *, encoding: str = ""):
        self.body = body
        self.headers = {"Content-Encoding": encoding}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self):
        return self.body


def test_transport_cache_discovery_write_and_main_boundaries(tmp_path, monkeypatch):
    xml = b"<urlset><url><loc>https://www.keycloak.org/server/configuration</loc></url></urlset>"
    monkeypatch.setattr(
        keycloak,
        "urlopen",
        lambda *_args, **_kwargs: Response(gzip.compress(xml), encoding="gzip"),
    )
    assert "urlset" in (keycloak.fetch_url("https://example.test") or "")
    assert keycloak.discover_urls() == ["https://www.keycloak.org/server/configuration"]
    missing = HTTPError("https://example.test", 404, "missing", {}, None)
    monkeypatch.setattr(keycloak, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(missing))
    assert keycloak.fetch_url("https://example.test") is None
    error = HTTPError("https://example.test", 500, "error", {}, None)
    monkeypatch.setattr(keycloak, "urlopen", lambda *_a, **_k: (_ for _ in ()).throw(error))
    assert keycloak.fetch_url("https://example.test") is None
    monkeypatch.setattr(
        keycloak,
        "urlopen",
        lambda *_a, **_k: (_ for _ in ()).throw(URLError("down")),
    )
    assert keycloak.fetch_url("https://example.test") is None
    monkeypatch.setattr(keycloak, "fetch_url", lambda _url: None)
    with pytest.raises(SystemExit):
        keycloak.discover_urls()

    cache_file = tmp_path / ".cache.json"
    docs = tmp_path / "docs"
    monkeypatch.setattr(keycloak, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(keycloak, "DOCS_DIR", str(docs))
    assert keycloak.load_cache() == {}
    keycloak.save_cache({"page": {"sha256": "abc"}})
    assert keycloak.load_cache() == {"page": {"sha256": "abc"}}
    output = docs / "page.md"
    keycloak.write_file(str(output), "body", dry_run=False, verbose=True, label="ADD")
    assert output.read_text() == "body"
    dry = docs / "dry.md"
    keycloak.write_file(str(dry), "body", dry_run=True, verbose=False, label="ADD")
    assert not dry.exists()

    called = []
    monkeypatch.setattr(keycloak, "sync", called.append)
    monkeypatch.setattr(keycloak.sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    keycloak.main()
    assert called[0].dry_run and called[0].force and called[0].verbose


def test_keycloak_html_sitemap_and_path_mapping():
    html = """<html><head><title>Server Guide</title></head><body>
<div class="kc-asciidoc" id="guide-body"><h1>Configure</h1>
<div class="admonitionblock note"><table><tr><td class="content"><p>Back up first.</p></td></tr></table></div>
<pre><code class="language-bash">kc.sh start</code></pre></div></body></html>"""
    title, markdown = keycloak.html_to_markdown(html)
    sitemap = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
<url><loc>https://www.keycloak.org/server/configuration</loc></url></urlset>"""

    assert title == "Server Guide"
    assert "# Configure" in markdown
    assert "> **Note:**" in markdown
    assert "kc.sh start" in markdown
    assert keycloak.parse_sitemap(sitemap) == ["https://www.keycloak.org/server/configuration"]
    assert keycloak.is_guide_url("https://www.keycloak.org/server/configuration")
    assert keycloak.guide_path("https://www.keycloak.org/server/configuration").endswith(
        "docs/server/configuration.md"
    )


def test_representative_asciidoc_html_dialect():
    html = """<html><head><title>Server Administration - Keycloak</title></head><body>
<div id="content"><div id="toc"><p>skip toc</p></div><div class="sidebarblock"><p>skip side</p></div>
<h1><a class="anchor" href="#admin"></a>Administration</h1>
<p>Use <strong>bold</strong>, <em>emphasis</em>, <code>kc.sh</code>,
<a href="https://example.test">external</a>, and <img alt="diagram" src="image.png">.</p><br><hr>
<ul><li><p>First</p><ul><li>Nested</li></ul></li></ul>
<ol><li>One</li><li>Two</li></ol><li>Defensive item</li>
<blockquote><p>Quoted text.</p></blockquote>
<pre data-lang="bash"><code class="language-shell">kc.sh start
kc.sh stop</code></pre>
<table><thead><tr><th>Name</th><th>Value</th></tr></thead><tbody>
<tr><td><code>mode</code></td><td>prod | dev</td></tr></tbody></table>
<div class="admonitionblock warning"><table><tr><td class="icon"><i>!</i></td>
<td class="content"><p>Back up first.</p><pre><code>backup</code></pre></td></tr></table></div>
<div class="top-menu-version"><div>skip nested</div></div>
</div><p>outside</p></body></html>"""

    title, markdown = keycloak.html_to_markdown(html)

    assert title == "Server Administration - Keycloak"
    assert "# Administration" in markdown
    assert "**bold**" in markdown and "*emphasis*" in markdown and "`kc.sh`" in markdown
    assert "[external](https://example.test)" in markdown
    assert "![diagram](image.png)" in markdown
    assert "- First" in markdown and "  - Nested" in markdown
    assert "1. One" in markdown and "2. Two" in markdown
    assert "> Quoted text." in markdown
    assert "```shell" in markdown and "kc.sh start" in markdown
    assert "| Name | Value |" in markdown
    assert "prod \\| dev" in markdown
    assert "> **Warning:**" in markdown and "> Back up first." in markdown
    assert "skip toc" not in markdown and "outside" not in markdown

    assert keycloak.html_to_markdown("<html><title>Only title</title></html>") == ("Only title", "")
    assert keycloak.guide_path("https://www.keycloak.org/").endswith("docs/index.md")
    assert not keycloak.is_guide_url("https://example.test/server/configuration")
    assert keycloak.build_page_markdown("", "Body", "https://example.test").startswith("*Source:")
    assert "1 page." in keycloak.build_section_readme("server", [{"title": "A", "rel": "a.md"}])
    assert "Index Pages" in keycloak.build_top_readme(
        {"_root": [{"title": "Root", "rel": "root.md"}], "server": [{"title": "A", "rel": "a.md"}]},
        [{"title": "Manual", "slug": "manual"}],
    )


def test_fetch_failure_is_reported_without_content(monkeypatch):
    monkeypatch.setattr(keycloak, "fetch_url", lambda _url: None)

    url, content = keycloak.fetch_one_page("https://www.keycloak.org/server/configuration")

    assert url == "https://www.keycloak.org/server/configuration"
    assert content is None


def test_sync_end_to_end_cache_hits_failures_and_removal(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    stale = docs / "stale.md"
    stale.write_text("stale")
    cache_file = tmp_path / ".cache.json"
    cache_file.write_text(json.dumps({"stale.md": {"sha256": "old"}}))
    guide_url = "https://www.keycloak.org/server/configuration"
    manual_url = "https://www.keycloak.org/docs/latest/server_admin/index.html"
    guide_html = (
        '<title>Configuration - Keycloak</title><div id="guide-body"><h1>Configure</h1><p>Guide.</p></div>'
    )
    manual_html = '<title>Server Admin - Keycloak</title><div id="content"><h1>Admin</h1><p>Manual.</p></div>'
    monkeypatch.setattr(keycloak, "DOCS_DIR", str(docs))
    monkeypatch.setattr(keycloak, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(keycloak, "MANUALS", (("server_admin", manual_url),))
    monkeypatch.setattr(keycloak, "discover_urls", lambda: [guide_url])
    content = {guide_url: guide_html, manual_url: manual_html}
    monkeypatch.setattr(keycloak, "fetch_one_page", lambda url: (url, content[url]))
    args = argparse.Namespace(force=False, dry_run=False, verbose=True)

    keycloak.sync(args)
    first_cache = json.loads(cache_file.read_text())
    assert not stale.exists()
    assert (docs / "server" / "configuration.md").exists()
    assert (docs / "server" / "README.md").exists()
    assert (docs / "manuals" / "server_admin.md").exists()
    assert (docs / "manuals" / "README.md").exists()

    keycloak.sync(args)
    assert json.loads(cache_file.read_text()) == first_cache

    monkeypatch.setattr(keycloak, "fetch_one_page", lambda url: (url, None))
    keycloak.sync(args)
    assert (docs / "server" / "configuration.md").exists()
    assert (docs / "manuals" / "server_admin.md").exists()
    preserved = json.loads(cache_file.read_text())
    assert "server/configuration.md" in preserved
    assert "manuals/server_admin.md" in preserved

    monkeypatch.setattr(keycloak, "discover_urls", lambda: [])
    monkeypatch.setattr(keycloak, "MANUALS", ())
    keycloak.sync(argparse.Namespace(force=False, dry_run=True, verbose=False))
    assert (docs / "server" / "configuration.md").exists()
