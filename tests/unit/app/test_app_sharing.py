"""Unit tests for the transport-neutral ``notebooklm._app.sharing`` core.

These pin the Click-free sharing workflows at the ``_app`` boundary with a
``MagicMock`` client + an injected partial-id resolver (the CLI normally
injects ``cli.resolve.resolve_notebook_id``):

* ``status`` / ``public`` / ``view-level`` / ``add`` / ``update`` / ``remove``
  executors resolve the notebook id and drive the right ``client.sharing`` RPC,
* ``set_view_level`` returns the ``(resolved_id, status)`` pair the CLI keys its
  envelope off,
* the executors take **already-parsed** :class:`SharePermission` /
  :class:`ShareViewLevel` enums — the ``str -> enum`` Click-``Choice`` parse
  stays in ``cli/share_cmd.py`` (asserted in ``tests/unit/cli/test_share.py``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.sharing import (
    execute_share_add_user,
    execute_share_remove_user,
    execute_share_set_public,
    execute_share_set_view_level,
    execute_share_status,
    execute_share_update_user,
)
from notebooklm.types import (
    SharePermission,
    ShareStatus,
    ShareViewLevel,
)


def _client() -> MagicMock:
    client = MagicMock()
    client.sharing = MagicMock()
    return client


async def _resolve_nb(_client, nb_id, *, json_output=False):
    return f"full_{nb_id}"


def _status(**overrides) -> ShareStatus:
    base: dict = {
        "notebook_id": "full_nb_part",
        "is_public": False,
        "access": "restricted",
        "view_level": ShareViewLevel.FULL_NOTEBOOK,
        "shared_users": [],
        "share_url": None,
    }
    base.update(overrides)
    return ShareStatus(**base)


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_share_status_resolves_then_fetches() -> None:
    client = _client()
    status = _status()
    client.sharing.get_status = AsyncMock(return_value=status)

    result = await execute_share_status(client, "nb_part", resolve_notebook_id=_resolve_nb)

    assert result is status
    client.sharing.get_status.assert_awaited_once_with("full_nb_part")


# ---------------------------------------------------------------------------
# set public
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_share_set_public_passes_enable_flag() -> None:
    client = _client()
    status = _status(is_public=True)
    client.sharing.set_public = AsyncMock(return_value=status)

    result = await execute_share_set_public(
        client, "nb_part", True, resolve_notebook_id=_resolve_nb
    )

    assert result is status
    client.sharing.set_public.assert_awaited_once_with("full_nb_part", True)


# ---------------------------------------------------------------------------
# set view level — returns the (resolved_id, status) pair
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_share_set_view_level_returns_id_and_status() -> None:
    client = _client()
    status = _status(view_level=ShareViewLevel.CHAT_ONLY)
    client.sharing.set_view_level = AsyncMock(return_value=status)

    resolved_id, result_status = await execute_share_set_view_level(
        client, "nb_part", ShareViewLevel.CHAT_ONLY, resolve_notebook_id=_resolve_nb
    )

    assert resolved_id == "full_nb_part"
    assert result_status is status
    client.sharing.set_view_level.assert_awaited_once_with("full_nb_part", ShareViewLevel.CHAT_ONLY)


# ---------------------------------------------------------------------------
# add user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_share_add_user_threads_permission_and_returns_id() -> None:
    client = _client()
    client.sharing.add_user = AsyncMock(return_value=None)

    resolved_id = await execute_share_add_user(
        client,
        "nb_part",
        "user@example.com",
        permission=SharePermission.EDITOR,
        notify=True,
        welcome_message="hi",
        resolve_notebook_id=_resolve_nb,
    )

    assert resolved_id == "full_nb_part"
    client.sharing.add_user.assert_awaited_once_with(
        "full_nb_part",
        "user@example.com",
        permission=SharePermission.EDITOR,
        notify=True,
        welcome_message="hi",
    )


# ---------------------------------------------------------------------------
# update user
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_share_update_user_passes_permission_positionally() -> None:
    client = _client()
    client.sharing.update_user = AsyncMock(return_value=None)

    resolved_id = await execute_share_update_user(
        client,
        "nb_part",
        "user@example.com",
        SharePermission.VIEWER,
        resolve_notebook_id=_resolve_nb,
    )

    assert resolved_id == "full_nb_part"
    client.sharing.update_user.assert_awaited_once_with(
        "full_nb_part", "user@example.com", SharePermission.VIEWER
    )


# ---------------------------------------------------------------------------
# remove user — no resolver (takes the full id directly)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_execute_share_remove_user_delegates() -> None:
    client = _client()
    client.sharing.remove_user = AsyncMock(return_value=None)

    # remove_user takes the full id directly (no resolver) and raises on failure.
    await execute_share_remove_user(client, "nb_1", "user@example.com")

    client.sharing.remove_user.assert_awaited_once_with("nb_1", "user@example.com")
