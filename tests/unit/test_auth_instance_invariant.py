"""Auth identity invariants for client-owned composition."""

from __future__ import annotations

import pytest

from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


@pytest.mark.asyncio
async def test_snapshot_provider_reads_provider_generation_by_identity() -> None:
    """Transport snapshots use the provider generation and preserve public auth."""
    auth = _make_auth()
    client = NotebookLMClient(auth)
    expected = await client._provider.generation()

    assert await client._backend._runtime._transport._snapshot_provider() is expected
    assert client.auth is auth
