"""Force the real Layer-4 rung after live *and* disk cookies become unusable.

Empties the on-disk ``storage_state.json`` cookie list and the live jar, so the
storage-reload rung has nothing to give and only a master-token re-mint can
restore the session. Also asserts the recovery repaired the persisted profile,
not merely the in-memory jar.

Result contract: ``{"before", "after", "master_token_calls", "persisted_cookies",
"live_cookies"}``.
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
PROFILE = "mid-session-master"


async def scenario() -> ScenarioResult:
    """Drive one mid-session master-token re-mint and verify it repaired disk."""
    master_calls = 0
    original_master = auth_session._try_master_token_reauth

    async def tracked_master(*args: Any, **kwargs: Any) -> bool:
        nonlocal master_calls
        master_calls += 1
        return await original_master(*args, **kwargs)

    auth_session._try_master_token_reauth = tracked_master
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
        persisted = json.loads(storage.read_text(encoding="utf-8"))
        require(
            before_ids == after_ids,
            "notebook identity changed after master-token recovery",
        )
        require(
            master_calls == 1,
            "mid-session Layer-4 recovery was not exercised once",
        )
        require(
            persisted.get("cookies"),
            "master-token recovery did not repair storage",
        )
        require(len(live) > 0, "master-token recovery did not restore live jar")
        return {
            "before": len(before),
            "after": len(after),
            "master_token_calls": master_calls,
            "persisted_cookies": len(persisted["cookies"]),
            "live_cookies": len(live),
        }


def main() -> None:
    """Entry point for ``python -m`` execution by the live-auth matrix."""
    run_scenario(scenario)


if __name__ == "__main__":
    main()
