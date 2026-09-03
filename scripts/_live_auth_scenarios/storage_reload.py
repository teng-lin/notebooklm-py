"""Strict storage-only mid-session recovery (#2161).

Proves the Layer-2 *storage reload* rung is the one that recovers a client whose
live cookie jar was emptied mid-session: every external rung (refresh command,
headless re-auth, master token) is replaced with a poisoned stub, so the cell
fails loudly instead of quietly passing on a different rung.

The orchestrator additionally deletes ``master_token.json`` from the disposable
profile and blanks ``NOTEBOOKLM_REFRESH_BROWSER`` / ``NOTEBOOKLM_REFRESH_CMD`` /
``NOTEBOOKLM_REFRESH_CMD_MIDSESSION`` before launching this module.

Result contract: ``{"before", "after", "reload_calls", "live_cookies"}``.
"""

from __future__ import annotations

from typing import Any

from notebooklm import NotebookLMClient
from notebooklm._auth import session as auth_session

from ._contract import ScenarioError, ScenarioResult, require, run_scenario

#: Disposable profile the orchestrator copies the source credentials into.
PROFILE = "mid-session-storage"


async def scenario() -> ScenarioResult:
    """Recover an emptied live jar from disk and prove no other rung ran."""
    reload_calls = 0
    original_reload = getattr(auth_session, "try_storage_cookie_reload", None)
    if original_reload is None:
        # The rung this cell exists to prove is gone; fail on the missing seam
        # rather than on the "rung was not exercised" symptom further down.
        raise ScenarioError("storage reload helper unavailable")

    async def tracked_reload(*args: Any, **kwargs: Any) -> bool:
        nonlocal reload_calls
        reload_calls += 1
        return await original_reload(*args, **kwargs)

    async def forbidden(*args: object, **kwargs: object) -> bool:
        raise AssertionError("external recovery rung reached")

    auth_session.try_storage_cookie_reload = tracked_reload
    auth_session._try_refresh_cmd_reauth = forbidden
    auth_session._try_headless_reauth = forbidden
    auth_session._try_master_token_reauth = forbidden

    async with NotebookLMClient.from_storage(profile=PROFILE) as c:
        before = await c.notebooks.list()
        before_ids = tuple(sorted(item.id for item in before))
        live = c._require_web_runtime().kernel.get_http_client().cookies
        live.clear()
        after = await c.notebooks.list()
        after_ids = tuple(sorted(item.id for item in after))
        require(reload_calls > 0, "storage reload rung was not exercised")
        require(len(live) > 0, "storage reload did not restore the live jar")
        require(before_ids == after_ids, "notebook identity changed after recovery")
        return {
            "before": len(before),
            "after": len(after),
            "reload_calls": reload_calls,
            "live_cookies": len(live),
        }


def main() -> None:
    """Entry point for ``python -m scripts._live_auth_scenarios.storage_reload``."""
    run_scenario(scenario)


if __name__ == "__main__":
    main()
