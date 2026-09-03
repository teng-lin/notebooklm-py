"""Consume a real sibling re-mint through one concurrent recovery wave.

A sibling CLI process re-mints the profile from its master token while this
process holds an open client. The master token is then deleted and the live jar
emptied, so the four simultaneous RPCs that follow can only succeed by reloading
the sibling's freshly written ``storage_state.json`` — and must share a single
recovery wave rather than stampeding one reload per request.

Result contract: ``{"before", "after", "reload_calls", "live_cookies",
"sibling_profile_changed"}``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from notebooklm import NotebookLMClient
from notebooklm._auth import session as auth_session

from ._contract import ScenarioResult, require, run_scenario

#: Disposable profile the orchestrator copies the source credentials into.
PROFILE = "mid-session-sibling"


async def scenario() -> ScenarioResult:
    """Re-mint from a sibling process, then recover four concurrent RPCs."""
    reload_calls = 0
    original_reload = auth_session.try_storage_cookie_reload

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
    profile_dir = Path(os.environ["NOTEBOOKLM_HOME"]) / "profiles" / PROFILE
    storage = profile_dir / "storage_state.json"
    before_hash = hashlib.sha256(storage.read_bytes()).hexdigest()

    async with NotebookLMClient.from_storage(profile=PROFILE) as client:
        before = await client.notebooks.list()
        before_ids = tuple(sorted(item.id for item in before))
        sibling = subprocess.run(
            [
                sys.executable,
                "-m",
                "notebooklm.notebooklm_cli",
                "--profile",
                PROFILE,
                "login",
                "--master-token-refresh",
            ],
            env=os.environ.copy(),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        require(
            sibling.returncode == 0,
            f"sibling master-token re-mint failed with exit code {sibling.returncode}",
        )
        after_hash = hashlib.sha256(storage.read_bytes()).hexdigest()
        require(
            after_hash != before_hash,
            "sibling re-mint did not replace profile state",
        )
        (profile_dir / "master_token.json").unlink()
        live = client._require_web_runtime().kernel.get_http_client().cookies
        live.clear()
        recovered = await asyncio.gather(*(client.notebooks.list() for _ in range(4)))
        counts = [len(items) for items in recovered]
        recovered_ids = [tuple(sorted(item.id for item in items)) for items in recovered]
        require(
            recovered_ids == [before_ids] * 4,
            "notebook identity changed after sibling recovery",
        )
        require(
            0 < reload_calls <= 3,
            "concurrent RPCs did not share one recovery wave",
        )
        require(len(live) > 0, "sibling recovery did not restore the live jar")
        return {
            "before": len(before),
            "after": counts,
            "reload_calls": reload_calls,
            "live_cookies": len(live),
            "sibling_profile_changed": True,
        }


def main() -> None:
    """Entry point for ``python -m`` execution by the live-auth matrix."""
    run_scenario(scenario)


if __name__ == "__main__":
    main()
