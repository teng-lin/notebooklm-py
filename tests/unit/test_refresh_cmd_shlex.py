"""``_run_refresh_cmd`` shell-injection hardening.

Pins the contract that ``NOTEBOOKLM_REFRESH_CMD`` defaults to ``shell=False``
via :func:`shlex.split`, with an explicit opt-in for the legacy ``shell=True``
mode and basename-only logging of the first token.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from notebooklm import auth as auth_mod
from notebooklm.auth import (
    NOTEBOOKLM_REFRESH_CMD_ENV,
    NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV,
    _run_refresh_cmd,
    _split_refresh_cmd,
)


@pytest.fixture(autouse=True)
def _clear_refresh_env(monkeypatch):
    """Each test starts with no inherited refresh-cmd env vars."""
    monkeypatch.delenv(NOTEBOOKLM_REFRESH_CMD_ENV, raising=False)
    monkeypatch.delenv(NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, raising=False)
    monkeypatch.delenv("_NOTEBOOKLM_REFRESH_ATTEMPTED", raising=False)


def _stub_storage_path(monkeypatch, tmp_path: Path) -> Path:
    """Point ``get_storage_path`` at a writable temp file."""
    storage = tmp_path / "storage_state.json"
    storage.write_text("{}")
    monkeypatch.setattr(auth_mod, "get_storage_path", lambda profile=None: storage)
    return storage


class _RecordingRun:
    """Stand-in for ``subprocess.run`` that records its call args."""

    def __init__(self, returncode: int = 0, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr
        self.calls: list[dict[str, Any]] = []

    def __call__(self, *args, **kwargs):
        # ``subprocess.run(target, shell=..., ...)`` — first positional is the
        # command. We record both positional and keyword args.
        self.calls.append({"args": args, "kwargs": kwargs})
        return subprocess.CompletedProcess(
            args=args[0] if args else kwargs.get("args"),
            returncode=self.returncode,
            stdout=self.stdout,
            stderr=self.stderr,
        )


class TestShlexDefault:
    """Default path: ``shlex.split`` + ``shell=False``."""

    @pytest.mark.asyncio
    async def test_simple_command_split_and_run_without_shell(self, monkeypatch, tmp_path):
        _stub_storage_path(monkeypatch, tmp_path)
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        recorder = _RecordingRun(returncode=0)
        monkeypatch.setattr(auth_mod.subprocess, "run", recorder)

        await _run_refresh_cmd()

        assert recorder.calls, "subprocess.run was not invoked"
        call = recorder.calls[0]
        target = call["args"][0]
        assert isinstance(target, list), "expected a list argv when shell=False"
        assert target == ["echo", "hi"]
        assert call["kwargs"]["shell"] is False

    @pytest.mark.asyncio
    async def test_quoted_command_split_preserves_tokens(self, monkeypatch, tmp_path):
        _stub_storage_path(monkeypatch, tmp_path)
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, 'echo "hello world"')
        recorder = _RecordingRun(returncode=0)
        monkeypatch.setattr(auth_mod.subprocess, "run", recorder)

        await _run_refresh_cmd()

        target = recorder.calls[0]["args"][0]
        # The quoted span stays a single argv element AND the syntactic
        # quotes are stripped on both platforms — POSIX via shlex.split,
        # Windows via CommandLineToArgvW.
        assert target == ["echo", "hello world"]
        assert len(target) == 2

    @pytest.mark.asyncio
    async def test_malformed_command_raises_runtime_error(self, monkeypatch, tmp_path):
        _stub_storage_path(monkeypatch, tmp_path)
        # Unterminated double quote — POSIX ``shlex.split`` raises ValueError.
        # Skip on Windows: ``CommandLineToArgvW`` is lenient and treats the
        # remainder of the string as the final quoted token rather than
        # signaling an error. We still get a valid argv, so this specific
        # surface is POSIX-only.
        if os.name == "nt":
            pytest.skip("CommandLineToArgvW is lenient about unterminated quotes")
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, 'echo "unterminated')
        # subprocess.run should never be reached.
        called = {"hit": False}

        def _boom(*args, **kwargs):
            called["hit"] = True
            raise AssertionError("subprocess.run must not run on parse failure")

        monkeypatch.setattr(auth_mod.subprocess, "run", _boom)

        with pytest.raises(RuntimeError, match="could not be parsed"):
            await _run_refresh_cmd()

        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_empty_argv_raises_runtime_error(self, monkeypatch, tmp_path):
        _stub_storage_path(monkeypatch, tmp_path)
        # All-whitespace string splits to []; the env-not-set guard treats ""
        # as missing, so we use spaces to bypass that and exercise the
        # empty-argv branch.
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, "   ")
        called = {"hit": False}

        def _boom(*args, **kwargs):
            called["hit"] = True
            raise AssertionError("subprocess.run must not run on empty argv")

        monkeypatch.setattr(auth_mod.subprocess, "run", _boom)

        with pytest.raises(RuntimeError, match="parsed to empty argv"):
            await _run_refresh_cmd()

        assert called["hit"] is False

    @pytest.mark.asyncio
    async def test_first_token_logged_basename_only(self, monkeypatch, tmp_path, caplog):
        _stub_storage_path(monkeypatch, tmp_path)
        secret_path = "/home/user/.secrets/refresh.sh"
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, f"{secret_path} --token=hunter2")
        recorder = _RecordingRun(returncode=0)
        monkeypatch.setattr(auth_mod.subprocess, "run", recorder)

        caplog.set_level(logging.INFO, logger=auth_mod.logger.name)
        await _run_refresh_cmd()

        # Argv reached the runner with the full path + token intact.
        assert recorder.calls[0]["args"][0] == [secret_path, "--token=hunter2"]
        # But neither the parent directory nor the token appear in the INFO log.
        info_lines = [
            record.getMessage() for record in caplog.records if record.levelno == logging.INFO
        ]
        running_lines = [line for line in info_lines if "Running refresh command" in line]
        assert running_lines, f"missing 'Running refresh command' log; got: {info_lines}"
        joined = "\n".join(running_lines)
        assert "refresh.sh" in joined
        assert "/home/user/.secrets" not in joined
        assert "hunter2" not in joined


class TestShellOptIn:
    """Legacy opt-in path: ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1``."""

    @pytest.mark.asyncio
    async def test_opt_in_uses_shell_true_with_raw_string(self, monkeypatch, tmp_path):
        _stub_storage_path(monkeypatch, tmp_path)
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, "echo $HOME | tr a-z A-Z")
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, "1")
        recorder = _RecordingRun(returncode=0)
        monkeypatch.setattr(auth_mod.subprocess, "run", recorder)

        await _run_refresh_cmd()

        call = recorder.calls[0]
        target = call["args"][0]
        # In shell-mode the raw string is forwarded verbatim — no split.
        assert target == "echo $HOME | tr a-z A-Z"
        assert call["kwargs"]["shell"] is True

    @pytest.mark.asyncio
    async def test_opt_in_emits_warning(self, monkeypatch, tmp_path, caplog):
        _stub_storage_path(monkeypatch, tmp_path)
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, "1")
        recorder = _RecordingRun(returncode=0)
        monkeypatch.setattr(auth_mod.subprocess, "run", recorder)

        caplog.set_level(logging.WARNING, logger=auth_mod.logger.name)
        await _run_refresh_cmd()

        warnings_emitted = [
            record.getMessage() for record in caplog.records if record.levelno == logging.WARNING
        ]
        assert any("shell-mode" in msg.lower() for msg in warnings_emitted), (
            f"expected shell-mode warning, got: {warnings_emitted}"
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "value",
        [
            "0",  # numeric falsy
            "true",  # YAML/JSON boolean false-friend
            "True",  # capitalized Python literal
            "yes",  # natural-language affirmative
            "on",  # systemd/INI affirmative
            "1 ",  # trailing whitespace (env files often add it)
            " 1",  # leading whitespace
            "",  # empty string (explicit unset-equivalent)
        ],
    )
    async def test_opt_in_only_strict_literal_one(self, monkeypatch, tmp_path, value):
        """Only the literal '1' opts in. Common truthy-looking strings
        ('true', 'yes', 'on', whitespace-padded '1', ...) MUST stay on the
        safe shell=False default — these are the YAML/INI footguns users
        most easily hit when copying ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL=1``
        from one config to another.
        """
        _stub_storage_path(monkeypatch, tmp_path)
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, "echo hi")
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV, value)
        recorder = _RecordingRun(returncode=0)
        monkeypatch.setattr(auth_mod.subprocess, "run", recorder)

        await _run_refresh_cmd()

        call = recorder.calls[0]
        assert isinstance(call["args"][0], list), f"value={value!r} unexpectedly opted into shell"
        assert call["kwargs"]["shell"] is False


class TestSplitRefreshCmd:
    """Unit-level coverage of the per-platform parser ``_split_refresh_cmd``.

    The Windows branch uses ``CommandLineToArgvW`` (not ``shlex.split``) so
    quoted paths like ``"C:\\Program Files\\Python\\python.exe"`` arrive
    in argv WITHOUT the surrounding literal quotes. This was the regression
    flagged by CodeRabbit on the initial review.
    """

    def test_posix_split_strips_quotes(self):
        if os.name == "nt":
            pytest.skip("POSIX-only branch")
        assert _split_refresh_cmd("echo hi") == ["echo", "hi"]
        assert _split_refresh_cmd('echo "hello world"') == ["echo", "hello world"]

    def test_posix_split_raises_on_unterminated_quote(self):
        if os.name == "nt":
            pytest.skip("POSIX-only branch")
        with pytest.raises(ValueError):
            _split_refresh_cmd('echo "unterminated')

    def test_windows_split_strips_quotes_from_paths(self):
        """Regression: CodeRabbit-flagged case — quoted Windows paths must
        arrive in argv WITHOUT the literal quote characters so
        ``subprocess.run(argv, shell=False)`` can locate the executable.
        """
        if os.name != "nt":
            pytest.skip("Windows-only branch")
        cmd = (
            r'"C:\Program Files\Python\python.exe" '
            r'"C:\Temp\script with spaces.py"'
        )
        argv = _split_refresh_cmd(cmd)
        assert argv == [
            r"C:\Program Files\Python\python.exe",
            r"C:\Temp\script with spaces.py",
        ]
        for token in argv:
            assert not token.startswith('"'), f"literal quote leaked into argv: {token!r}"
            assert not token.endswith('"'), f"literal quote leaked into argv: {token!r}"

    def test_windows_split_handles_whitespace_only(self):
        if os.name != "nt":
            pytest.skip("Windows-only branch")
        # Whitespace-only / empty input: parser returns an empty argv (the
        # caller surfaces it as RuntimeError).
        assert _split_refresh_cmd("   ") == []


class TestEndToEndWithRealSubprocess:
    """Integration smoke: real subprocess invocation under shell=False."""

    @pytest.mark.asyncio
    async def test_python_command_via_shlex_split(self, monkeypatch, tmp_path):
        """A real refresh script runs successfully via shlex.split."""
        _stub_storage_path(monkeypatch, tmp_path)
        script = tmp_path / "refresh.py"
        marker = tmp_path / "ran.txt"
        script.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ok')\n")
        # Build a properly-quoted command line that the parser can re-parse.
        if os.name == "nt":
            cmd = subprocess.list2cmdline([sys.executable, str(script)])
        else:
            import shlex as _shlex

            cmd = _shlex.join([sys.executable, str(script)])
        monkeypatch.setenv(NOTEBOOKLM_REFRESH_CMD_ENV, cmd)

        await _run_refresh_cmd()

        assert marker.read_text() == "ok"
