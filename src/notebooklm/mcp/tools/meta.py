"""Meta MCP tool: ``server_info``.

Reports the package version and a local auth-health probe so an agent can tell,
before any notebook call, whether the server is authenticated. The auth check
reuses the transport-neutral :func:`notebooklm._app.auth_check.run_auth_check`
core (storage-exists / JSON-valid / cookies-present / SID), driven against the
on-disk ``storage_state.json`` the runtime would actually load (no network
round-trip — ``test_fetch`` is off).

``server_info`` takes no notebook argument and is read-only. The storage path +
active profile are resolved via the neutral :mod:`notebooklm.paths` helpers, so
this module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from typing import Any

from fastmcp import Context

from ... import __version__
from ..._app.auth_check import AuthCheckPlan, run_auth_check
from ...paths import get_active_profile, get_storage_path
from .._confirm import READ_ONLY
from .._errors import mcp_errors
from ..server import SERVER_NAME


def _no_env_auth_json() -> str:
    """Inline-auth reader for the neutral core.

    The MCP server authenticates from on-disk storage (``from_storage``), never
    from inline ``NOTEBOOKLM_AUTH_JSON``, so the plan always sets
    ``has_env_auth=False`` and this accessor is never invoked. It is wired only
    to satisfy the core's required keyword.
    """
    return ""  # pragma: no cover - unreachable while has_env_auth is False


def register(mcp: Any) -> None:
    """Register the meta tool on ``mcp``."""

    @mcp.tool(annotations=READ_ONLY)
    async def server_info(ctx: Context) -> dict[str, Any]:
        """Report the server version and local authentication health.

        Takes no arguments. Returns the package ``version`` and an ``auth`` block
        (``authenticated`` / ``storage_exists`` / ``sid_cookie`` / ``profile`` /
        ``storage_path``). Use it to confirm the server is logged in before
        driving notebook tools; if ``authenticated`` is false, run
        ``notebooklm login`` on the server host.
        """
        with mcp_errors():
            profile = get_active_profile()
            storage_path = get_storage_path(profile)
            plan = AuthCheckPlan(
                storage_path=storage_path,
                profile=profile,
                has_env_auth=False,
                has_home_env=False,
                auth_source_label=f"file ({storage_path})",
                test_fetch=False,
                json_output=True,
            )
            result = await run_auth_check(plan, read_env_auth_json=_no_env_auth_json)
            return {
                "server": SERVER_NAME,
                "version": __version__,
                "auth": {
                    "authenticated": result.all_passed,
                    "storage_exists": bool(result.checks.get("storage_exists")),
                    "json_valid": bool(result.checks.get("json_valid")),
                    "cookies_present": bool(result.checks.get("cookies_present")),
                    "sid_cookie": bool(result.checks.get("sid_cookie")),
                    "profile": profile,
                    "storage_path": str(storage_path),
                },
            }
