from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import run


def make_fetcher(root: Path, vendor: str, content: str = "") -> Path:
    directory = root / vendor
    directory.mkdir()
    fetcher = directory / "fetch.py"
    fetcher.write_text(content)
    return fetcher


def test_discover_vendors_returns_only_fetcher_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fetcher(tmp_path, "zeta")
    make_fetcher(tmp_path, "alpha")
    (tmp_path / "empty").mkdir()
    make_fetcher(tmp_path, ".hidden")
    (tmp_path / "plain-file").write_text("not a directory")
    monkeypatch.setattr(run, "SCRIPT_DIR", str(tmp_path))

    assert run.discover_vendors() == ["alpha", "zeta"]


@pytest.mark.parametrize("marker", ["# requires-interactive", "required=True", "required_group"])
def test_needs_interactive_recognizes_supported_markers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, marker: str
) -> None:
    make_fetcher(tmp_path, "private", marker)
    monkeypatch.setattr(run, "SCRIPT_DIR", str(tmp_path))

    assert run.needs_interactive("private") is True
    assert run.needs_interactive("missing") is False


def test_run_vendor_uses_current_python_and_optional_stdin_suppression(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    make_fetcher(tmp_path, "vendor")
    completed = subprocess.CompletedProcess([], 7)
    run_mock = Mock(return_value=completed)
    monkeypatch.setattr(run, "SCRIPT_DIR", str(tmp_path))
    monkeypatch.setattr(subprocess, "run", run_mock)

    result = run.run_vendor("vendor", ["--dry-run"], ["--extra"], suppress_stdin=True)

    assert result == 7
    run_mock.assert_called_once_with(
        [sys.executable, str(tmp_path / "vendor" / "fetch.py"), "--dry-run", "--extra"],
        cwd=str(tmp_path),
        stdin=subprocess.DEVNULL,
    )


def test_fzf_picker_returns_names_and_handles_cancel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "needs_interactive", lambda vendor: vendor == "private")
    completed = subprocess.CompletedProcess([], 0, stdout="alpha\nprivate (interactive)\n")
    monkeypatch.setattr(subprocess, "run", Mock(return_value=completed))

    assert run.interactive_pick_fzf(["alpha", "private"]) == ["alpha", "private"]

    monkeypatch.setattr(subprocess, "run", Mock(return_value=subprocess.CompletedProcess([], 130, stdout="")))
    assert run.interactive_pick_fzf(["alpha"]) == []


def test_fzf_picker_falls_back_when_fzf_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(subprocess, "run", Mock(side_effect=FileNotFoundError))

    assert run.interactive_pick_fzf(["alpha"]) is None


def test_basic_picker_parses_ranges_names_and_deduplicates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("builtins.input", lambda _prompt: "1 2-3 gamma unknown 3")
    monkeypatch.setattr(run, "needs_interactive", lambda _vendor: False)

    assert run.interactive_pick_basic(["alpha", "beta", "gamma"]) == ["alpha", "beta", "gamma"]
    assert "Unknown vendor: unknown" in capsys.readouterr().out


def test_main_lists_vendors_without_running_them(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run, "discover_vendors", lambda: ["alpha", "private"])
    monkeypatch.setattr(run, "needs_interactive", lambda vendor: vendor == "private")
    monkeypatch.setattr(sys, "argv", ["run.py", "--list"])

    run.main()

    output = capsys.readouterr().out
    assert "alpha" in output
    assert "private *" in output
    assert "2 vendors" in output


def test_main_all_skips_interactive_fetchers_and_propagates_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run, "discover_vendors", lambda: ["alpha", "private"])
    monkeypatch.setattr(run, "needs_interactive", lambda vendor: vendor == "private")
    run_vendor = Mock(return_value=9)
    monkeypatch.setattr(run, "run_vendor", run_vendor)
    monkeypatch.setattr(sys, "argv", ["run.py", "--all", "--dry-run"])

    with pytest.raises(SystemExit, match="1"):
        run.main()

    run_vendor.assert_called_once_with("alpha", ["--dry-run"], None)
    output = capsys.readouterr().out
    assert "Skipping private" in output
    assert "1 failed (alpha)" in output


def test_basic_picker_handles_all_empty_interrupt_and_ambiguous(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    vendors = ["alpha", "alpine", "beta"]
    monkeypatch.setattr(run, "needs_interactive", lambda _vendor: False)
    monkeypatch.setattr("builtins.input", lambda _prompt: "all")
    assert run.interactive_pick_basic(vendors) == vendors
    monkeypatch.setattr("builtins.input", lambda _prompt: "")
    assert run.interactive_pick_basic(vendors) == []
    monkeypatch.setattr("builtins.input", Mock(side_effect=EOFError))
    assert run.interactive_pick_basic(vendors) == []
    monkeypatch.setattr("builtins.input", lambda _prompt: "al 3-bad")
    assert run.interactive_pick_basic(vendors) == []
    assert "Ambiguous 'al'" in capsys.readouterr().out


def test_interactive_picker_uses_basic_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run, "interactive_pick_fzf", lambda _vendors: None)
    monkeypatch.setattr(run, "interactive_pick_basic", lambda _vendors: ["alpha"])
    assert run.interactive_pick(["alpha"]) == ["alpha"]


def test_main_runs_prefix_with_passthrough_and_reports_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run, "discover_vendors", lambda: ["alpha", "beta"])
    monkeypatch.setattr(run, "needs_interactive", lambda _vendor: False)
    run_vendor = Mock(return_value=0)
    monkeypatch.setattr(run, "run_vendor", run_vendor)
    monkeypatch.setattr(sys, "argv", ["run.py", "alp", "--verbose", "--", "--specific", "value"])

    run.main()

    run_vendor.assert_called_once_with("alpha", ["--verbose"], ["--specific", "value"])
    assert "Done: 1 succeeded" in capsys.readouterr().out


def test_main_handles_unknown_empty_and_no_discovery(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(run, "discover_vendors", lambda: [])
    monkeypatch.setattr(sys, "argv", ["run.py"])
    with pytest.raises(SystemExit):
        run.main()

    monkeypatch.setattr(run, "discover_vendors", lambda: ["alpha", "alpine"])
    monkeypatch.setattr(sys, "argv", ["run.py", "unknown"])
    with pytest.raises(SystemExit):
        run.main()

    monkeypatch.setattr(run, "interactive_pick", lambda _vendors: [])
    monkeypatch.setattr(sys, "argv", ["run.py"])
    run.main()
    assert "Nothing selected" in capsys.readouterr().out
