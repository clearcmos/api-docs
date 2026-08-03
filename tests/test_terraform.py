from __future__ import annotations

import gzip
import json
import subprocess
import sys
from argparse import Namespace
from pathlib import Path
from unittest.mock import Mock
from urllib.error import HTTPError

import pytest

from tests.support import load_fetcher

terraform = load_fetcher("terraform")


def test_frontmatter_manifest_and_readmes_are_deterministic() -> None:
    assert terraform.strip_frontmatter("---\ntitle: Example\n---\n# Body\n") == "# Body"
    docs = [
        {"id": "2", "category": "resources", "slug": "b", "title": "B"},
        {"id": "1", "category": "resources", "slug": "a", "title": "A"},
    ]
    assert terraform.docs_manifest_hash(docs) == terraform.docs_manifest_hash(list(reversed(docs)))
    category = terraform.build_category_readme("resources", [{"title": "Widget", "filename": "widget.md"}])
    assert "[Widget](./widget.md)" in category
    assert "hashicorp/example" in terraform.build_top_readme(
        "hashicorp/example", "1.2.3", {"resources": docs}
    )


def test_output_completeness_requires_every_recorded_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(terraform, "DOCS_DIR", str(tmp_path))
    target = tmp_path / "hashicorp" / "example" / "README.md"
    target.parent.mkdir(parents=True)
    target.write_text("# Example\n")
    entry = {"outputs": ["hashicorp/example/README.md"]}

    assert terraform.outputs_present(entry) is True
    target.unlink()
    assert terraform.outputs_present(entry) is False
    assert terraform.outputs_present({"outputs": []}) is False


def test_fetch_doc_content_validates_json_api_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        terraform,
        "fetch_url",
        lambda _url: '{"data":{"attributes":{"title":"Widget","content":"# Widget"}}}',
    )
    assert terraform.fetch_doc_content("1") == {"title": "Widget", "content": "# Widget"}
    monkeypatch.setattr(terraform, "fetch_url", lambda _url: "[]")
    assert terraform.fetch_doc_content("1") is None


def test_picker_uses_fzf_selection_and_handles_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = [{"name": "hashicorp/aws", "tier": "official", "downloads": 2_000_000}]
    run_mock = Mock(return_value=subprocess.CompletedProcess([], 0, stdout="hashicorp/aws  (official)\n"))
    monkeypatch.setattr(subprocess, "run", run_mock)
    assert terraform.pick_provider_interactive(providers) == "hashicorp/aws"

    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 130, stdout="")))
    assert terraform.pick_provider_interactive(providers) is None


def test_retry_budget_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terraform.time, "monotonic", lambda: 100.0)
    assert terraform.out_of_budget(terraform.MAX_RETRIES, 100.0) is True
    assert terraform.out_of_budget(0, 100.0) is False
    assert terraform.retry_wait(0) == 1.5


def test_sync_handles_partial_success_fast_path_and_removal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docs_root = tmp_path / "docs"
    monkeypatch.setattr(terraform, "DOCS_DIR", str(docs_root))
    monkeypatch.setattr(terraform, "CACHE_FILE", str(tmp_path / "cache.json"))
    monkeypatch.setattr(terraform, "PROVIDER_DOCS_FILE", str(tmp_path / "provider.json"))
    provider = "hashicorp/example"
    docs = [
        {"id": "1", "category": "resources", "slug": "widget", "title": "Widget"},
        {"id": "2", "category": "data-sources", "slug": "lookup", "title": "Lookup"},
    ]
    version_data = {"docs": docs}

    def fetch_url(url: str, **_kwargs) -> str:
        if url.endswith("hashicorp/example"):
            return json.dumps({"version": "1.2.3"})
        return json.dumps(version_data)

    monkeypatch.setattr(terraform, "fetch_url", fetch_url)
    contents: dict[str, dict | None] = {
        "1": {"content": "---\ntitle: Widget\n---\n# Widget"},
        "2": None,
    }
    monkeypatch.setattr(terraform, "fetch_doc_content", lambda doc_id: contents[doc_id])
    args = Namespace(provider=provider, force=False, dry_run=False, verbose=True)

    terraform.sync(args)
    assert (docs_root / "hashicorp" / "example" / "resources" / "widget.md").exists()
    assert "source:hashicorp/example" not in terraform.load_cache()

    contents["2"] = {"content": "# Lookup"}
    terraform.sync(args)
    cache = terraform.load_cache()
    assert "source:hashicorp/example" in cache
    terraform.sync(args)

    version_data["docs"] = docs[:1]
    terraform.sync(args)
    assert not (docs_root / "hashicorp" / "example" / "data-sources" / "lookup.md").exists()


def test_sync_rejects_missing_registry_responses(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(terraform, "load_cache", lambda: {})
    monkeypatch.setattr(terraform, "fetch_url", lambda _url: None)
    args = Namespace(provider="hashicorp/example", force=True, dry_run=True, verbose=False)
    with pytest.raises(SystemExit):
        terraform.sync(args)

    responses = iter(['{"version":"1"}', None])
    monkeypatch.setattr(terraform, "fetch_url", lambda _url: next(responses))
    with pytest.raises(SystemExit):
        terraform.sync(args)


def test_cli_validates_provider_and_delegates(monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[Namespace] = []
    monkeypatch.setattr(terraform, "sync", called.append)
    monkeypatch.setattr(sys, "argv", ["fetch.py", "--provider", "hashicorp/example", "--dry-run"])
    terraform.main()
    assert called[0].provider == "hashicorp/example"

    monkeypatch.setattr(sys, "argv", ["fetch.py", "--provider", "invalid"])
    with pytest.raises(SystemExit):
        terraform.main()


def test_transport_retry_and_provider_index(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Response:
        headers = {"Content-Encoding": "gzip"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self) -> bytes:
            return gzip.compress(b"ok")

    attempts = iter([OSError("transient"), Response()])

    def urlopen(*_args, **_kwargs):
        value = next(attempts)
        if isinstance(value, Exception):
            raise value
        return value

    monkeypatch.setattr(terraform, "urlopen", urlopen)
    monkeypatch.setattr(terraform.time, "sleep", lambda _seconds: None)
    assert terraform.fetch_url("https://example.test") == "ok"
    fatal = HTTPError("https://example.test", 404, "missing", {}, None)
    monkeypatch.setattr(terraform, "urlopen", lambda *_args, **_kwargs: (_ for _ in ()).throw(fatal))
    assert terraform.fetch_url("https://example.test") is None

    monkeypatch.setattr(terraform, "INDEX_FILE", str(tmp_path / "provider-index.json"))
    pages = {
        1: {
            "meta": {"pagination": {"total-pages": 2, "total-count": 2}},
            "data": [
                {
                    "attributes": {
                        "full-name": "hashicorp/aws",
                        "tier": "official",
                        "downloads": 10,
                    }
                }
            ],
        },
        2: {
            "data": [
                {
                    "attributes": {
                        "full-name": "hashicorp/random",
                        "tier": "partner",
                        "downloads": 20,
                    }
                }
            ]
        },
    }
    monkeypatch.setattr(terraform, "_fetch_index_page", lambda page: pages[page])
    providers = terraform.refresh_provider_index()
    assert [item["name"] for item in providers] == ["hashicorp/random", "hashicorp/aws"]
    assert terraform.load_provider_index() == providers
    monkeypatch.setattr(terraform, "_fetch_index_page", lambda _page: None)
    assert terraform.refresh_provider_index() == []


def test_picker_falls_back_without_fzf(monkeypatch: pytest.MonkeyPatch) -> None:
    providers = [
        {"name": "hashicorp/aws", "tier": "official", "downloads": 2_000_000},
        {"name": "hashicorp/random", "tier": "official", "downloads": 10_000},
    ]
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=FileNotFoundError))
    answers = iter(["random", ""])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    assert terraform.pick_provider_interactive(providers) == "hashicorp/random"
