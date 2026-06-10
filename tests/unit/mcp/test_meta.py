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
