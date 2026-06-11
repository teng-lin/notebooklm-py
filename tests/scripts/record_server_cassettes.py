"""Maintainer helper: record the REST-server artifact cassettes against live API.

The server adapter issues some RPCs with a *different request shape* than the CLI
(e.g. the CLI resolves all sources first while the server passes ``source_ids``
through verbatim), so the existing CLI cassettes don't match the server path.
This script records server-shaped cassettes for the artifact lifecycle endpoints
that ``tests/server/test_integration_real_client.py`` then replays.

It drives the **server** through FastAPI's ``TestClient`` with a real
``NotebookLMClient`` (auth from your ``~/.notebooklm`` profile), under VCR record
mode, so the recorded ``f.req`` shapes are exactly what the server emits.

Records (into ``tests/cassettes``):
  * ``server_generate_quiz.yaml``     — POST artifacts (quiz, all sources) → 202 + poll
  * ``server_download_mind_map.yaml`` — POST artifacts/download (mind-map) → bytes
  * ``server_add_file.yaml``          — POST sources/file (multipart upload) → 201

Usage (maintainer, one Google account with a populated generation notebook)::

    NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<uuid> \
        uv run python tests/scripts/record_server_cassettes.py

The notebook must own at least one source (for generation) and one completed
mind-map (for the download leg). After recording, verify the cassettes are clean:

    uv run python tests/scripts/check_cassettes_clean.py --strict --recursive

CI never runs this script. Replay uses scrubbed cassettes + mock auth.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Make the repo root importable so ``tests.vcr_config`` resolves when this script
# is run directly (pytest sets pythonpath=["."]; a plain ``python`` run does not).
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

# Record mode + keepalive-poke disable MUST be set before importing the VCR
# config (its record_mode is computed at import time) and before any client opens
# (so the RotateCookies poke never records an extra interaction).
os.environ["NOTEBOOKLM_VCR_RECORD"] = "1"
os.environ["NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"] = "1"

import io  # noqa: E402
from contextlib import asynccontextmanager  # noqa: E402

from fastapi.testclient import TestClient  # noqa: E402
from tests.vcr_config import notebooklm_vcr  # noqa: E402

from notebooklm.auth import AuthTokens  # noqa: E402
from notebooklm.client import NotebookLMClient  # noqa: E402
from notebooklm.server.app import create_app  # noqa: E402

_TOKEN = "record-token"  # nosec - local only; never leaves this process

#: Content for the recorded file-upload source. Enough text to be a valid source.
_UPLOAD_BYTES = (
    b"NotebookLM REST server VCR upload fixture. This file exercises the multipart "
    b"upload path: spool to a server-owned temp file, then INIT_UPLOAD + PUT bytes "
    b"+ ADD_SOURCE through the real client.\n"
)


def _notebook_id() -> str:
    nb = os.environ.get("NOTEBOOKLM_GENERATION_NOTEBOOK_ID", "").strip()
    if not nb:
        sys.exit("Set NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<uuid> (a notebook with sources).")
    return nb


def _fresh_client() -> TestClient:
    """A TestClient over a fresh real-client app (a TestClient is single-entry)."""

    @asynccontextmanager
    async def factory():
        auth = await AuthTokens.from_storage()  # real profile auth
        async with NotebookLMClient(auth) as client:
            yield client

    os.environ["NOTEBOOKLM_SERVER_TOKEN"] = _TOKEN
    app = create_app(client_factory=factory)
    headers = {"Authorization": f"Bearer {_TOKEN}", "Host": "127.0.0.1"}
    return TestClient(app, headers=headers, raise_server_exceptions=False)


def main() -> int:
    nb = _notebook_id()

    # Generate over ALL sources (no source_ids) — the server now defaults to all
    # sources like the CLI, so a bare generate no longer 502s "… unavailable".
    print("Recording server_generate_quiz.yaml (generate quiz, all sources + one poll)...")
    with notebooklm_vcr.use_cassette("server_generate_quiz.yaml"), _fresh_client() as c:
        gen = c.post(f"/v1/notebooks/{nb}/artifacts", json={"type": "quiz"})
        print("  generate ->", gen.status_code, str(gen.json())[:120])
        task_id = gen.json().get("task_id")
        if task_id:
            poll = c.get(f"/v1/notebooks/{nb}/artifacts/{task_id}")
            print("  poll ->", poll.status_code, str(poll.json())[:120])

    print("Recording server_download_mind_map.yaml (download completed mind-map)...")
    with notebooklm_vcr.use_cassette("server_download_mind_map.yaml"), _fresh_client() as c:
        dl = c.post(f"/v1/notebooks/{nb}/artifacts/download", json={"type": "mind-map"})
        print("  download ->", dl.status_code, f"{len(dl.content)} bytes")

    print("Recording server_add_file.yaml (multipart file upload)...")
    with notebooklm_vcr.use_cassette("server_add_file.yaml"), _fresh_client() as c:
        files = {"file": ("server-vcr-upload.txt", io.BytesIO(_UPLOAD_BYTES), "text/plain")}
        up = c.post(f"/v1/notebooks/{nb}/sources/file", files=files)
        print("  upload ->", up.status_code, str(up.json())[:120])

    print(
        "Done. Now verify: uv run python tests/scripts/check_cassettes_clean.py --strict --recursive"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
