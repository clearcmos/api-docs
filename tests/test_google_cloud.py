import argparse
import gzip
import hashlib
import json

import pytest

from tests.support import load_fetcher

google_cloud = load_fetcher("google-cloud")


class Response:
    def __init__(self, status, body=b"", headers=None):
        self.status = status
        self.body = body
        self.headers = headers or {}

    def read(self):
        return self.body

    def getheader(self, name, default=""):
        return self.headers.get(name, default)


class Connection:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.requests = []
        self.closed = False

    def request(self, method, path, headers):
        self.requests.append((method, path, headers))

    def getresponse(self):
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


def test_http_sitemap_cache_and_main_boundaries(tmp_path, monkeypatch):
    connection = Connection(
        [
            Response(302, headers={"Location": "/redirected?x=1"}),
            Response(200, gzip.compress(b"body"), {"Content-Encoding": "gzip"}),
        ]
    )
    monkeypatch.setattr(google_cloud, "_connection", lambda fresh=False: connection)
    assert google_cloud.http_get("/start") == (200, b"body")
    external = Connection([Response(302, headers={"Location": "https://example.test/out"})])
    monkeypatch.setattr(google_cloud, "_connection", lambda fresh=False: external)
    assert google_cloud.http_get("/start")[1] is None
    permanent = Connection([Response(404, b"missing")])
    monkeypatch.setattr(google_cloud, "_connection", lambda fresh=False: permanent)
    assert google_cloud.http_get("/missing") == (404, None)

    index = b"<sitemapindex><loc>https://docs.cloud.google.com/shard1.xml</loc><loc>https://docs.cloud.google.com/bad.xml</loc></sitemapindex>"
    shard = b"<urlset><url><loc>https://docs.cloud.google.com/run/docs/start</loc><lastmod>2026-01-01</lastmod></url><url><loc>https://example.test/out</loc></url></urlset>"
    replies = {"/sitemap.xml": (200, index), "/shard1.xml": (200, shard), "/bad.xml": (500, None)}
    monkeypatch.setattr(google_cloud, "http_get", lambda path: replies[path])
    pages, complete = google_cloud.fetch_sitemap_urls(verbose=True)
    assert pages == {"/run/docs/start": "2026-01-01"}
    assert not complete
    monkeypatch.setattr(google_cloud, "http_get", lambda _path: (500, None))
    with pytest.raises(SystemExit):
        google_cloud.fetch_sitemap_urls(verbose=False)

    cache_file = tmp_path / ".cache.json"
    monkeypatch.setattr(google_cloud, "CACHE_FILE", str(cache_file))
    assert google_cloud.load_cache() == {}
    google_cloud.save_cache({"run/start.md": {"sha256": "abc"}})
    assert google_cloud.load_cache() == {"run/start.md": {"sha256": "abc"}}
    readme = google_cloud.build_top_readme({"README.md": {}, "run/start.md": {}, "standalone.md": {}}, 2)
    assert "`run/` - 1 pages" in readme and "`standalone/` - 1 pages" in readme

    called = []
    monkeypatch.setattr(google_cloud, "sync", called.append)
    monkeypatch.setattr(
        google_cloud.sys,
        "argv",
        [
            "fetch.py",
            "--dry-run",
            "--force",
            "--verbose",
            "--procs",
            "2",
            "--threads",
            "3",
            "--only",
            "run",
            "--limit",
            "4",
            "--include-sdk-reference",
            "--include-translations",
            "--sitemap-cache",
        ],
    )
    google_cloud.main()
    assert called[0].procs == 2 and called[0].threads == 3 and called[0].include_translations


def test_devsite_conversion_and_path_mapping():
    page = """<html><head><script type="application/ld+json">{"headline": "Cloud Run"}</script></head>
<body><div class="devsite-article-body"><h2>Deploy</h2><aside class="note">Use <code>gcloud</code>.</aside>
<pre syntax="bash">gcloud run deploy</pre></div></body></html>"""

    markdown = google_cloud.html_to_markdown(page, "https://docs.cloud.google.com/run/docs/deploy")

    assert markdown is not None
    assert markdown.startswith("# Cloud Run\n\nSource:")
    assert "## Deploy" in markdown
    assert "gcloud run deploy" in markdown
    assert google_cloud.path_to_rel("/run/docs/deploy/") == "run/docs/deploy.md"


def test_representative_devsite_html_dialect():
    page = """<html><head><script type="application/ld+json">{"headline": "Rich Page"}</script></head><body>
<div class="devsite-article-body"><div class="devsite-banner">skip chrome</div>
<h1>Rich Page</h1><h2>Examples</h2><p>Use <strong>bold</strong>, <em>em</em>,
<code>gcloud</code>, <a href="/run/docs">relative</a>, <a href="//example.test/x">scheme</a>,
and <img alt="diagram" src="/images/a.png">.</p><br><hr>
<aside class="warning"><p>Back up first.</p><ul><li>One</li></ul></aside>
<ul><li>Bullet<ul><li>Nested</li></ul></li></ul><ol><li>First</li><li>Second</li></ol>
<blockquote><p>Quote.</p></blockquote>
<pre syntax="bash"><a href="https://example.test">gcloud</a> run deploy</pre>
<table><thead><tr><th>Name</th><th>Value</th></tr></thead><tbody>
<tr><td><code>mode</code></td><td>prod | dev<br>next</td></tr></tbody></table>
<table><tr><td colspan="2"><strong>Fields</strong></td></tr>
<tr><td>name</td><td><ul><li>Field description</li></ul></td></tr></table>
<span class="material-icons">skip icon</span><ul class="toc"><li>skip toc</li></ul>
<script>skip script</script><devsite-selector><p>Tab body.</p></devsite-selector>
</div><p>outside</p></body></html>"""

    markdown = google_cloud.html_to_markdown(page, "https://docs.cloud.google.com/run/rich")

    assert markdown is not None
    assert markdown.count("# Rich Page") == 1
    assert "**bold**" in markdown and "*em*" in markdown and "`gcloud`" in markdown
    assert "[relative](https://docs.cloud.google.com/run/docs)" in markdown
    assert "![diagram](https://docs.cloud.google.com/images/a.png)" in markdown
    assert "> Back up first." in markdown
    assert "- Bullet" in markdown and "  - Nested" in markdown
    assert "1. First" in markdown
    assert "Quote." in markdown
    assert "```bash" in markdown and "gcloud run deploy" in markdown
    assert "| Name | Value |" in markdown and "prod \\| dev" in markdown
    assert "**Fields**" in markdown and "- **name**" in markdown
    assert "skip chrome" not in markdown and "outside" not in markdown
    assert google_cloud.html_to_markdown("<title>No body</title>", "https://example.test") is None
    assert google_cloud.extract_title("<title>A &amp; B | Google Cloud</title>") == "A & B"
    assert google_cloud.extract_title("") == ""


def test_plan_filters_sdk_translations_and_duplicates(monkeypatch):
    pages = {
        "/run/docs/start": "2026-01-01",
        "/python/docs/reference/widget": "2026-01-01",
        "/run/docs/start?hl=fr": "2026-01-01",
    }
    monkeypatch.setattr(google_cloud, "fetch_sitemap_urls", lambda _verbose: (pages, True))
    args = argparse.Namespace(
        sitemap_cache=False,
        verbose=False,
        only=None,
        include_translations=False,
        include_sdk_reference=False,
        limit=None,
    )

    plan, complete = google_cloud.plan_pages(args)

    assert plan == [("/run/docs/start", "run/docs/start.md", "2026-01-01")]
    assert complete


def test_plan_snapshot_translations_only_and_limit(tmp_path, monkeypatch):
    snapshot = tmp_path / "sitemap.json"
    snapshot.write_text(
        json.dumps(
            {
                "/run/docs/start": "1",
                "/run/docs/start?hl=fr": "2",
                "/compute/docs/start": "3",
            }
        )
    )
    monkeypatch.setattr(google_cloud, "SITEMAP_SNAPSHOT", str(snapshot))
    args = argparse.Namespace(
        sitemap_cache=True,
        verbose=False,
        only="run",
        include_translations=True,
        include_sdk_reference=True,
        limit=1,
    )
    plan, complete = google_cloud.plan_pages(args)
    assert plan == [("/run/docs/start", "run/docs/start.md", "1")]
    assert complete


def test_process_one_uses_existing_output_on_hash_match(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    output = docs / "run" / "index.md"
    output.parent.mkdir(parents=True)
    page = '<title>Run | Google Cloud</title><div class="devsite-article-body"><p>Body.</p></div>'
    markdown = google_cloud.html_to_markdown(page, "https://docs.cloud.google.com/run")
    assert markdown is not None
    output.write_text(markdown)
    monkeypatch.setattr(google_cloud, "DOCS_DIR", str(docs))
    monkeypatch.setattr(google_cloud, "http_get", lambda _path: (200, page.encode()))

    digest = hashlib.sha256(markdown.encode()).hexdigest()
    result = google_cloud._process_one(("/run", "run/index.md", "2026-01-01", digest))

    assert result[0] == "unchanged"


class Future:
    def __init__(self, value):
        self.value = value

    def result(self):
        return self.value


class Executor:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def submit(self, fn, *args):
        return Future(fn(*args))


def test_worker_statuses_batch_and_sync_orchestration(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    monkeypatch.setattr(google_cloud, "DOCS_DIR", str(docs))
    page = '<title>Page</title><div class="devsite-article-body"><p>Body.</p></div>'
    monkeypatch.setattr(
        google_cloud,
        "http_get",
        lambda path: (404, None) if path == "/404" else (200, page.encode()),
    )
    assert google_cloud._process_one(("/404", "404.md", "1", None))[0] == "http404"
    assert google_cloud._process_one(("/empty", "empty.md", "1", None))[0] == "added"
    monkeypatch.setattr(google_cloud, "html_to_markdown", lambda _html, _url: None)
    assert google_cloud._process_one(("/empty", "empty.md", "1", None))[0] == "empty"
    google_cloud._worker_pool = None
    monkeypatch.setattr(google_cloud, "_process_one", lambda job: ("added", job[1], "sha", job[2], job[0]))
    assert google_cloud._process_batch([("/a", "a.md", "1", None)])[0][0] == "added"
    google_cloud._worker_pool.shutdown()
    google_cloud._worker_pool = None

    cache_file = tmp_path / ".cache.json"
    stale_file = docs / "old" / "stale.md"
    stale_file.parent.mkdir()
    stale_file.write_text("stale")
    unchanged_file = docs / "same.md"
    unchanged_file.write_text("same")
    cache_file.write_text(
        json.dumps(
            {
                "old/stale.md": {"sha256": "old", "lastmod": "0"},
                "same.md": {"sha256": "same", "lastmod": "1"},
                "changed.md": {"sha256": "old", "lastmod": "0"},
            }
        )
    )
    monkeypatch.setattr(google_cloud, "CACHE_FILE", str(cache_file))
    plan = [
        ("/new", "new.md", "1"),
        ("/changed", "changed.md", "1"),
        ("/same", "same.md", "1"),
        ("/gone", "gone.md", "1"),
    ]
    monkeypatch.setattr(google_cloud, "plan_pages", lambda _args: (plan, True))
    calls = {}

    def process(jobs):
        results = []
        for path, rel, lastmod, old_sha in jobs:
            calls[rel] = calls.get(rel, 0) + 1
            if rel == "gone.md":
                results.append(("http404", rel, None, lastmod, path))
            elif rel == "changed.md" and calls[rel] == 1:
                results.append(("failed", rel, None, lastmod, path))
            else:
                results.append(("added" if old_sha is None else "updated", rel, "newsha", lastmod, path))
        return results

    monkeypatch.setattr(google_cloud, "_process_batch", process)
    monkeypatch.setattr(google_cloud, "ProcessPoolExecutor", Executor)
    monkeypatch.setattr(google_cloud, "as_completed", lambda futures: futures)
    args = argparse.Namespace(
        force=False,
        dry_run=False,
        verbose=True,
        procs=1,
        threads=1,
        only="",
        limit=0,
        include_sdk_reference=False,
        include_translations=False,
    )
    google_cloud.sync(args)
    saved = json.loads(cache_file.read_text())
    assert not stale_file.exists()
    assert saved["new.md"]["sha256"] == "newsha"
    assert saved["changed.md"]["sha256"] == "newsha"
    assert "gone.md" not in saved
    assert calls["changed.md"] == 2

    google_cloud.sync(argparse.Namespace(**{**vars(args), "dry_run": True, "force": True}))
