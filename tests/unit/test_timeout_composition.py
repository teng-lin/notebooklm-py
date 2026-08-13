"""The #2205 per-RPC timeout composition contract.

Two RPCs carry a built-in read window that is longer than the shared 30 s
metadata one: chat (``DEFAULT_CHAT_TIMEOUT``) and IMPORT_RESEARCH (the #2187
batch-scaled window). Both used to *replace* the client's configured
``timeout=`` outright, so ``NotebookLMClient(auth, timeout=600)`` silently got
180 s for chat and <=240 s for IMPORT_RESEARCH with no opt-out.

The rule these tests pin, bottom to top:

1. ``compose_builtin_read_timeout`` — a built-in window may only ever *lengthen*
   the configured base timeout.
2. ``resolve_chat_read_timeout`` — only the untouched default composes; an
   explicit ``chat_timeout`` (including one *below* the base, for deliberately
   fast failure, and including ``None``) is honored verbatim. This is why the
   kwarg defaults to a sentinel: a plain ``= DEFAULT_CHAT_TIMEOUT`` default
   cannot tell "left alone" from "explicitly asked for 180".
3. The constructor wiring, and finally the effective ``read`` timeout on the
   real httpx request — the only layer that proves the budget actually reaches
   the wire rather than being recomputed on the way down.
"""

from __future__ import annotations

import json
import re

import pytest

from notebooklm import NotebookLMClient
from notebooklm._research_import import _import_research_read_timeout
from notebooklm._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_CHAT_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT,
    DEFAULT_TIMEOUT,
    compose_builtin_read_timeout,
    resolve_chat_read_timeout,
)
from notebooklm.rpc import RPCMethod


def _answer_response_body() -> bytes:
    """A minimal valid streamed chat response (shape mirrors ``test_chat_ask_invariants``).

    The server-assigned conversation id at ``first[2][0]`` is mandatory —
    ``ChatAPI.ask`` raises ``ChatError`` without it (issue #659).
    """
    inner_json = json.dumps(
        [["The answer is long enough to be accepted.", None, ["server-conv", 12345], None, [1]]]
    )
    chunk_json = json.dumps([["wrb.fr", None, inner_json]])
    return f")]}}'\n{len(chunk_json)}\n{chunk_json}\n".encode()


def _read_timeout_of(request) -> float | None:
    """The effective httpx read timeout of a recorded request.

    ``httpx`` stamps the resolved timeout onto ``request.extensions`` when it
    sends, so this reads what the transport was actually told — not what some
    intermediate layer intended.
    """
    return request.extensions["timeout"]["read"]


class TestComposeBuiltinReadTimeout:
    def test_builtin_window_wins_when_base_timeout_is_shorter(self):
        assert compose_builtin_read_timeout(180.0, 30.0) == 180.0

    def test_configured_base_wins_when_it_is_longer(self):
        # The reported bug: a 600 s budget must not be cut back to the constant.
        assert compose_builtin_read_timeout(180.0, 600.0) == 600.0

    def test_equal_values_compose_to_the_same_window(self):
        assert compose_builtin_read_timeout(180.0, 180.0) == 180.0

    def test_infinite_base_timeout_stays_infinite(self):
        # httpx reads ``None`` as "no timeout"; a finite constant must not
        # re-impose a ceiling on a client that explicitly opted out.
        assert compose_builtin_read_timeout(180.0, None) is None


class TestResolveChatReadTimeout:
    def test_untouched_default_keeps_the_builtin_window_on_a_default_client(self):
        assert resolve_chat_read_timeout(AUTO_READ_TIMEOUT, DEFAULT_TIMEOUT) == DEFAULT_CHAT_TIMEOUT

    def test_untouched_default_lifts_to_a_larger_configured_budget(self):
        assert resolve_chat_read_timeout(AUTO_READ_TIMEOUT, 600.0) == 600.0

    def test_explicit_shorter_value_is_honored_over_a_larger_base(self):
        # The reason the rule is not a blanket ``max()``: a caller who wants
        # chat to fail fast must still get fast failure.
        assert resolve_chat_read_timeout(10.0, 600.0) == 10.0

    def test_explicit_none_inherits_the_base_timeout_verbatim(self):
        assert resolve_chat_read_timeout(None, 600.0) is None

    def test_explicit_value_equal_to_the_builtin_default_is_not_lifted(self):
        # Distinguishable from the untouched default only because the kwarg
        # defaults to a sentinel rather than to the constant itself.
        assert resolve_chat_read_timeout(DEFAULT_CHAT_TIMEOUT, 600.0) == DEFAULT_CHAT_TIMEOUT


class TestImportResearchReadTimeoutComposition:
    def test_batch_scaling_survives_on_a_default_client(self):
        # #2203's scaling behavior is correct and must not regress.
        assert _import_research_read_timeout(3, base_timeout=DEFAULT_TIMEOUT) == (
            DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + 3 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )

    def test_larger_configured_budget_floors_the_scaled_window(self):
        assert _import_research_read_timeout(3, base_timeout=600.0) == 600.0

    def test_larger_configured_budget_beats_the_batch_ceiling(self):
        # The ceiling bounds the *scaling*, not the caller's explicit budget.
        assert _import_research_read_timeout(1000, base_timeout=600.0) == 600.0
        assert DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT < 600.0

    def test_infinite_base_timeout_stays_infinite(self):
        assert _import_research_read_timeout(3, base_timeout=None) is None

    def test_explicit_override_replaces_both_scaling_and_floor(self):
        assert _import_research_read_timeout(1000, base_timeout=600.0, override=90.0) == 90.0

    def test_remaining_budget_clamps_the_window(self):
        assert (
            _import_research_read_timeout(1000, base_timeout=600.0, remaining_budget=45.0) == 45.0
        )

    def test_remaining_budget_clamps_an_explicit_override(self):
        assert (
            _import_research_read_timeout(
                3, base_timeout=30.0, override=900.0, remaining_budget=45.0
            )
            == 45.0
        )

    def test_remaining_budget_bounds_an_infinite_window(self):
        assert _import_research_read_timeout(3, base_timeout=None, remaining_budget=45.0) == 45.0

    def test_remaining_budget_larger_than_the_window_changes_nothing(self):
        assert _import_research_read_timeout(3, base_timeout=30.0, remaining_budget=1800.0) == (
            DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + 3 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )


class TestConstructorWiring:
    def test_default_client_keeps_the_builtin_windows(self, auth_tokens):
        client = NotebookLMClient(auth_tokens)

        assert client.chat._chat_timeout == DEFAULT_CHAT_TIMEOUT
        assert client.research._base_timeout == DEFAULT_TIMEOUT
        assert client.research._import_research_timeout is None

    def test_configured_timeout_reaches_both_surfaces(self, auth_tokens):
        client = NotebookLMClient(auth_tokens, timeout=600.0)

        assert client.chat._chat_timeout == 600.0
        assert client.research._base_timeout == 600.0

    def test_import_research_timeout_kwarg_is_forwarded(self, auth_tokens):
        client = NotebookLMClient(auth_tokens, timeout=600.0, import_research_timeout=900.0)

        assert client.research._import_research_timeout == 900.0


class TestEffectiveWireTimeout:
    """The reported scenario, asserted on the request httpx actually sent."""

    @pytest.mark.asyncio
    async def test_import_research_rides_the_configured_600s_budget(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        body = build_rpc_response(RPCMethod.IMPORT_RESEARCH, [[[["src_001"], "Web Source"]]])
        httpx_mock.add_response(content=body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens, timeout=600.0) as client:
            await client.research.import_sources(
                notebook_id="nb_123",
                task_id="task_123",
                sources=[{"url": "http://example.com", "title": "Web Source", "result_type": 1}],
            )

        assert _read_timeout_of(httpx_mock.get_requests()[0]) == 600.0

    @pytest.mark.asyncio
    async def test_import_research_keeps_the_batch_scaled_window_by_default(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        body = build_rpc_response(RPCMethod.IMPORT_RESEARCH, [[[["src_001"], "Web Source"]]])
        httpx_mock.add_response(content=body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens) as client:
            await client.research.import_sources(
                notebook_id="nb_123",
                task_id="task_123",
                sources=[{"url": "http://example.com", "title": "Web Source", "result_type": 1}],
            )

        assert _read_timeout_of(httpx_mock.get_requests()[0]) == (
            DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )

    @pytest.mark.asyncio
    async def test_import_research_honors_an_explicit_override(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        body = build_rpc_response(RPCMethod.IMPORT_RESEARCH, [[[["src_001"], "Web Source"]]])
        httpx_mock.add_response(content=body.encode(), method="POST")

        async with NotebookLMClient(auth_tokens, import_research_timeout=90.0) as client:
            await client.research.import_sources(
                notebook_id="nb_123",
                task_id="task_123",
                sources=[{"url": "http://example.com", "title": "Web Source", "result_type": 1}],
            )

        assert _read_timeout_of(httpx_mock.get_requests()[0]) == 90.0

    @pytest.mark.asyncio
    async def test_chat_rides_the_configured_600s_budget(
        self, auth_tokens, httpx_mock, mock_get_conversation_id
    ):
        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth_tokens, timeout=600.0) as client:
            await client.chat.ask(notebook_id="nb_123", question="What is this?", source_ids=["s1"])

        chat_requests = [
            request
            for request in httpx_mock.get_requests()
            if "GenerateFreeFormStreamed" in str(request.url)
        ]
        assert chat_requests, "no streamed chat request was recorded"
        assert _read_timeout_of(chat_requests[0]) == 600.0

    @pytest.mark.asyncio
    async def test_chat_keeps_its_builtin_window_on_a_default_client(
        self, auth_tokens, httpx_mock, mock_get_conversation_id
    ):
        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth_tokens) as client:
            await client.chat.ask(notebook_id="nb_123", question="What is this?", source_ids=["s1"])

        chat_requests = [
            request
            for request in httpx_mock.get_requests()
            if "GenerateFreeFormStreamed" in str(request.url)
        ]
        assert _read_timeout_of(chat_requests[0]) == DEFAULT_CHAT_TIMEOUT
