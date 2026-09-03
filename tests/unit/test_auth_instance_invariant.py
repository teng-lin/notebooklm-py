"""Auth identity invariants for client-owned composition."""

from __future__ import annotations

import pytest

from notebooklm._web.transport.request_types import AuthSnapshot
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

    async with client:
        lifecycle = client._collaborators.lifecycle
        generation = client._collaborators.call_supervisor._current
        expected_epoch = lifecycle._epoch
        assert lifecycle.is_open()
        assert expected_epoch > 0
        assert generation is not None and generation.epoch == expected_epoch

        async def snapshot(*, auth: AuthTokens, expected_epoch: int | None = None) -> AuthSnapshot:
            assert expected_epoch == lifecycle._epoch
            captured["auth"] = auth
            return AuthSnapshot(
                csrf_token=auth.csrf_token,
                session_id=auth.session_id,
                authuser=auth.authuser,
                account_email=auth.account_email,
            )

        client._web_runtime.auth_coord.snapshot = snapshot  # type: ignore[method-assign]

        await client._web_runtime.composed.transport._snapshot_provider(expected_epoch)

    assert captured["auth"] is client._auth
