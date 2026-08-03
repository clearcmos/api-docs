import argparse
import gzip
import json
from urllib.error import HTTPError, URLError

import pytest

from tests.support import load_fetcher

obs = load_fetcher("obs")


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


def test_transport_discovery_cache_write_and_main_boundaries(tmp_path, monkeypatch):
    raw_index = 'Search.setIndex({"docnames": ["index", "core"]})'
    compressed = gzip.compress(raw_index.encode())
    monkeypatch.setattr(obs, "urlopen", lambda *_args, **_kwargs: Response(compressed, encoding="gzip"))
    assert obs.fetch_url("https://example.test") == raw_index
    assert obs.discover_docnames() == ["index", "core"]

    not_found = HTTPError("https://example.test", 404, "missing", {}, None)
    monkeypatch.setattr(obs, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(not_found))
    assert obs.fetch_url("https://example.test") is None
    server_error = HTTPError("https://example.test", 500, "error", {}, None)
    monkeypatch.setattr(obs, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(server_error))
    assert obs.fetch_url("https://example.test") is None
    monkeypatch.setattr(obs, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("down")))
    assert obs.fetch_url("https://example.test") is None

    monkeypatch.setattr(obs, "fetch_url", lambda _url: None)
    with pytest.raises(SystemExit):
        obs.discover_docnames()
    monkeypatch.setattr(obs, "fetch_url", lambda _url: "invalid")
    with pytest.raises(SystemExit):
        obs.discover_docnames()
    monkeypatch.setattr(obs, "fetch_url", lambda _url: 'Search.setIndex({"docnames": []})')
    with pytest.raises(SystemExit):
        obs.discover_docnames()

    cache_file = tmp_path / ".cache.json"
    docs = tmp_path / "docs"
    monkeypatch.setattr(obs, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(obs, "DOCS_DIR", str(docs))
    assert obs.load_cache() == {}
    obs.save_cache({"index": {"sha256": "abc"}})
    assert obs.load_cache() == {"index": {"sha256": "abc"}}
    output = docs / "index.md"
    obs.write_file(str(output), "body", dry_run=False, verbose=True, label="ADD")
    assert output.read_text() == "body"
    dry = docs / "dry.md"
    obs.write_file(str(dry), "body", dry_run=True, verbose=False, label="ADD")
    assert not dry.exists()

    called = []
    monkeypatch.setattr(obs, "sync", called.append)
    monkeypatch.setattr(obs.sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose"])
    obs.main()
    assert called[0].dry_run and called[0].force and called[0].verbose


def test_rst_converter_metadata_and_toctree_mapping():
    sources = {
        "index": """OBS API
========

.. toctree::
   :caption: API Reference

   Core <core>
""",
        "core": """Core
====

.. _create-source:

Create Source
-------------

.. function:: obs_source_create(name)

   Create a source.

See :ref:`create-source`.
""",
    }
    titles, labels = obs.collect_metadata(sources)
    markdown = obs.convert_page("core", sources["core"], titles, labels)
    trees = obs.parse_toctrees(sources["index"])

    assert titles == {"index": "OBS API", "core": "Core"}
    assert labels["create-source"]["slug"] == "create-source"
    assert "**`obs_source_create(name)`**" in markdown
    assert "[Create Source](#create-source)" in markdown
    assert trees == [{"caption": "API Reference", "entries": [("Core", "core")]}]


def test_representative_rst_dialect_and_readme_tree():
    source = """Reference
=========

.. _section-label:

Section
-------

Inline ``code``, `site <https://example.test>`_, :doc:`Other <other>`,
:wiki:`OBS Studio`, :c:func:`~obs.source_create`, and :ref:`section-label`.

.. _named: https://example.test/named

Use `named`_ and named_.

.. function:: obs_startup(locale)

   Start OBS.

   :param const char * locale: Locale name.
   :return: Whether startup succeeded.

.. struct:: obs_source

   Source structure.

.. code:: cpp

   int value = 1;

.. note:: Remember

   Use the main thread.

.. warning::

   Do not block.

.. versionadded:: 30.0

   Added recently.

.. versionchanged:: 31.0

.. deprecated:: 32.0

   Use another API.

.. custom:: Visible argument

   Visible body.

- First item
  continuation
- Second item

Example::

   one
   two

----
"""
    titles, labels = obs.collect_metadata({"reference": source, "other": "Other\n=====\n"})
    markdown = obs.convert_page("reference", source, titles, labels)

    assert "[site](https://example.test)" in markdown
    assert "[Other](other.md)" in markdown
    assert "[OBS Studio](https://obsproject.com/wiki/OBS Studio)" in markdown
    assert "`source_create()`" in markdown
    assert "[Section](#section)" in markdown
    assert "[named](https://example.test/named)" in markdown
    assert "**`obs_startup(locale)`**" in markdown
    assert "- **const char * locale**: Locale name." in markdown
    assert "```cpp\nint value = 1;\n```" in markdown
    assert "> [!NOTE]" in markdown
    assert "> [!WARNING]" in markdown
    assert "*New in version 30.0.*" in markdown
    assert "Deprecated since version 32.0" in markdown
    assert "- First item continuation" in markdown
    assert "```\none\ntwo\n```" in markdown

    index = """Index
=====

.. toctree::
   :caption: Guides

   Reference <reference>
   https://example.test/external
"""
    reference = """Reference
=========

.. toctree::

   other
"""
    readme = obs.build_readme(
        ["index", "reference", "other", "orphan"],
        {"index": index, "reference": reference, "other": "Other\n=====\n", "orphan": "Orphan\n======\n"},
        {"index": "Index", "reference": "Reference", "other": "Other", "orphan": "Orphan"},
    )
    assert "## Guides" in readme
    assert "  - [Other](other.md)" in readme
    assert "[https://example.test/external](https://example.test/external)" in readme
    assert "## Other" in readme and "[Orphan](orphan.md)" in readme


def test_sync_preserves_cached_page_when_source_fetch_fails(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "missing.md").write_text("last known good")
    cache_file = tmp_path / ".cache.json"
    cache_file.write_text(json.dumps({"missing": {"sha256": "old"}}))
    search = 'Search.setIndex({"docnames": ["index", "missing"]})'
    monkeypatch.setattr(obs, "DOCS_DIR", str(docs))
    monkeypatch.setattr(obs, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(
        obs,
        "fetch_url",
        lambda url: (
            search
            if url == obs.SEARCHINDEX_URL
            else ("Index\n=====\n" if url.endswith("index.rst.txt") else None)
        ),
    )

    obs.sync(argparse.Namespace(force=False, dry_run=False, verbose=False))

    assert (docs / "missing.md").read_text() == "last known good"
    assert json.loads(cache_file.read_text())["missing"] == {"sha256": "old"}
