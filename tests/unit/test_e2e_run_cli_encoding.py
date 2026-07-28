"""Regression guard for the e2e ``run_cli`` subprocess-capture encoding.

Lives in the normal unit suite (``tests/e2e`` is ``--ignore``d and auth-gated, so
it never runs in ordinary CI) and needs no network: it monkeypatches
``subprocess.run`` and asserts the load-bearing UTF-8 capture kwargs survive.

Without them, ``run_cli`` decoded the CLI child's UTF-8 stdout with the locale
codec (cp1252 on Windows); a non-Latin-1 char in a live answer (e.g. a closing
curly quote ``”`` → ``E2 80 9D``) then crashed the reader thread, ``proc.stdout``
came back ``None``, and ``json.loads(None)`` raised a misleading ``TypeError``.
"""

from __future__ import annotations

import subprocess

from tests.e2e.conftest import run_cli


def test_run_cli_pins_utf8_capture(monkeypatch) -> None:
    # run_cli defaults the child via setdefault, so an ambient PYTHONUTF8!="1"
    # would no-op the default and fail this spuriously — isolate the default.
    monkeypatch.delenv("PYTHONUTF8", raising=False)
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_cli("list", "--json")

    kwargs = captured["kwargs"]
    # Our decode is pinned to UTF-8 with a never-None fallback...
    assert kwargs["encoding"] == "utf-8"
    assert kwargs["errors"] == "replace"
    # ...and the child is defaulted to UTF-8 output too.
    assert kwargs["env"]["PYTHONUTF8"] == "1"


def test_run_cli_child_utf8_is_overridable(monkeypatch) -> None:
    """``PYTHONUTF8`` is a base default, so ``extra_env`` can still override it."""
    captured: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    run_cli("list", extra_env={"PYTHONUTF8": "0"})

    assert captured["kwargs"]["env"]["PYTHONUTF8"] == "0"
    # The decode kwargs are not env-driven — they stay pinned regardless.
    assert captured["kwargs"]["encoding"] == "utf-8"
