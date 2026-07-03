"""U1/U2: ``notebooklm-server`` launcher guards (bind + token fail-closed)."""

from __future__ import annotations

import logging
import os
from unittest.mock import MagicMock

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


def test_is_loopback_rejects_non_ip_hostname() -> None:
    # A hostname that is neither in _LOOPBACK_HOSTNAMES nor a numeric IP literal
    # is treated as non-loopback (the ipaddress.ip_address ValueError branch).
    assert launcher._is_loopback("example.com") is False


# --- _build_parser: defaults, env-derived defaults, flag overrides -----------


def test_build_parser_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("NOTEBOOKLM_SERVER_HOST", "NOTEBOOKLM_SERVER_PORT", "NOTEBOOKLM_LOG_LEVEL"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv(SERVER_TOKEN_ENV, raising=False)
    monkeypatch.delenv(launcher.SERVER_TOKEN_FILE_ENV, raising=False)
    args = launcher._build_parser().parse_args([])
    assert args.host == "127.0.0.1"
    assert args.port == "8000"  # kept as a string; converted later by _resolve_port
    assert args.token is None  # deprecated flag defaults to None (never env-seeded)
    assert args.token_file is None
    assert args.log_level == "INFO"


def test_build_parser_reads_env_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_SERVER_HOST", "::1")
    monkeypatch.setenv("NOTEBOOKLM_SERVER_PORT", "9001")
    monkeypatch.setenv("NOTEBOOKLM_LOG_LEVEL", "DEBUG")
    # The bearer token no longer feeds --token (deprecated); --token-file reads its
    # own env default instead.
    monkeypatch.setenv(launcher.SERVER_TOKEN_FILE_ENV, "/etc/nblm/token")
    args = launcher._build_parser().parse_args([])
    assert args.host == "::1"
    assert args.port == "9001"
    assert args.token is None
    assert args.token_file == "/etc/nblm/token"
    assert args.log_level == "DEBUG"


def test_build_parser_flags_override_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_SERVER_HOST", "::1")
    args = launcher._build_parser().parse_args(["--host", "127.0.0.1", "--port", "8123"])
    assert args.host == "127.0.0.1"
    assert args.port == "8123"


def test_build_parser_profile_default_env_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Default is None (the parser reads NOTEBOOKLM_PROFILE, so clear it first).
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    assert launcher._build_parser().parse_args([]).profile is None
    # Env supplies the default...
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "envprof")
    assert launcher._build_parser().parse_args([]).profile == "envprof"
    # ...and the flag overrides the env.
    assert launcher._build_parser().parse_args(["--profile", "work"]).profile == "work"


# --- _configure_logging: maps the level name, falls back to INFO ------------


def test_configure_logging_maps_level(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(launcher.logging, "basicConfig", lambda **kw: captured.update(kw))
    launcher._configure_logging("debug")
    assert captured["level"] == logging.DEBUG
    launcher._configure_logging("not-a-level")  # unknown names fall back to INFO
    assert captured["level"] == logging.INFO


# --- main(): full launch path with uvicorn.run stubbed out ------------------


def _stub_uvicorn_run(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    """Stub out ``main()``'s side effects and capture the ``uvicorn.run`` call.

    Replaces ``uvicorn.run`` (so no server actually binds), ``create_app`` (so no
    real app is built), and the consumer-side ``logging`` binding with a wrapper
    whose ``basicConfig`` is a no-op (so ``main()`` does not reconfigure logging
    — without mutating the real ``logging`` module). Returns the dict the
    ``uvicorn.run`` call is captured into.
    """
    import uvicorn

    captured: dict[str, object] = {}

    def _capture(app: object, **kwargs: object) -> None:
        captured["app"] = app
        captured.update(kwargs)

    # Wrap the real module so getLogger()/level constants still work, but swallow
    # basicConfig — patching launcher's binding, not the global logging module.
    mock_logging = MagicMock(wraps=logging)
    mock_logging.basicConfig = lambda **_kwargs: None

    monkeypatch.setattr(uvicorn, "run", _capture)
    monkeypatch.setattr(launcher, "create_app", MagicMock(return_value="APP"))
    monkeypatch.setattr(launcher, "logging", mock_logging)
    return captured


def test_main_runs_server_with_resolved_args(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_uvicorn_run(monkeypatch)
    monkeypatch.setenv(SERVER_TOKEN_ENV, "env-secret")
    launcher.main(["--host", "127.0.0.1", "--port", "8123", "--log-level", "WARNING"])
    assert captured["app"] == "APP"
    assert captured["host"] == "127.0.0.1"
    assert captured["port"] == 8123  # _resolve_port converts the string to an int
    assert captured["log_level"] == "warning"  # uvicorn wants a lowercase level
    # Default (loopback) mode: proxy headers OFF so request.client.host is the real
    # socket peer (the loopback guard's basis), not a spoofable X-Forwarded-For.
    assert captured["proxy_headers"] is False


def test_main_threads_profile_into_create_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1769: --profile must reach create_app so the server binds the right profile."""
    _stub_uvicorn_run(monkeypatch)  # patches launcher.create_app with a MagicMock
    monkeypatch.setenv(SERVER_TOKEN_ENV, "env-secret")
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    launcher.main(["--host", "127.0.0.1", "--profile", "work"])
    launcher.create_app.assert_called_once_with(profile="work")  # type: ignore[attr-defined]


def test_main_rejects_deprecated_token_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_uvicorn_run(monkeypatch)
    monkeypatch.setenv(SERVER_TOKEN_ENV, "env-secret")
    # --token leaks via `ps`; it is refused outright even when a valid env token
    # would otherwise let the server start.
    with pytest.raises(SystemExit):
        launcher.main(["--host", "127.0.0.1", "--token", "argv-secret"])


def test_main_loads_token_from_file(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from pathlib import Path

    _stub_uvicorn_run(monkeypatch)
    monkeypatch.delenv(SERVER_TOKEN_ENV, raising=False)
    token_path = Path(str(tmp_path)) / "token.txt"
    token_path.write_text("file-token\n", encoding="utf-8")
    launcher.main(["--host", "127.0.0.1", "--token-file", str(token_path)])
    # The file's contents (stripped) seed the env the auth dependency reads; only
    # the PATH ever appeared on argv.
    assert os.environ[SERVER_TOKEN_ENV] == "file-token"


def test_main_empty_token_file_refuses(monkeypatch: pytest.MonkeyPatch, tmp_path: object) -> None:
    from pathlib import Path

    _stub_uvicorn_run(monkeypatch)
    monkeypatch.delenv(SERVER_TOKEN_ENV, raising=False)
    token_path = Path(str(tmp_path)) / "empty.txt"
    token_path.write_text("   \n", encoding="utf-8")
    with pytest.raises(SystemExit):
        launcher.main(["--host", "127.0.0.1", "--token-file", str(token_path)])


def test_main_refuses_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_uvicorn_run(monkeypatch)
    monkeypatch.delenv(SERVER_TOKEN_ENV, raising=False)
    with pytest.raises(SystemExit):
        launcher.main(["--host", "127.0.0.1"])


def test_main_refuses_non_loopback_host(monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_uvicorn_run(monkeypatch)
    monkeypatch.setenv(SERVER_TOKEN_ENV, "env-secret")
    monkeypatch.delenv(launcher.ALLOW_EXTERNAL_BIND_ENV, raising=False)
    with pytest.raises(SystemExit):
        launcher.main(["--host", "0.0.0.0"])


def test_main_allows_external_bind_with_optin(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _stub_uvicorn_run(monkeypatch)
    monkeypatch.setenv(SERVER_TOKEN_ENV, "env-secret")
    monkeypatch.setenv(launcher.ALLOW_EXTERNAL_BIND_ENV, "1")
    launcher.main(["--host", "0.0.0.0"])
    assert captured["host"] == "0.0.0.0"
    # External-bind opt-in (behind a trusted proxy): forwarded headers are honored.
    assert captured["proxy_headers"] is True


def test_meta_server_name_matches_app_server_name() -> None:
    # meta.SERVER_NAME is named locally (not imported) to avoid a circular import
    # with server.app; pin the two equal so they can never drift (the comment in
    # meta.py points here — enforce, don't document).
    from notebooklm.server.app import SERVER_NAME as app_name
    from notebooklm.server.routes.meta import SERVER_NAME as meta_name

    assert meta_name == app_name
