"""Optional browser-backed mid-session recovery (Layer-2.5 refresh command).

The orchestrator points ``NOTEBOOKLM_REFRESH_CMD`` at
``examples/refresh_browser_cookies.py`` and opts the rung in mid-session, then
empties both the persisted cookie list and the live jar. Only the refresh
command can recover from that, and it must be invoked exactly once.

Result contract: ``{"before", "after", "refresh_command_calls", "live_cookies"}``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from notebooklm import NotebookLMClient
from notebooklm._auth import session as auth_session

from ._contract import ScenarioResult, require, run_scenario

#: Disposable profile the orchestrator copies the source credentials into.
PROFILE = "mid-session-browser"


async def scenario() -> ScenarioResult:
    """Recover a fully invalidated session through the refresh command once."""
    refresh_calls = 0
    original_refresh = auth_session._try_refresh_cmd_reauth

    async def tracked_refresh(*args: Any, **kwargs: Any) -> bool:
        nonlocal refresh_calls
        refresh_calls += 1
        return await original_refresh(*args, **kwargs)

    auth_session._try_refresh_cmd_reauth = tracked_refresh
    profile_dir = Path(os.environ["NOTEBOOKLM_HOME"]) / "profiles" / PROFILE
    storage = profile_dir / "storage_state.json"

    async with NotebookLMClient.from_storage(profile=PROFILE) as client:
        before = await client.notebooks.list()
        before_ids = tuple(sorted(item.id for item in before))
        state = json.loads(storage.read_text(encoding="utf-8"))
        state["cookies"] = []
        replacement = storage.with_suffix(".matrix-tmp")
        replacement.write_text(json.dumps(state), encoding="utf-8")
        # Match ``atomic_write_json``'s 0o600 (``_atomic_io.py``): a bare
        # ``write_text`` lands at the process umask (0o644), and ``os.replace``
        # carries the temp file's mode onto the profile. The disposable home is
        # already 0o700, so this is defence in depth rather than a live hole,
        # but a credential file must never widen on any path.
        replacement.chmod(0o600)
        os.replace(replacement, storage)
        live = client._require_web_runtime().kernel.get_http_client().cookies
        live.clear()
        after = await client.notebooks.list()
        after_ids = tuple(sorted(item.id for item in after))
        require(
            before_ids == after_ids,
            "notebook identity changed after browser recovery",
        )
        require(
            refresh_calls == 1,
            "browser refresh command rung was not exercised once",
        )
        require(len(live) > 0, "browser recovery did not restore live jar")
        return {
            "before": len(before),
            "after": len(after),
            "refresh_command_calls": refresh_calls,
            "live_cookies": len(live),
        }


def main() -> None:
    """Entry point for ``python -m`` execution by the live-auth matrix."""
    run_scenario(scenario)


if __name__ == "__main__":
    main()
