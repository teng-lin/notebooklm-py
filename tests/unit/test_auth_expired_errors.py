"""Expired cold-start auth must reach public callers as AuthError (#2461)."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pytest_httpx import HTTPXMock

from notebooklm import AuthError, NotebookLMClient
from notebooklm._app.errors import ErrorCategory, classify
from notebooklm._auth.extraction import _LoginRedirectError
from notebooklm._env import get_base_url
from notebooklm.auth import AuthTokens, fetch_tokens, fetch_tokens_with_domains
from notebooklm.notebooklm_cli import cli
from notebooklm.options import AndroidBackendConfig, ClientConfig
from notebooklm.paths import get_storage_path


@pytest.fixture
def expired_auth(monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock) -> Path:
    """Use an isolated profile with a real HTTP redirect and no recovery material."""
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "default")
    monkeypatch.setenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE", "1")
    monkeypatch.setenv("NOTEBOOKLM_HEADLESS_REAUTH", "0")
    monkeypatch.delenv("NOTEBOOKLM_REFRESH_CMD", raising=False)
    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    storage = get_storage_path(profile="default")
    storage.parent.mkdir(parents=True, exist_ok=True)
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": name, "value": "expired", "domain": ".google.com", "path": "/"}
                    for name in ("SID", "__Secure-1PSIDTS", "OSID")
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    httpx_mock.add_response(
        url=f"{get_base_url()}/",
        status_code=302,
        headers={"Location": "https://accounts.google.com/signin?secret=hidden"},
        is_reusable=True,
    )
    httpx_mock.add_response(
        url="https://accounts.google.com/signin?secret=hidden",
        text="<html>Sign in</html>",
        is_reusable=True,
    )
    return storage


@pytest.mark.parametrize("source", ["file", "inline"])
@pytest.mark.parametrize("loader", ["client", "legacy_client", "tokens"])
async def test_public_stored_auth_reports_expired_session(
    expired_auth: Path, monkeypatch: pytest.MonkeyPatch, source: str, loader: str
) -> None:
    """Canonical and legacy stored-auth entrypoints expose the same typed failure."""
    path: Path | None = expired_auth
    if source == "inline":
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", expired_auth.read_text(encoding="utf-8"))
        path = None

    with pytest.raises(AuthError, match="Run 'notebooklm login'") as raised:
        if loader == "client":
            async with NotebookLMClient.from_storage(path) as client:
                await client.notebooks.list()
        elif loader == "legacy_client":
            with pytest.warns(DeprecationWarning, match="Awaiting NotebookLMClient.from_storage"):
                await NotebookLMClient.from_storage(path)
        else:
            with pytest.warns(DeprecationWarning, match="AuthTokens.from_storage"):
                await AuthTokens.from_storage(path)

    assert isinstance(raised.value.__cause__, _LoginRedirectError)
    assert raised.value.recoverable is True
    assert "hidden" not in str(raised.value)
    assert classify(raised.value).category is ErrorCategory.AUTH
    assert classify(raised.value).retriable is False


@pytest.mark.parametrize("source", ["file", "inline"])
@pytest.mark.parametrize("legacy", [False, True])
async def test_android_expired_auth_is_typed_without_recovery_or_writes(
    expired_auth: Path,
    monkeypatch: pytest.MonkeyPatch,
    httpx_mock: HTTPXMock,
    source: str,
    legacy: bool,
) -> None:
    """Android auth loading categorizes redirects without entering Web recovery."""
    before = expired_auth.read_bytes()
    before_stat = expired_auth.stat()
    path: Path | None = expired_auth
    if source == "inline":
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", before.decode("utf-8"))
        path = None
    # Entering Web recovery would run this missing command and retain its
    # RuntimeError, rather than produce the expected AuthError below.
    monkeypatch.setenv("NOTEBOOKLM_REFRESH_CMD", str(expired_auth.parent / "must-not-run"))
    monkeypatch.setenv("NOTEBOOKLM_HEADLESS_REAUTH", "1")
    monkeypatch.delenv("NOTEBOOKLM_DISABLE_KEEPALIVE_POKE")

    with pytest.raises(AuthError, match="Run 'notebooklm login'") as raised:
        context = NotebookLMClient.from_storage(
            path, config=ClientConfig(backend=AndroidBackendConfig()), allow_headless=True
        )
        if legacy:
            with pytest.warns(DeprecationWarning, match="Awaiting NotebookLMClient.from_storage"):
                await context
        else:
            async with context:
                pytest.fail("expired Android auth must fail during loading")

    assert isinstance(raised.value.__cause__, _LoginRedirectError)
    assert raised.value.recoverable is True
    assert "hidden" not in str(raised.value)
    assert classify(raised.value).category is ErrorCategory.AUTH
    assert expired_auth.read_bytes() == before
    after_stat = expired_auth.stat()
    assert (after_stat.st_ino, after_stat.st_mtime_ns) == (
        before_stat.st_ino,
        before_stat.st_mtime_ns,
    )
    requests = httpx_mock.get_requests()
    assert len(requests) == 2
    assert all(request.method == "GET" for request in requests)


@pytest.mark.parametrize("domain_preserving", [False, True])
async def test_public_token_fetch_reports_expired_session(
    expired_auth: Path, domain_preserving: bool
) -> None:
    """Both public token helpers classify exhausted redirects as authentication errors."""
    with pytest.raises(AuthError, match="Run 'notebooklm login'"):
        if domain_preserving:
            await fetch_tokens_with_domains(expired_auth)
        else:
            await fetch_tokens(
                {"SID": "expired", "__Secure-1PSIDTS": "expired"}, storage_path=expired_auth
            )


def test_cli_expired_auth_has_auth_code_and_user_error_exit(expired_auth: Path) -> None:
    """The CLI preserves login guidance and uses its authentication error exit code."""
    result = CliRunner().invoke(cli, ["--profile", "default", "list", "--json"])

    assert result.exit_code == 1, result.output
    payload = json.loads(result.output)
    assert payload["code"] == "AUTH_ERROR"
    assert "Run 'notebooklm login'" in payload["message"]


@pytest.mark.skipif(importlib.util.find_spec("fastmcp") is None, reason="requires MCP extra")
async def test_mcp_expired_auth_preserves_login_hint(expired_auth: Path) -> None:
    """MCP exposes actionable authentication guidance instead of a generic error."""
    fastmcp = pytest.importorskip("fastmcp")
    from fastmcp.exceptions import ToolError

    from notebooklm.mcp.server import create_server

    async with fastmcp.Client(create_server(profile="default", backend="web")) as client:
        assert await client.list_tools()
        with pytest.raises(ToolError) as raised:
            await client.call_tool("notebook_list", {})

    message = str(raised.value)
    assert message.startswith("AUTH:"), message
    assert "Run 'notebooklm login'" in message
    assert "retriable=false" in message
    assert "hint: Re-authenticate and retry." in message
