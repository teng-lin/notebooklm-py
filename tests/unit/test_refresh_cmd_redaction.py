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
    """stdout/stderr is routed to DEBUG for diagnosis, with secrets scrubbed.

    The package logger has a redaction filter installed at import time, so the
    captured record carries the diagnostic ``stdout=``/``stderr=`` structure
    (proving the data path exists) while the credential SHAPES are scrubbed in
    place. After #1517 the redaction covers the ``ya29.`` access-token shape and
    the ``SID=`` cookie, so this DEBUG sink — the leak path the issue calls out —
    fails closed; the full redaction filter is unit-tested in ``test_logging.py``.
    """
    _stub_subprocess_run_with_leaky_output(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"), pytest.raises(RuntimeError):
        asyncio.run(auth_module._run_refresh_cmd())

    debug_records = [r for r in caplog.records if r.levelno == logging.DEBUG]
    debug_text = "\n".join(r.getMessage() for r in debug_records)
    # The data path delivers SANITIZED CONTENT, not just empty labels: the
    # non-secret diagnostic context around each secret survives so ``--verbose``
    # users can still see what failed, with the credential collapsed to ``***``.
    # ``_SECRET_STDOUT`` is ``"Bearer ya29.…"``  -> ``stdout='Bearer ***'``;
    # ``_SECRET_STDERR`` is ``"rotate-cookie failed: SID=…"``
    #                                            -> ``stderr='rotate-cookie failed: SID=***'``.
    assert "stdout='Bearer ***'" in debug_text, (
        f"Expected scrubbed-but-present stdout content in DEBUG log: {debug_text!r}"
    )
    assert "stderr='rotate-cookie failed: SID=***'" in debug_text, (
        f"Expected scrubbed-but-present stderr content in DEBUG log: {debug_text!r}"
    )
    # And the raw credential shapes never survive (#1517): the ``ya29.`` access
    # token and the ``SID=`` cookie value are gone.
    assert _SECRET_STDOUT not in debug_text
    assert _SECRET_STDERR not in debug_text
    assert "ya29.SECRET-TOKEN" not in debug_text
    assert "SECRET-SID-VALUE" not in debug_text


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


def _capture_refresh_subprocess_env(monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Stub ``subprocess.run`` to record (and return) the ``env`` kwarg it received.

    Returns a dict that the caller can inspect after ``_run_refresh_cmd`` runs;
    the stub itself returns a zero-exit result so the refresh call completes
    normally. Mirrors ``_stub_subprocess_run_with_leaky_output`` above.
    """
    captured: dict[str, str] = {}

    class _Result:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(*_args: Any, **kwargs: Any) -> _Result:
        captured.update(kwargs.get("env") or {})
        return _Result()

    monkeypatch.setattr(subprocess, "run", _fake_run)
    return captured


def test_refresh_cmd_env_does_not_inherit_auth_json(
    refresh_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``NOTEBOOKLM_AUTH_JSON`` must be stripped from the refresh subprocess env.

    The env var carries the full Playwright ``storage_state`` (credential-
    equivalent) when callers route auth through environment instead of disk.
    ``os.environ.copy()`` would forward it to the refresh subprocess and any
    grandchildren it spawns, where it is visible via ``/proc/<pid>/environ``
    to the same UID and inherited by every child.

    Strip it before exec. The refresh command already receives the canonical
    on-disk path via ``NOTEBOOKLM_REFRESH_STORAGE_PATH``.
    """
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", '{"cookies":[{"name":"SID","value":"X"}]}')
    captured_env = _capture_refresh_subprocess_env(monkeypatch)

    asyncio.run(auth_module._run_refresh_cmd())

    assert captured_env, "subprocess.run was not invoked with an env kwarg"
    assert "NOTEBOOKLM_AUTH_JSON" not in captured_env, (
        f"NOTEBOOKLM_AUTH_JSON leaked into refresh subprocess env: keys={sorted(captured_env)}"
    )
    # The refresh-routing channel must still be set so the child can locate
    # the on-disk storage (this is what replaces the env-borne JSON).
    assert "NOTEBOOKLM_REFRESH_STORAGE_PATH" in captured_env
    assert "NOTEBOOKLM_REFRESH_PROFILE" in captured_env
    # Sanity: PATH (or some unrelated parent env var) still propagates so
    # we are stripping selectively, not wholesale.
    assert "PATH" in captured_env or "HOME" in captured_env, (
        "expected unrelated parent env vars to still propagate"
    )


def test_refresh_cmd_env_unaffected_when_auth_json_unset(
    refresh_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``NOTEBOOKLM_AUTH_JSON`` is not set, ``.pop(..., None)`` is a no-op
    and the refresh subprocess still runs to completion (regression guard)."""
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    captured_env = _capture_refresh_subprocess_env(monkeypatch)

    asyncio.run(auth_module._run_refresh_cmd())
    assert "NOTEBOOKLM_AUTH_JSON" not in captured_env
    assert "NOTEBOOKLM_REFRESH_STORAGE_PATH" in captured_env


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
