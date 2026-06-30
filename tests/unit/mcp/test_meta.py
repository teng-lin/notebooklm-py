"""Unit tests for the meta MCP tool (``server_info``).

``server_info`` takes no notebook argument: it reports the package version and a
local auth-health probe (does storage exist / is the SID cookie present). The
probe runs against the neutral ``_app.auth_check`` core, so the test points the
storage path at a temp file via the ``NOTEBOOKLM_HOME`` env var the path resolver
honors.
"""

from __future__ import annotations

import json

import pytest

# Skip cleanly when the `mcp` extra (fastmcp) is absent; see conftest.py.
pytest.importorskip("fastmcp")

from notebooklm import __version__  # noqa: E402 - after importorskip guard
from notebooklm.exceptions import RPCError  # noqa: E402 - after importorskip guard
from notebooklm.types import AccountLimits, AccountTier  # noqa: E402 - after importorskip guard

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
    assert result.structured_content["version"] == __version__
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


async def test_server_info_default_omits_account(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """Default call (include_account unset) has NO ``account`` key and hits no RPC."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.settings.get_account_limits = AsyncMock()
    mock_client.settings.get_account_tier = AsyncMock()
    result = await mcp_call("server_info")
    assert "account" not in result.structured_content
    mock_client.settings.get_account_limits.assert_not_called()
    mock_client.settings.get_account_tier.assert_not_called()


async def test_server_info_include_account_authenticated(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """include_account=True + live session → account block with tier + limits."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.settings.get_account_limits = AsyncMock(
        return_value=AccountLimits(notebook_limit=100, source_limit=50)
    )
    mock_client.settings.get_account_tier = AsyncMock(
        return_value=AccountTier(tier="NOTEBOOKLM_TIER_PRO", plan_name="Pro")
    )
    result = await mcp_call("server_info", {"include_account": True})
    assert result.structured_content["account"] == {
        "available": True,
        "tier": "NOTEBOOKLM_TIER_PRO",
        "plan_name": "Pro",
        "notebook_limit": 100,
        "source_limit": 50,
    }


async def test_server_info_include_account_unauthenticated(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """include_account=True but no storage → degraded, and no RPC is attempted."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path / "empty"))
    mock_client.settings.get_account_limits = AsyncMock()
    mock_client.settings.get_account_tier = AsyncMock()
    result = await mcp_call("server_info", {"include_account": True})
    assert result.structured_content["account"] == {
        "available": False,
        "reason": "not authenticated",
    }
    mock_client.settings.get_account_limits.assert_not_called()
    mock_client.settings.get_account_tier.assert_not_called()


async def test_server_info_include_account_degrades_on_rpc_error(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """A stale session (local auth passes but the RPC fails) degrades gracefully —
    the account block reports unavailable while version/auth stay intact."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.settings.get_account_limits = AsyncMock(side_effect=RPCError("session expired"))
    mock_client.settings.get_account_tier = AsyncMock()
    result = await mcp_call("server_info", {"include_account": True})
    account = result.structured_content["account"]
    assert account["available"] is False
    assert "session expired" in account["reason"]
    # The diagnostic stays useful: version + auth block survive the degradation.
    assert result.structured_content["version"] == __version__
    assert result.structured_content["auth"]["authenticated"] is True


async def test_server_info_include_account_degrades_when_tier_rpc_raises(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """The tier RPC failing (not just the limits RPC) also degrades gracefully —
    the two reads run concurrently, so either one raising must be caught."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.settings.get_account_limits = AsyncMock(return_value=AccountLimits())
    mock_client.settings.get_account_tier = AsyncMock(side_effect=RPCError("tier lookup failed"))
    result = await mcp_call("server_info", {"include_account": True})
    assert result.structured_content["account"]["available"] is False
    assert "tier lookup failed" in result.structured_content["account"]["reason"]


async def test_server_info_include_account_tier_none_is_available(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """tier RPC is best-effort: a bare AccountTier() (tier None) is available:True,
    not an error (locks the documented success-with-null-tier contract)."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    mock_client.settings.get_account_limits = AsyncMock(return_value=AccountLimits())
    mock_client.settings.get_account_tier = AsyncMock(return_value=AccountTier())
    result = await mcp_call("server_info", {"include_account": True})
    assert result.structured_content["account"] == {
        "available": True,
        "tier": None,
        "plan_name": None,
        "notebook_limit": None,
        "source_limit": None,
    }


async def test_server_info_include_account_degraded_reason_is_scrubbed(
    mcp_call, mock_client, tmp_path, monkeypatch
) -> None:
    """The degraded reason goes through the same scrubber as every other MCP error,
    so a host filesystem path in the exception never reaches the caller (#1682)."""
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    _write_authed_storage()
    leaky = RPCError("auth failed loading /home/secretuser/.notebooklm/storage_state.json")
    mock_client.settings.get_account_limits = AsyncMock(side_effect=leaky)
    mock_client.settings.get_account_tier = AsyncMock()
    result = await mcp_call("server_info", {"include_account": True})
    reason = result.structured_content["account"]["reason"]
    assert result.structured_content["account"]["available"] is False
    # The OS username segment is masked; the rest of the message survives.
    assert "secretuser" not in reason
    assert "/home/***/" in reason
