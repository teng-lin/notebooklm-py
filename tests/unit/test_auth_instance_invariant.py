"""Auth identity invariants for client-owned composition."""

from __future__ import annotations

import pytest

from notebooklm._request_types import AuthSnapshot
from notebooklm.auth import AuthTokens
from tests._helpers.client_factory import build_client_shell_for_tests


def _make_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "x", "__Secure-1PSIDTS": "y"},
        csrf_token="csrf",
        session_id="sid",
    )


@pytest.mark.asyncio
async def test_snapshot_provider_captures_client_auth_by_identity() -> None:
    """Transport snapshots must pass the identical client-owned auth object."""
    auth = _make_auth()
    client = build_client_shell_for_tests(auth)
    captured: dict[str, AuthTokens] = {}

    async def snapshot(*, auth: AuthTokens) -> AuthSnapshot:
        captured["auth"] = auth
        return AuthSnapshot(
            csrf_token=auth.csrf_token,
            session_id=auth.session_id,
            authuser=auth.authuser,
            account_email=auth.account_email,
        )

    client._collaborators.auth_coord.snapshot = snapshot  # type: ignore[method-assign]

    await client._composed.transport._snapshot_provider()

    assert captured["auth"] is client._auth
