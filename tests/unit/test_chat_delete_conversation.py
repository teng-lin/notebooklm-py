"""Unit tests for ``ChatAPI.delete_conversation``.

Pins the wire contract for the ``J7Gthc`` RPC (params shape, source_path)
and the local-cache invariant: the per-instance cache is purged only when
the server-side delete succeeds.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._chat import ChatAPI
from notebooklm.rpc import RPCMethod


@pytest.fixture
def mock_core() -> MagicMock:
    core = MagicMock()
    core.rpc_call = AsyncMock(return_value=None)
    return core


@pytest.fixture
def api(mock_core: MagicMock) -> ChatAPI:
    return ChatAPI(mock_core)


class TestDeleteConversation:
    @pytest.mark.asyncio
    async def test_sends_expected_payload(self, api: ChatAPI, mock_core: MagicMock) -> None:
        assert await api.delete_conversation("nb_xyz", "conv_abc") is True

        # Pin the load-bearing args only; the capability adapter's wiring
        # defaults (allow_null, operation_variant, etc.) are covered elsewhere.
        mock_core.rpc_call.assert_awaited_once()
        args, kwargs = mock_core.rpc_call.call_args
        assert args == (RPCMethod.DELETE_CONVERSATION, [[], "conv_abc", None, 1])
        assert kwargs["source_path"] == "/notebook/nb_xyz"

    @pytest.mark.asyncio
    async def test_clears_local_cache_for_deleted_conversation(
        self, api: ChatAPI, mock_core: MagicMock
    ) -> None:
        api._cache.cache_conversation_turn("conv_abc", "Q1?", "A1.", turn_number=1)
        api._cache.cache_conversation_turn("conv_other", "Q?", "A.", turn_number=1)
        assert api._cache.get_cached_conversation("conv_abc"), "precondition: cache seeded"

        await api.delete_conversation("nb_xyz", "conv_abc")

        assert api._cache.get_cached_conversation("conv_abc") == []
        assert api._cache.get_cached_conversation("conv_other"), (
            "unrelated cached conversations must survive a targeted delete"
        )

    @pytest.mark.asyncio
    async def test_rpc_failure_propagates_and_cache_survives(
        self, api: ChatAPI, mock_core: MagicMock
    ) -> None:
        # Seed BEFORE arming the failure so the test detects a regression
        # that clears the cache pre- or mid-failure. Seeding after would
        # mask exactly the bug the test is meant to catch.
        api._cache.cache_conversation_turn("conv_abc", "Q1?", "A1.", turn_number=1)
        mock_core.rpc_call.side_effect = RuntimeError("server 500")

        with pytest.raises(RuntimeError, match="server 500"):
            await api.delete_conversation("nb_xyz", "conv_abc")

        # Cache must survive a failed delete so the caller can retry.
        assert api._cache.get_cached_conversation("conv_abc"), (
            "cache cleared despite RPC failure — retry path now broken"
        )
