"""Unit tests for ``ChatAPI.delete_conversation``.

Pins the wire contract for the ``J7Gthc`` RPC (params shape, source_path)
and the local-cache invariant: the per-instance cache is purged only when
the server-side delete succeeds.

Wave 8 of the session-decoupling plan (ADR-0014 Rule 2 Corollary): the
chat-local ``ChatRuntime`` Protocol composite was deleted in favour of
direct constructor injection of the underlying collaborators. These
tests use narrow ``MagicMock(spec=...)`` fakes for the two collaborators
``delete_conversation`` actually touches: the ``rpc`` dispatcher and the
``loop_guard`` (whose ``assert_bound_loop`` is invoked up front to reject
cross-loop misuse before the per-conversation lock is acquired, #1225).
The remaining two collaborators (transport, reqid) are unused by this
method and are mocked without specs.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._chat import ChatAPI
from notebooklm._runtime.contracts import LoopGuard, RpcCaller
from notebooklm.rpc import RPCMethod


@pytest.fixture
def mock_rpc() -> MagicMock:
    """Narrow ``RpcCaller`` fake — the only collaborator this surface uses.

    Constructor injection via ``ChatAPI(rpc=..., transport=..., reqid=...,
    loop_guard=...)`` satisfies ADR-0007 (no post-hoc attribute assignment
    of an ``AsyncMock`` onto ``rpc_call``); the ``AsyncMock`` is wired
    into the ``MagicMock(spec=...)`` via its constructor so the ADR-0007
    meta-lint stays clean.
    """
    return MagicMock(spec=RpcCaller, rpc_call=AsyncMock(return_value=None))


@pytest.fixture
def api(mock_rpc: MagicMock) -> ChatAPI:
    return ChatAPI(
        rpc=mock_rpc,
        transport=MagicMock(),
        reqid=MagicMock(),
        # ``delete_conversation`` calls ``loop_guard.assert_bound_loop()`` up
        # front (#1225), so the guard needs a ``LoopGuard`` spec — a bare
        # ``MagicMock`` rejects ``assert_*`` attribute access as a typo guard.
        loop_guard=MagicMock(spec=LoopGuard),
    )


class TestDeleteConversation:
    @pytest.mark.asyncio
    async def test_sends_expected_payload(self, api: ChatAPI, mock_rpc: MagicMock) -> None:
        assert await api.delete_conversation("nb_xyz", "conv_abc") is None

        # Pin the load-bearing args only; the capability adapter's wiring
        # defaults (allow_null, operation_variant, etc.) are covered elsewhere.
        mock_rpc.rpc_call.assert_awaited_once()
        args, kwargs = mock_rpc.rpc_call.call_args
        assert args == (RPCMethod.DELETE_CONVERSATION, [[], "conv_abc", None, 1])
        assert kwargs["source_path"] == "/notebook/nb_xyz"

    @pytest.mark.asyncio
    async def test_clears_local_cache_for_deleted_conversation(
        self, api: ChatAPI, mock_rpc: MagicMock
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
        self, api: ChatAPI, mock_rpc: MagicMock
    ) -> None:
        # Seed BEFORE arming the failure so the test detects a regression
        # that clears the cache pre- or mid-failure. Seeding after would
        # mask exactly the bug the test is meant to catch.
        api._cache.cache_conversation_turn("conv_abc", "Q1?", "A1.", turn_number=1)
        mock_rpc.rpc_call.side_effect = RuntimeError("server 500")

        with pytest.raises(RuntimeError, match="server 500"):
            await api.delete_conversation("nb_xyz", "conv_abc")

        # Cache must survive a failed delete so the caller can retry.
        assert api._cache.get_cached_conversation("conv_abc"), (
            "cache cleared despite RPC failure — retry path now broken"
        )
