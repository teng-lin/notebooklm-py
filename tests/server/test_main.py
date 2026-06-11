"""U1/U2: ``notebooklm-server`` launcher guards (bind + token fail-closed)."""

from __future__ import annotations

import pytest

from notebooklm.server import __main__ as launcher
from notebooklm.server._auth import SERVER_TOKEN_ENV


def test_refuses_non_loopback_host_without_override() -> None:
    with pytest.raises(SystemExit):
        launcher._check_bind_allowed("0.0.0.0", allow_external=False)


def test_accepts_loopback_host() -> None:
    launcher._check_bind_allowed("127.0.0.1", allow_external=False)
    launcher._check_bind_allowed("localhost", allow_external=False)


def test_accepts_non_loopback_with_override() -> None:
    launcher._check_bind_allowed("203.0.113.5", allow_external=True)


def test_refuses_empty_host_even_with_override() -> None:
    with pytest.raises(SystemExit):
        launcher._check_bind_allowed("", allow_external=True)


def test_refuses_to_start_without_a_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SERVER_TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit):
        launcher._check_token_configured()


def test_token_present_allows_start(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SERVER_TOKEN_ENV, "secret")
    launcher._check_token_configured()  # no raise


def test_bad_port_fails_clean() -> None:
    with pytest.raises(SystemExit):
        launcher._resolve_port("not-a-number")
    assert launcher._resolve_port("8123") == 8123


@pytest.mark.parametrize("raw", ["-1", "65536", "70000"])
def test_out_of_range_port_fails_clean(raw: str) -> None:
    # An in-range-int-but-out-of-socket-range port fails at parse time with a
    # clear message, not later at bind time.
    with pytest.raises(SystemExit):
        launcher._resolve_port(raw)


@pytest.mark.parametrize("raw,expected", [("0", 0), ("65535", 65535)])
def test_boundary_ports_accepted(raw: str, expected: int) -> None:
    assert launcher._resolve_port(raw) == expected
