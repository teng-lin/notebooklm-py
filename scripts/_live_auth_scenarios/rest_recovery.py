"""Drive REST through live-jar recovery and degraded-startup lazy binding.

Two halves, both against a real ASGI app over ``httpx.ASGITransport``:

1. *mid-session* — a served request recovers after the live cookie jar of the
   bound client is emptied.
2. *stale startup* — the server starts with an unusable profile (no cookies, the
   master token withheld), must enter degraded startup with no bound client,
   and must lazily bind a working client once a sibling CLI re-mints the
   profile.

Result contract: ``{"before", "after_live_reload", "after_stale_startup_rebind",
"stale_startup_rebound"}``.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx

from notebooklm import NotebookLMClient
from notebooklm.server.app import create_app

from ._contract import ScenarioResult, require, run_scenario

#: Disposable profile that starts out healthy and loses only its live jar.
PROFILE = "rest-live"
#: Disposable profile that is made unusable before the server starts.
STALE_PROFILE = "rest-stale"
#: Must match ``NOTEBOOKLM_SERVER_TOKEN`` in the orchestrator's phase overrides.
TOKEN = "matrix-rest-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Host": "127.0.0.1"}


def notebook_ids(response: httpx.Response) -> tuple[str, ...]:
    """Return the sorted notebook ids of a ``/v1/notebooks`` response."""
    return tuple(sorted(item["id"] for item in response.json()["notebooks"]))


async def mid_session() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Serve one request, empty the bound client's jar, serve another."""
    holder: dict[str, NotebookLMClient] = {}

    @contextlib.asynccontextmanager
    async def factory() -> AsyncIterator[NotebookLMClient]:
        async with NotebookLMClient.from_storage(profile=PROFILE, keepalive=600.0) as client:
            holder["client"] = client
            yield client

    app = create_app(profile=PROFILE, client_factory=factory)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(
            app=app, client=("127.0.0.1", 5555), raise_app_exceptions=False
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            before = await http.get("/v1/notebooks", headers=HEADERS)
            require(before.status_code == 200, before.text[-1000:])
            holder["client"]._require_web_runtime().kernel.get_http_client().cookies.clear()
            after = await http.get("/v1/notebooks", headers=HEADERS)
            require(after.status_code == 200, after.text[-1000:])
            return notebook_ids(before), notebook_ids(after)


async def stale_startup() -> tuple[str, ...]:
    """Start degraded on an unusable profile, then bind after a sibling re-mint."""
    profile_dir = Path(os.environ["NOTEBOOKLM_HOME"]) / "profiles" / STALE_PROFILE
    storage = profile_dir / "storage_state.json"
    token = profile_dir / "master_token.json"
    held_token = profile_dir / "master_token.matrix-held"
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
    require(token.is_file(), f"{STALE_PROFILE} profile has no master_token.json")
    token.replace(held_token)

    app = create_app(profile=STALE_PROFILE)
    async with app.router.lifespan_context(app):
        require(
            app.state.notebooklm.client is None,
            "REST server did not enter degraded startup",
        )
        held_token.replace(token)
        sibling = subprocess.run(
            [
                sys.executable,
                "-m",
                "notebooklm.notebooklm_cli",
                "--profile",
                STALE_PROFILE,
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
            f"stale-profile master-token re-mint failed with exit code {sibling.returncode}",
        )
        transport = httpx.ASGITransport(
            app=app, client=("127.0.0.1", 5555), raise_app_exceptions=False
        )
        async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1") as http:
            response = await http.get("/v1/notebooks", headers=HEADERS)
        require(response.status_code == 200, response.text[-1000:])
        require(
            app.state.notebooklm.client is not None,
            "REST server did not bind a recovered client",
        )
        return notebook_ids(response)


async def scenario() -> ScenarioResult:
    """Run both REST recovery halves and require one stable notebook identity."""
    before_ids, after_ids = await mid_session()
    rebound_ids = await stale_startup()
    require(
        before_ids == after_ids == rebound_ids,
        "REST recovery changed notebook identity",
    )
    payload: dict[str, Any] = {
        "before": len(before_ids),
        "after_live_reload": len(after_ids),
        "after_stale_startup_rebind": len(rebound_ids),
        "stale_startup_rebound": True,
    }
    return payload


def main() -> None:
    """Entry point for ``python -m`` execution by the live-auth matrix."""
    run_scenario(scenario)


if __name__ == "__main__":
    main()
