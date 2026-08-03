import argparse
import gzip
import json
from urllib.error import URLError

import pytest

from tests.support import load_fetcher

arch = load_fetcher("arch")


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


def test_transport_and_discovery_pagination(monkeypatch):
    body = gzip.compress(b'{"query": {"pages": []}}')
    monkeypatch.setattr(arch, "urlopen", lambda *_args, **_kwargs: Response(body, encoding="gzip"))
    assert arch.http_get_json("https://example.test") == {"query": {"pages": []}}
    monkeypatch.setattr(arch.time, "sleep", lambda _delay: None)
    monkeypatch.setattr(
        arch,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("down")),
    )
    assert arch.http_get_json("https://example.test", retries=2) is None

    title_replies = iter(
        [
            {
                "query": {
                    "allpages": [
                        {"title": "Pacman"},
                        {"title": "Pacman (Español)"},
                    ]
                },
                "continue": {"apcontinue": "next"},
            },
            {"query": {"allpages": [{"title": "Systemd"}]}},
        ]
    )
    monkeypatch.setattr(arch, "http_get_json", lambda _url: next(title_replies))
    assert arch.discover_titles(verbose=True) == ["Pacman", "Systemd"]

    revision_replies = iter(
        [
            {
                "query": {
                    "pages": [
                        {"title": "Pacman", "revisions": [{"revid": 12}]},
                        {"title": "Ignored (Español)", "revisions": [{"revid": 3}]},
                    ]
                },
                "continue": {"continue": "||", "gapcontinue": "next"},
            },
            {"query": {"pages": [{"title": "Systemd", "revisions": []}]}},
        ]
    )
    monkeypatch.setattr(arch, "http_get_json", lambda _url: next(revision_replies))
    assert arch.discover_revisions(verbose=True) == {"Pacman": 12, "Systemd": 0}
    monkeypatch.setattr(arch, "http_get_json", lambda _url: None)
    with pytest.raises(RuntimeError):
        arch.discover_revisions()
    with pytest.raises(SystemExit):
        arch.discover_titles()


def test_representative_wikitext_dialect():
    source = """<!-- comment -->__TOC__
{{Related articles start}}{{Related|Pacman|Package manager}}{{Related articles end}}
{{Style|remove me}}
== Inline ==
'''''both''''' '''bold''' ''italic'' <strong>strong</strong> <em>em</em> <kbd>key</kbd>
<nowiki>literal</nowiki><ref>citation</ref><br>[https://example.test Example]
[[wikipedia:Arch Linux|Wikipedia]] [[Pacman#Usage|anchor]]
{{ic|echo {{!}} pipe}} {{AUR|yay}} {{man|1|grep}} {{Wikipedia|Arch Linux|Arch}}
{{ic1|one}} {{ic2|two}} {{nbsp}}{{bull}}{{yes}}{{no}}{{n/a}}{{anchor|a}}
{{strong|strong}} {{em|em}} {{Unknown|visible}}
{{Warning|1=Careful = yes
Second line}}
{{File|name=test.conf|content=<nowiki>key=value</nowiki>}}
{{hc|Header|command --flag}}
{{bc|echo block}}
<pre>plain code</pre>
<source lang="python">print('x')</source>
{| class="wikitable"
|+ Caption
! Name !! Value
|-
| align="right" | A || B
| continuation
|}
# Ordered
: Quote
; Term
----
<hr>
 legacy code
[[Category:Hidden]]
"""

    markdown = arch.wikitext_to_markdown(source)

    assert "**Related articles**" in markdown
    assert "[Wikipedia](https://en.wikipedia.org/wiki/Arch_Linux)" in markdown
    assert "[anchor](https://wiki.archlinux.org/title/Pacman#Usage)" in markdown
    assert "[yay](https://aur.archlinux.org/packages/yay)" in markdown
    assert "[grep(1)]" in markdown
    assert "> **Warning:** Careful = yes" in markdown
    assert "**`test.conf`**" in markdown
    assert "```python" in markdown
    assert "| Name | Value |" in markdown
    assert "1. Ordered" in markdown
    assert "> Quote" in markdown
    assert "**Term**" in markdown
    assert "legacy code" in markdown
    assert "Category:Hidden" not in markdown
    assert arch._consume_template("plain", 0) == ("", 0)
    assert arch._consume_template("{{open", 0) == ("{{open", 6)
    assert arch._split_template_args("x|[[a|b]]|{{c|d}}") == ["x", "[[a|b]]", "{{c|d}}"]
    assert arch._render_admonition("custom", "") == ["> **Custom**", ""]
    assert arch._render_code_block("", "") == ["```", "```", ""]
    assert arch._render_table("") == []
    assert arch.is_tracking_category("Pages with broken section links")


def test_wikitext_conversion_and_paths():
    source = """== Setup ==
{{Note|Install {{Pkg|pacman}} first.}}
* See [[Installation guide|the guide]]
<syntaxhighlight lang="bash">pacman -Syu</syntaxhighlight>
"""

    markdown = arch.wikitext_to_markdown(source)

    assert "## Setup" in markdown
    assert "> **Note:** Install [pacman](https://archlinux.org/packages/?q=pacman) first." in markdown
    assert "[the guide](https://wiki.archlinux.org/title/Installation_guide)" in markdown
    assert "```bash\npacman -Syu\n```" in markdown
    assert arch.title_to_filename("Bluetooth/Headset") == "Bluetooth_Headset.md"
    assert arch.category_to_filename("Audio/Video") == "Audio_Video.md"
    assert arch.is_translated_title("Installation guide (Español)")


def test_bulk_fetch_reports_partial_failure(monkeypatch):
    replies = iter(
        [
            {
                "query": {
                    "pages": [
                        {
                            "title": "Pacman",
                            "revisions": [{"slots": {"main": {"content": "Package manager"}}}],
                            "categories": [{"title": "Category:Package management"}],
                        }
                    ]
                },
                "continue": {"continue": "||", "clcontinue": "next"},
            },
            None,
        ]
    )
    monkeypatch.setattr(arch, "http_get_json", lambda _url: next(replies))

    pages, failed_batches = arch.fetch_pages_bulk(["Pacman"])

    assert pages["Pacman"] == {
        "wikitext": "Package manager",
        "categories": ["Package management"],
    }
    assert failed_batches == 1


def test_sync_end_to_end_cache_hit_failure_and_removal(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    stale = docs / "Stale.md"
    stale.write_text("stale")
    cache_file = tmp_path / ".cache.json"
    cache_file.write_text(json.dumps({"Stale.md": {"sha256": "old", "title": "Stale"}}))
    monkeypatch.setattr(arch, "DOCS_DIR", str(docs))
    monkeypatch.setattr(arch, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(arch, "discover_revisions", lambda verbose=False: {"Pacman": 12})
    monkeypatch.setattr(
        arch,
        "fetch_pages_bulk",
        lambda _titles, verbose=False: (
            {"Pacman": {"wikitext": "== Usage ==\n{{Pkg|pacman}}", "categories": ["Package management"]}},
            0,
        ),
    )
    args = argparse.Namespace(force=False, dry_run=False, verbose=True, limit=0)

    arch.sync(args)
    first_cache = json.loads(cache_file.read_text())
    assert not stale.exists()
    assert (docs / "Pacman.md").exists()
    assert (docs / "_categories" / "Package_management.md").exists()
    assert first_cache["Pacman.md"]["revision"] == 12

    monkeypatch.setattr(arch, "fetch_pages_bulk", lambda titles, verbose=False: ({}, 0))
    arch.sync(args)
    assert json.loads(cache_file.read_text())["Pacman.md"] == first_cache["Pacman.md"]

    monkeypatch.setattr(
        arch,
        "discover_revisions",
        lambda verbose=False: (_ for _ in ()).throw(RuntimeError("down")),
    )
    with pytest.raises(SystemExit):
        arch.sync(args)
    monkeypatch.setattr(arch, "discover_revisions", lambda verbose=False: {"Pacman": 12})
    monkeypatch.setattr(arch, "fetch_pages_bulk", lambda _titles, verbose=False: ({}, 1))
    with pytest.raises(SystemExit):
        arch.sync(argparse.Namespace(force=True, dry_run=False, verbose=False, limit=1))


def test_cache_write_builders_and_main_boundaries(tmp_path, monkeypatch):
    cache_file = tmp_path / ".cache.json"
    docs = tmp_path / "docs"
    monkeypatch.setattr(arch, "CACHE_FILE", str(cache_file))
    monkeypatch.setattr(arch, "DOCS_DIR", str(docs))
    assert arch.load_cache() == {}
    arch.save_cache({"page": {"sha256": "abc"}})
    assert arch.load_cache() == {"page": {"sha256": "abc"}}
    output = docs / "page.md"
    arch.write_file(str(output), "body", dry_run=False, verbose=True, label="ADD")
    assert output.read_text() == "body"
    dry = docs / "dry.md"
    arch.write_file(str(dry), "body", dry_run=True, verbose=False, label="ADD")
    assert not dry.exists()

    page = arch.build_page_markdown("Pacman", "## Usage\n", ["Package management"])
    assert "**Categories:** [Package management]" in page
    assert "1 article in this category." in arch.build_category_md("Package management", ["Pacman"])
    assert "1 categories." in arch.build_categories_index({"Package management": ["Pacman"]})
    assert "1 English articles" in arch.build_top_readme(1, 1)

    called = []
    monkeypatch.setattr(arch, "sync", called.append)
    monkeypatch.setattr(arch.sys, "argv", ["fetch.py", "--dry-run", "--force", "--verbose", "--limit", "2"])
    arch.main()
    assert called[0].dry_run and called[0].force and called[0].verbose and called[0].limit == 2
