"""Unit tests for ``NOTEBOOKLM_REFRESH_CMD`` failure redaction (P1-18).

The refresh-command subprocess can print arbitrary content to stdout/stderr,
including bearer tokens, cookies, full URLs with query-string credentials,
and absolute paths into a user's home/credentials directory. Surfacing that
output verbatim through ``RuntimeError`` (which then bubbles up through
``handle_errors`` and lands on stderr or in a JSON envelope) leaks secrets.

The contract:

1. The exception message must contain only:
   - The env-var name (``NOTEBOOKLM_REFRESH_CMD``)
   - The integer exit code
   - The executable's basename (no absolute path)
2. The exception message must NOT contain stdout/stderr content.
3. The full stdout/stderr is routed to ``logger.debug`` at the package's
   redacting logger so ``-vv`` users with the redaction filter installed can
   still diagnose failures.
4. ``cli.error_handler`` prints only ``exc.args[0]`` (the redacted message)
   for the catch-all ``Exception`` branch; full traceback goes to
   ``logger.debug`` only.
"""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Iterator
from typing import Any

import pytest

from notebooklm import auth as auth_module

_SECRET_STDOUT = "Bearer ya29.SECRET-TOKEN-IN-STDOUT-deadbeef"
_SECRET_STDERR = "rotate-cookie failed: SID=SECRET-SID-VALUE-cafefeed"
_REFRESH_EXECUTABLE_PATH = "/home/user/.secret-credentials-dir/refresh-cookies.sh"


@pytest.fixture
def refresh_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Set NOTEBOOKLM_REFRESH_CMD to a known absolute path."""
    monkeypatch.setenv(auth_module.NOTEBOOKLM_REFRESH_CMD_ENV, _REFRESH_EXECUTABLE_PATH)
    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD_USE_SHELL", raising=False)
    yield


def _stub_subprocess_run_with_leaky_output(
    monkeypatch: pytest.MonkeyPatch,
    *,
    returncode: int = 1,
) -> None:
    """Replace ``subprocess.run`` so it returns secret-laden stdout/stderr."""

    class _Result:
        def __init__(self) -> None:
            self.returncode = returncode
            self.stdout = _SECRET_STDOUT
            self.stderr = _SECRET_STDERR

    def _fake_run(*_args: Any, **_kwargs: Any) -> _Result:
        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)


def test_refresh_failure_message_omits_stdout_secrets(
    refresh_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_subprocess_run_with_leaky_output(monkeypatch)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(auth_module._run_refresh_cmd())
    message = exc_info.value.args[0]
    assert _SECRET_STDOUT not in message
    assert "ya29." not in message


def test_refresh_failure_message_omits_stderr_secrets(
    refresh_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_subprocess_run_with_leaky_output(monkeypatch)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(auth_module._run_refresh_cmd())
    message = exc_info.value.args[0]
    assert _SECRET_STDERR not in message
    assert "SECRET-SID" not in message


def test_refresh_failure_message_shows_exit_code_and_basename(
    refresh_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stub_subprocess_run_with_leaky_output(monkeypatch, returncode=42)
    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(auth_module._run_refresh_cmd())
    message = exc_info.value.args[0]
    assert "42" in message
    # basename, not the absolute path
    assert "refresh-cookies.sh" in message
    assert "/home/user/.secret-credentials-dir" not in message


def test_refresh_failure_routes_full_output_to_debug_log(
    refresh_env: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Full stdout/stderr is available at DEBUG level for diagnosis.

    The package logger has a redaction filter installed at import time, so
    even when we capture the raw record here the user-facing handler scrubs
    well-known token shapes. This test confirms the data path exists; the
    redaction filter is unit-tested separately in ``test_logging.py``.
    """
    _stub_subprocess_run_with_leaky_output(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"), pytest.raises(RuntimeError):
        asyncio.run(auth_module._run_refresh_cmd())

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    debug_text = "\n".join(r.getMessage() for r in debug_records)
    # The secret output should be present in the DEBUG-level data path so
    # developers can diagnose subprocess failures with ``--verbose``.
    assert _SECRET_STDOUT in debug_text or _SECRET_STDERR in debug_text, (
        "Expected refresh-cmd stdout/stderr to be routed to DEBUG log"
    )


def test_error_handler_prints_only_exc_args_for_unexpected_exception(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The CLI's catch-all branch surfaces only the redacted message."""
    from notebooklm.cli.error_handler import handle_errors

    redacted_message = (
        f"{auth_module.NOTEBOOKLM_REFRESH_CMD_ENV} exited 1 (executable: refresh-cookies.sh)"
    )
    # Use the same structure as the real refresh-cmd raise: a RuntimeError
    # whose args[0] is the redacted message. The handler should print that
    # message and not touch any other attributes.
    err = RuntimeError(redacted_message)
    # Attach a fake __cause__ that has secret stuff; the handler must NOT
    # walk the cause chain into the user-facing output.
    err.__cause__ = RuntimeError(_SECRET_STDOUT)

    with pytest.raises(SystemExit) as exc_info, handle_errors():
        raise err

    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert _SECRET_STDOUT not in combined
    assert redacted_message in combined


def test_error_handler_handles_non_string_first_arg(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Claude bot review feedback: ``e.args[0]`` may be non-string for
    third-party exceptions (e.g. ``ValueError(42)``). Confirm the handler
    str-casts defensively rather than relying on f-string implicit ``str()``.
    """
    from notebooklm.cli.error_handler import handle_errors

    with pytest.raises(SystemExit) as exc_info, handle_errors():
        raise ValueError(42)
    assert exc_info.value.code == 2
    captured = capsys.readouterr()
    assert "Unexpected error: 42" in (captured.out + captured.err)


def test_error_handler_routes_traceback_to_debug(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Tracebacks for unexpected exceptions go to DEBUG, not stderr."""
    from notebooklm.cli.error_handler import handle_errors

    redacted_message = "REFRESH_CMD exited 1 (executable: refresh.sh)"
    err = RuntimeError(redacted_message)
    err.__cause__ = RuntimeError(_SECRET_STDOUT)

    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.cli.error_handler"),
        pytest.raises(SystemExit),
        handle_errors(),
    ):
        raise err

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug_records, "Expected at least one DEBUG record from error_handler"
    debug_text = "\n".join((r.getMessage() + "\n" + (r.exc_text or "")) for r in debug_records)
    # The full exception (with its cause chain) is what DEBUG-level captures
    # for developers; this is the place secrets COULD legitimately surface
    # for diagnosis. We assert the DEBUG path exists, not that it scrubs —
    # the redaction filter (tested separately) handles scrubbing on the way out.
    assert "RuntimeError" in debug_text or err.__class__.__name__ in debug_text
