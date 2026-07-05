"""Unit tests for the meta MCP tool (``server_info``).

``server_info`` takes no notebook argument: it reports the package version and a
local auth-health probe (does storage exist / is the SID cookie present). The
probe runs against the neutral ``_app.auth_check`` core, so the test points the
storage path at a temp file via the ``NOTEBOOKLM_HOME`` env var the path resolver
honors.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from fastmcp import Client  # noqa: E402 - after importorskip guard

from notebooklm._version_info import version_string  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import RPCError  # noqa: E402 - after importorskip guard
from notebooklm.mcp.server import create_server  # noqa: E402 - after importorskip guard
from notebooklm.mcp.tools import meta as meta_tool  # noqa: E402 - after importorskip guard
from notebooklm.types import (  # noqa: E402 - after importorskip guard
    AccountLimits,
    UserSettings,
)

from .conftest import AsyncMock  # noqa: E402 - after importorskip guard


def _write_authed_storage() -> None:
    """Write a minimal SID-bearing storage_state.json at the resolved path.

    Resolves the path at call time, so the caller MUST have already pointed
    ``NOTEBOOKLM_HOME`` at a temp dir (``monkeypatch.setenv``) before calling.
    """
    from notebooklm.paths import get_storage_path

    storage_path = get_storage_path()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "x", "domain": ".google.com"},
                    {"name": "HSID", "value": "y", "domain": ".google.com"},
                    {"name": "__Secure-1PSIDTS", "value": "z", "domain": ".google.com"},
                ]
            }
        ),
        encoding="utf-8",
    )


async def test_server_info_reports_version(mcp_call, mock_client, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    result = await mcp_call("server_info")
    assert result.structured_content["version"] == version_string()
    assert result.structured_content["server"] == "notebooklm"


async def test_server_info_auth_missing(mcp_call, mock_client, tmp_path, monkeypatch) -> None:
    """No storage file → auth health reports not authenticated, no exception."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path / "empty"))
    result = await mcp_call("server_info")
    auth = result.structured_content["auth"]
    assert auth["authenticated"] is False
    assert auth["storage_exists"] is False


async def test_server_info_auth_present(mcp_call, mock_client, tmp_path, monkeypatch) -> None:
    """A storage file with an SID cookie → authenticated true."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    # Write a minimal storage_state.json at the resolved path.
    from notebooklm.paths import get_storage_path

    storage_path = get_storage_path()
    storage_path.parent.mkdir(parents=True, exist_ok=True)
    storage_path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "x", "domain": ".google.com"},
                    {"name": "HSID", "value": "y", "domain": ".google.com"},
                    {"name": "__Secure-1PSIDTS", "value": "z", "domain": ".google.com"},
                ]
            }
        ),
        encoding="utf-8",
    )
    result = await mcp_call("server_info")
    auth = result.structured_content["auth"]
    assert auth["storage_exists"] is True
    assert auth["sid_cookie"] is True
    assert auth["authenticated"] is True


async def test_server_info_does_not_leak_absolute_storage_path(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """Security (#1682): the absolute auth storage path must never reach a caller.

    ``server_info`` is readable by any authenticated (possibly remote) client, so
    it must not disclose the server-host OS username / filesystem layout. It returns
    only the ``profile`` name + auth booleans.
    """
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    result = await mcp_call("server_info")
    auth = result.structured_content["auth"]
    # No path-shaped field is exposed...
    assert "storage_path" not in auth
    # ...and the resolved storage directory does not appear anywhere in the payload
    # (guards against a path leaking via any other key, present or future).
    assert str(tmp_path) not in json.dumps(result.structured_content)
    # The non-sensitive identity fields are still present.
    assert "profile" in auth
    assert "authenticated" in auth


async def test_server_info_profile_is_resolved_not_null(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """``auth.profile`` reports the resolved profile, never ``None`` (#1790).

    The MCP server never sets a module-level active profile, so the field used to
    come back ``null`` even on a healthy session — undercutting the docstring that
    points diagnostics at it. It must instead name the profile the auth probe ran
    against (``"default"`` when no named profile is configured).
    """
    from notebooklm import paths

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.setattr(paths, "_active_profile", None)
    result = await mcp_call("server_info")
    assert result.structured_content["auth"]["profile"] == "default"


async def test_server_info_profile_reflects_named_profile(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """A named profile (via ``NOTEBOOKLM_PROFILE``) is surfaced in ``auth.profile``."""
    from notebooklm import paths

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "work")
    monkeypatch.setattr(paths, "_active_profile", None)
    result = await mcp_call("server_info")
    assert result.structured_content["auth"]["profile"] == "work"


async def test_server_info_uses_bound_profile_for_probe(mock_client, tmp_path, monkeypatch) -> None:
    """``create_server(profile=X)`` makes server_info probe that same profile (#1791)."""
    from notebooklm import paths

    seen: dict[str, Any] = {}

    async def _fake_run(plan: Any, *, read_env_auth_json: Any) -> Any:
        seen["profile"] = plan.profile
        seen["storage_path"] = plan.storage_path
        return SimpleNamespace(
            all_passed=True,
            checks={
                "storage_exists": True,
                "json_valid": True,
                "cookies_present": True,
                "sid_cookie": True,
            },
        )

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield mock_client

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.delenv("NOTEBOOKLM_PROFILE", raising=False)
    monkeypatch.setattr(paths, "_active_profile", None)
    monkeypatch.setattr(meta_tool, "run_auth_check", _fake_run)

    async with Client(create_server(profile="work", client_factory=factory)) as client:
        result = await client.call_tool("server_info")

    assert result.structured_content["auth"]["profile"] == "work"
    assert seen == {"profile": "work", "storage_path": paths.get_storage_path("work")}
    assert paths.get_active_profile() is None


async def test_server_info_locks_resolved_profile_for_lifespan(
    mock_client, tmp_path, monkeypatch
) -> None:
    """``profile=None`` resolves once at startup, matching the lifespan-bound client."""
    from notebooklm import paths

    seen: dict[str, Any] = {}

    async def _fake_run(plan: Any, *, read_env_auth_json: Any) -> Any:
        seen["profile"] = plan.profile
        seen["storage_path"] = plan.storage_path
        return SimpleNamespace(
            all_passed=True,
            checks={
                "storage_exists": True,
                "json_valid": True,
                "cookies_present": True,
                "sid_cookie": True,
            },
        )

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[MagicMock]:
        yield mock_client

    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    monkeypatch.setenv("NOTEBOOKLM_PROFILE", "work")
    monkeypatch.setattr(paths, "_active_profile", None)
    monkeypatch.setattr(meta_tool, "run_auth_check", _fake_run)

    async with Client(create_server(client_factory=factory)) as client:
        monkeypatch.setenv("NOTEBOOKLM_PROFILE", "other")
        result = await client.call_tool("server_info")

    assert result.structured_content["auth"]["profile"] == "work"
    assert seen == {"profile": "work", "storage_path": paths.get_storage_path("work")}
    assert paths.get_active_profile() is None


async def test_server_info_default_omits_account(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """Default call (include_account unset) has NO ``account`` key and hits no RPC."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.settings.get_user_settings = AsyncMock()
    result = await mcp_call("server_info")
    assert "account" not in result.structured_content
    mock_client.settings.get_user_settings.assert_not_called()


async def test_server_info_include_account_authenticated(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """include_account=True + live session → account block with identity + limits."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.get_account_email = AsyncMock(return_value="alice@example.com")
    mock_client.get_account_authuser = MagicMock(return_value=1)
    mock_client.settings.get_user_settings = AsyncMock(
        return_value=UserSettings(
            limits=AccountLimits(notebook_limit=100, source_limit=50),
            output_language="ja",
        )
    )
    result = await mcp_call("server_info", {"include_account": True})
    assert result.structured_content["account"] == {
        "email": "alice@example.com",
        "authuser": 1,
        "available": True,
        "notebook_limit": 100,
        "source_limit": 50,
        "output_language": "ja",
    }
    # The live probe is enabled only when the local auth check passed.
    mock_client.get_account_email.assert_awaited_once_with(live_fallback=True)
    # One fetch backs both limits + language (the #1724 dedupe contract).
    mock_client.settings.get_user_settings.assert_awaited_once_with()


async def test_server_info_include_account_unauthenticated(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """include_account=True but no storage → degraded, and no RPC is attempted."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path / "empty"))
    mock_client.settings.get_user_settings = AsyncMock()
    result = await mcp_call("server_info", {"include_account": True})
    assert result.structured_content["account"] == {
        "email": None,
        "authuser": 0,
        "available": False,
        "reason": "not authenticated",
    }
    # Identity is still surfaced, but the live probe is suppressed offline.
    mock_client.get_account_email.assert_awaited_once_with(live_fallback=False)
    mock_client.settings.get_user_settings.assert_not_called()


async def test_server_info_include_account_degrades_on_rpc_error(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """A stale session (local auth passes but the RPC fails) degrades gracefully —
    the account block reports unavailable while version/auth stay intact."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.get_account_email = AsyncMock(return_value="alice@example.com")
    mock_client.get_account_authuser = MagicMock(return_value=1)
    mock_client.settings.get_user_settings = AsyncMock(side_effect=RPCError("session expired"))
    result = await mcp_call("server_info", {"include_account": True})
    account = result.structured_content["account"]
    assert account["available"] is False
    assert "session expired" in account["reason"]
    # Identity survives the quota-read degradation.
    assert account["email"] == "alice@example.com"
    assert account["authuser"] == 1
    # The diagnostic stays useful: version + auth block survive the degradation.
    assert result.structured_content["version"] == version_string()
    assert result.structured_content["auth"]["authenticated"] is True


async def test_server_info_include_account_success_with_null_fields(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """Bare limits + no language is available:True (locks the success-with-null contract)."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.settings.get_user_settings = AsyncMock(
        return_value=UserSettings(limits=AccountLimits(), output_language=None)
    )
    result = await mcp_call("server_info", {"include_account": True})
    # Default mock identity (email None, authuser 0) is tolerated on the success path.
    assert result.structured_content["account"] == {
        "email": None,
        "authuser": 0,
        "available": True,
        "notebook_limit": None,
        "source_limit": None,
        "output_language": None,
    }


async def test_server_info_include_account_degraded_reason_is_scrubbed(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """The degraded reason goes through the same scrubber as every other MCP error,
    so a host filesystem path in the exception never reaches the caller (#1682)."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    leaky = RPCError("auth failed loading /home/secretuser/.notebooklm/storage_state.json")
    mock_client.settings.get_user_settings = AsyncMock(side_effect=leaky)
    result = await mcp_call("server_info", {"include_account": True})
    reason = result.structured_content["account"]["reason"]
    assert result.structured_content["account"]["available"] is False
    # The OS username segment is masked; the rest of the message survives.
    assert "secretuser" not in reason
    assert "/home/***/" in reason
