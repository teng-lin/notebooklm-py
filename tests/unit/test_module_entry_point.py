"""``python -m notebooklm`` — the packaged module entry point.

``src/notebooklm/__main__.py`` is only reachable through ``python -m`` or
``runpy``, so nothing in the suite exercised it: a rename of
``notebooklm_cli.main`` would break the documented invocation with every test
still green. These cases pin the wiring in-process (so coverage sees it) and
end-to-end through a real interpreter.
"""

from __future__ import annotations

import runpy
import subprocess
import sys

import pytest

import notebooklm.notebooklm_cli as cli_module


def test_running_the_package_as_main_invokes_the_cli_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``run_name="__main__"`` takes the guarded branch, not just the import."""
    calls: list[tuple] = []
    monkeypatch.setattr(cli_module, "main", lambda *a, **k: calls.append((a, k)))

    runpy.run_module("notebooklm", run_name="__main__", alter_sys=True)

    assert calls == [((), {})]


def test_importing_the_module_does_not_invoke_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Imported under its real name, the ``__name__`` guard must hold."""
    calls: list[tuple] = []
    monkeypatch.setattr(cli_module, "main", lambda *a, **k: calls.append((a, k)))

    runpy.run_module("notebooklm.__main__", run_name="notebooklm.__main__")

    assert calls == []


def test_python_dash_m_notebooklm_reports_the_cli_help() -> None:
    """End-to-end through a real interpreter — no import-path shortcuts."""
    result = subprocess.run(
        [sys.executable, "-m", "notebooklm", "--help"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert "Usage:" in result.stdout


def test_python_dash_m_notebooklm_propagates_a_nonzero_exit() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "notebooklm", "definitely-not-a-command"],
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode != 0
