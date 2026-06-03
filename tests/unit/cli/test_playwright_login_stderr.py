"""Unit tests for ``playwright_login.redact_subprocess_output`` (audit G4).

These tests pin the security contract for surfacing captured subprocess
``stdout`` / ``stderr`` from the Playwright pre-flight in
``cli/services/playwright_login.py``:

1. Environment-variable VALUES that appear in the captured output are
   redacted before printing (defence against accidental secret leak when
   the playwright CLI echoes config / tracebacks).
2. ANSI control sequences (CSI colour codes, OSC titles, two-byte escapes
   used by pip/playwright progress UI) are stripped.
3. Multiple env-var values in a single string are all redacted.
4. Empty / well-known constant env values do NOT trigger spurious
   redactions across normal stderr lines.
5. The install-failure path in ``ensure_chromium_installed`` routes
   captured stderr through the helper before printing — verified with
   stub ``subprocess.CompletedProcess`` fixtures so the test never
   requires a real Playwright install.

The helper is a pure string transform; tests pass an explicit ``env``
mapping so they never mutate ``os.environ``.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

from notebooklm.cli.playwright_login_io import make_login_io
from notebooklm.cli.services.playwright_login import (
    ensure_chromium_installed,
    redact_subprocess_output,
)

# ---------------------------------------------------------------------------
# redact_subprocess_output — env-var value redaction
# ---------------------------------------------------------------------------


def test_redacts_psidts_like_secret_in_stderr() -> None:
    """A PSIDTS-style cookie value in stderr must be replaced with <redacted>."""
    secret = "psidts_abc123_long_secret_value"
    env = {"NOTEBOOKLM_PSIDTS": secret}
    raw = f"Traceback: failed to authenticate with cookie {secret}\n"

    sanitised = redact_subprocess_output(raw, env=env)

    assert secret not in sanitised
    assert "<redacted>" in sanitised
    assert "Traceback" in sanitised  # non-secret content preserved


def test_redacts_multiple_env_values_in_one_string() -> None:
    """Every non-trivial env-var value appearing in the input gets redacted."""
    env = {
        "SECRET_A": "alpha_value_that_should_be_redacted",
        "SECRET_B": "beta_value_that_should_also_redact",
    }
    raw = (
        "config dump:\n"
        "  field_one=alpha_value_that_should_be_redacted\n"
        "  field_two=beta_value_that_should_also_redact\n"
    )

    sanitised = redact_subprocess_output(raw, env=env)

    assert "alpha_value_that_should_be_redacted" not in sanitised
    assert "beta_value_that_should_also_redact" not in sanitised
    assert sanitised.count("<redacted>") == 2
    assert "field_one" in sanitised
    assert "field_two" in sanitised


def test_skips_trivial_env_values() -> None:
    """Empty / single-char / well-known env values must NOT trigger redaction.

    Otherwise common path / boolean strings would be replaced across the
    normal subprocess output (e.g. ``PATH=/`` would smear ``<redacted>``
    over every "/" in the captured stderr).
    """
    env = {
        "EMPTY": "",
        "SLASH": "/",
        "DOT": ".",
        "BOOL_TRUE": "true",
        "BOOL_FALSE": "False",
        "ZERO": "0",
        "ONE": "1",
        "Y": "y",
        "STAR": "*",
        "SHORT": "ab",  # below _REDACTION_MIN_VALUE_LEN
    }
    raw = "stderr line: path=/usr/local/bin, flag=true, retries=0\n"

    sanitised = redact_subprocess_output(raw, env=env)

    assert "<redacted>" not in sanitised
    # Content should be unchanged apart from ANSI stripping (none here).
    assert sanitised == raw


def test_longer_value_wins_when_one_is_a_substring_of_another() -> None:
    """A longer env value that contains a shorter env value gets redacted as a whole."""
    env = {
        "LONG": "secret_token_full_value",
        "SHORTER": "secret_token",  # substring of LONG
    }
    raw = "leaked: secret_token_full_value\n"

    sanitised = redact_subprocess_output(raw, env=env)

    # Either way the long secret must not appear in cleartext.
    assert "secret_token_full_value" not in sanitised
    # And we shouldn't end up with a half-redacted "<redacted>_full_value"
    # because the longer key sorts first.
    assert "_full_value" not in sanitised


# ---------------------------------------------------------------------------
# redact_subprocess_output — ANSI stripping
# ---------------------------------------------------------------------------


def test_strips_ansi_csi_color_codes() -> None:
    """CSI sequences (the common 'colour' codes) are removed."""
    raw = "\x1b[31mERROR\x1b[0m: install failed\n"

    sanitised = redact_subprocess_output(raw, env={})

    assert "\x1b" not in sanitised
    assert sanitised == "ERROR: install failed\n"


def test_strips_ansi_osc_sequences() -> None:
    """OSC sequences (window titles etc.) terminated by BEL or ST are removed."""
    bel = "\x1b]0;some title\x07after-bel"
    st = "\x1b]0;another title\x1b\\after-st"

    assert redact_subprocess_output(bel, env={}) == "after-bel"
    assert redact_subprocess_output(st, env={}) == "after-st"


def test_strips_two_byte_escape_sequences() -> None:
    """Two-byte ESC sequences (Fp/nF/Fe) used by progress UI are removed."""
    raw = "before\x1bMmiddle\x1bDend"

    sanitised = redact_subprocess_output(raw, env={})

    assert "\x1b" not in sanitised
    assert sanitised == "beforemiddleend"


def test_strips_full_c1_fe_range() -> None:
    """The two-byte ``ESC`` + Fe-class final byte covers the full 0x40-0x5F range.

    This guards the C1 Fe class boundary (``ESC ^`` PM, ``ESC _`` APC, etc.)
    that an earlier draft missed because the catch-all character class
    excluded ``]`` (0x5D) and ``^`` (0x5E).
    """
    # 0x40='@' through 0x5F='_' is the full C1 Fe range.
    full_range = "".join(f"\x1b{chr(b)}" for b in range(0x40, 0x60))
    raw = f"start{full_range}end"

    sanitised = redact_subprocess_output(raw, env={})

    assert "\x1b" not in sanitised
    assert sanitised == "startend"


def test_strips_ansi_and_redacts_env_value_together() -> None:
    """Combined ANSI + env-value input is sanitised in both dimensions."""
    secret = "psidts_value_must_redact"
    env = {"PSIDTS": secret}
    raw = f"\x1b[33mWARN\x1b[0m: using cookie {secret} for auth\n"

    sanitised = redact_subprocess_output(raw, env=env)

    assert "\x1b" not in sanitised
    assert secret not in sanitised
    assert "<redacted>" in sanitised
    assert "WARN" in sanitised


# ---------------------------------------------------------------------------
# redact_subprocess_output — edge cases
# ---------------------------------------------------------------------------


def test_empty_input_returns_empty() -> None:
    """Empty input is returned verbatim (no crash, no allocation)."""
    assert redact_subprocess_output("", env={"X": "long_enough_value"}) == ""


def test_uses_os_environ_when_env_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``env`` defaults to None, the helper snapshots ``os.environ``."""
    secret = "from_os_environ_should_redact"
    monkeypatch.setenv("REDACT_TEST_SECRET", secret)
    raw = f"line: {secret}\n"

    sanitised = redact_subprocess_output(raw)

    assert secret not in sanitised
    assert "<redacted>" in sanitised


def test_redacts_ansi_interrupted_secret() -> None:
    """A secret split by an inert ANSI reset must still be redacted.

    Regression: if the helper redacted BEFORE stripping ANSI, then
    ``"abc\\x1b[0m123"`` would not match env value ``"abc123"`` (the
    escape bytes break the substring), and the subsequent ANSI strip
    would reassemble the secret in the clear. ANSI must be stripped
    FIRST so reassembled secrets are caught.
    """
    secret = "psidts_full_secret_abc123"
    env = {"PSIDTS": secret}
    # Split the secret with a no-op ANSI reset in the middle.
    raw = "leaked: psidts_full_secret_\x1b[0mabc123\n"

    sanitised = redact_subprocess_output(raw, env=env)

    assert secret not in sanitised
    assert "<redacted>" in sanitised
    # And no raw escape byte remains.
    assert "\x1b" not in sanitised


def test_redacts_nested_json_leaf_value() -> None:
    """Leaf strings inside a JSON-shaped env value (NOTEBOOKLM_AUTH_JSON) are redactable."""
    inner_secret = "cookie_value_inside_json_blob"
    auth_json = '{"cookies":[{"name":"PSIDTS","value":"' + inner_secret + '"}],"profile":"default"}'
    env = {"NOTEBOOKLM_AUTH_JSON": auth_json}
    # Subprocess prints only the nested cookie value, not the whole JSON.
    raw = f"playwright: using auth cookie {inner_secret} for request\n"

    sanitised = redact_subprocess_output(raw, env=env)

    assert inner_secret not in sanitised
    assert "<redacted>" in sanitised


def test_redacts_nested_json_array_leaf() -> None:
    """Leaf strings inside a JSON array (not just object) are also expanded."""
    inner_secret = "another_secret_in_array"
    env = {"FAKE_LIST": f'["benign", "{inner_secret}", "also_benign"]'}
    raw = f"line: token={inner_secret} fail\n"

    sanitised = redact_subprocess_output(raw, env=env)

    assert inner_secret not in sanitised


def test_malformed_json_env_value_falls_back_to_exact_match() -> None:
    """An env value that LOOKS like JSON but doesn't parse still redacts as a string."""
    not_quite_json = "{not_real_json_but_starts_with_brace"
    env = {"WEIRD": not_quite_json}
    raw = f"line: WEIRD={not_quite_json}\n"

    sanitised = redact_subprocess_output(raw, env=env)

    assert not_quite_json not in sanitised
    assert "<redacted>" in sanitised


# ---------------------------------------------------------------------------
# ensure_chromium_installed — applies redaction on install-failure
# ---------------------------------------------------------------------------


class _StubCompletedProcess:
    """Minimal stand-in for ``subprocess.CompletedProcess`` used in tests.

    Lets the test inject specific ``stdout`` / ``stderr`` / ``returncode``
    values without invoking Playwright. Mirrors the public attribute
    surface that ``ensure_chromium_installed`` reads.
    """

    def __init__(self, *, stdout: str = "", stderr: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_install_failure_prints_sanitised_stderr(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Install-failure path runs captured stderr through ``redact_subprocess_output``."""
    secret = "install_path_secret_value_xyz"
    monkeypatch.setenv("REDACT_INSTALL_TEST", secret)

    dry_run = _StubCompletedProcess(
        stdout="chromium will download to /tmp/...",
        stderr="",
        returncode=0,
    )
    install_fail = _StubCompletedProcess(
        stdout="",
        stderr=(
            f"\x1b[31mERROR\x1b[0m: failed to fetch playwright bundle\nenv dump: TOKEN={secret}\n"
        ),
        returncode=1,
    )

    call_log: list[list[str]] = []

    def fake_run(cmd: list[str], **_: Any) -> _StubCompletedProcess:
        call_log.append(cmd)
        if "--dry-run" in cmd:
            return dry_run
        return install_fail

    monkeypatch.setattr(subprocess, "run", fake_run)

    # ``exit_with_code(1)`` calls ``sys.exit(1)``; capture the SystemExit.
    with pytest.raises(SystemExit) as exc_info:
        ensure_chromium_installed(make_login_io())

    assert exc_info.value.code == 1
    assert len(call_log) == 2  # dry-run probe + install attempt

    captured = capsys.readouterr().out
    # Secret env value must NOT appear in cleartext.
    assert secret not in captured
    # The sanitised diagnostic block should have been emitted.
    assert "Failed to install Chromium browser" in captured
    assert "Subprocess output (sanitised)" in captured
    assert "<redacted>" in captured
    # ANSI codes from the stub stderr must have been stripped.
    assert "\x1b" not in captured


def test_install_failure_falls_back_to_stdout_when_stderr_is_only_whitespace(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """If sanitised stderr is whitespace-only, the diagnostic falls back to stdout.

    Regression: an earlier draft used ``sanitised_stderr or sanitised_stdout``
    which short-circuits on any truthy stderr (including ``"   \\n"``),
    so a stderr that contained only ANSI progress noise would shadow a
    stdout line carrying the actual failure message.
    """
    dry_run = _StubCompletedProcess(
        stdout="chromium will download to /tmp/...",
        stderr="",
        returncode=0,
    )
    install_fail = _StubCompletedProcess(
        stdout="actual actionable error from stdout\n",
        # Stderr that sanitises down to whitespace (ANSI progress only).
        stderr="\x1b[2K\x1b[1G\n   \n",
        returncode=1,
    )

    def fake_run(cmd: list[str], **_: Any) -> _StubCompletedProcess:
        if "--dry-run" in cmd:
            return dry_run
        return install_fail

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(SystemExit):
        ensure_chromium_installed(make_login_io())

    captured = capsys.readouterr().out
    assert "actual actionable error from stdout" in captured
    assert "Subprocess output (sanitised)" in captured


def test_dry_run_unexpected_stderr_routed_through_redactor(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Dry-run stderr (debug log path) also goes through the redactor."""
    import logging as _logging

    secret = "dry_run_secret_value_abc"
    monkeypatch.setenv("REDACT_DRY_RUN_TEST", secret)

    dry_run = _StubCompletedProcess(
        # "will download" marker absent → take the early-return branch.
        stdout="chromium is already installed",
        stderr=f"warning: TOKEN={secret}\n",
        returncode=0,
    )

    def fake_run(cmd: list[str], **_: Any) -> _StubCompletedProcess:
        return dry_run

    monkeypatch.setattr(subprocess, "run", fake_run)

    with caplog.at_level(_logging.DEBUG, logger="notebooklm.cli.services.playwright_login"):
        ensure_chromium_installed(make_login_io())

    # The debug log line should be present but redacted.
    matching = [
        record.getMessage()
        for record in caplog.records
        if "playwright install --dry-run stderr" in record.getMessage()
    ]
    assert matching, "expected a debug log line for dry-run stderr"
    assert all(secret not in msg for msg in matching)
    assert any("<redacted>" in msg for msg in matching)
