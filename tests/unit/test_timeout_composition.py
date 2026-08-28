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
from pathlib import Path
from types import SimpleNamespace

import pytest

from notebooklm import NotebookLMClient
from notebooklm._runtime.config import (
    AUTO_READ_TIMEOUT,
    DEFAULT_CHAT_TIMEOUT,
    DEFAULT_CONNECT_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT,
    DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT,
    DEFAULT_TIMEOUT,
    MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT,
    compose_builtin_read_timeout,
    resolve_chat_read_timeout,
)
from notebooklm._web.chat import WebChatAPI
from notebooklm._web.policy import (
    IDEMPOTENCY_REGISTRY,
    resolve_effective_disable_internal_retries,
)
from notebooklm._web.research_import import _import_research_read_timeout
from notebooklm.rpc import RPCMethod

#: batchexecute puts the RPC id in the query string (``?rpcids=…``), so it
#: identifies the IMPORT_RESEARCH POST among everything else a client sends.
#: Derived from the enum rather than hardcoded so an id rotation (the #1
#: breakage class in this repo) can't leave the selector silently matching
#: nothing.
_IMPORT_RESEARCH_RPC_ID = RPCMethod.IMPORT_RESEARCH.value


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

    ``httpx`` stamps the resolved timeout onto ``request.extensions`` during
    ``build_request`` — from the per-request override when there is one, and
    from the client's own default when there is not. So this reads what the
    transport was actually told in *both* branches of ``Kernel.post``, rather
    than what some intermediate layer intended.
    """
    return request.extensions["timeout"]["read"]


def _read_timeout_for(httpx_mock, marker: str) -> float | None:
    """Read timeout of the one recorded request matching ``marker``.

    Selected by RPC id / endpoint rather than by position: a future pre-flight
    request during client open would silently shift a positional index onto the
    wrong request, and several of these assertions would still pass by accident
    because the base timeout matches the expected value.
    """
    matches = [r for r in httpx_mock.get_requests() if marker in str(r.url)]
    assert len(matches) == 1, f"expected exactly one {marker} request, got {len(matches)}"
    return _read_timeout_of(matches[0])


class TestComposeBuiltinReadTimeout:
    def test_builtin_window_wins_when_base_timeout_is_shorter(self):
        assert compose_builtin_read_timeout(180.0, 30.0) == 180.0

    def test_configured_base_wins_when_it_is_longer(self):
        # The reported bug: a 600 s budget must not be cut back to the constant.
        assert compose_builtin_read_timeout(180.0, 600.0) == 600.0

    def test_equal_values_compose_to_the_same_window(self):
        assert compose_builtin_read_timeout(180.0, 180.0) == 180.0

    def test_no_base_timeout_means_no_per_rpc_override(self):
        # A client built with ``timeout=None`` (discouraged by the annotation,
        # but reachable at runtime — ``Kernel.post`` explicitly contemplates it)
        # opted out of a read ceiling. A finite constant must not re-impose one,
        # so the composition declines to override at all.
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
        # #2187's scaling behavior is correct and must not regress.
        assert _import_research_read_timeout(3, base_timeout=DEFAULT_TIMEOUT) == (
            DEFAULT_IMPORT_RESEARCH_BASE_TIMEOUT + 3 * DEFAULT_IMPORT_RESEARCH_PER_SOURCE_TIMEOUT
        )

    def test_larger_configured_budget_floors_the_scaled_window(self):
        assert _import_research_read_timeout(3, base_timeout=600.0) == 600.0

    def test_larger_configured_budget_beats_the_batch_ceiling(self):
        # The ceiling bounds the *scaling*, not the caller's explicit budget.
        assert _import_research_read_timeout(1000, base_timeout=600.0) == 600.0
        assert DEFAULT_IMPORT_RESEARCH_MAX_TIMEOUT < 600.0

    def test_no_base_timeout_means_no_per_rpc_override(self):
        # Same defensive branch as the chat side; see the note there.
        assert _import_research_read_timeout(3, base_timeout=None) is None

    def test_explicit_override_replaces_both_scaling_and_floor(self):
        assert _import_research_read_timeout(1000, base_timeout=600.0, override=90.0) == 90.0

    def test_override_none_inherits_the_base_timeout_verbatim(self):
        # Same three-way reading as ``chat_timeout``: ``None`` means "no
        # per-RPC window at all", which is distinct from "unset".
        assert _import_research_read_timeout(1000, base_timeout=600.0, override=None) is None

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

    def test_remaining_budget_bounds_an_unoverridden_window(self):
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
        assert client.research._import_research_timeout is AUTO_READ_TIMEOUT

    def test_configured_timeout_reaches_both_surfaces(self, auth_tokens):
        client = NotebookLMClient(auth_tokens, timeout=600.0)

        assert client.chat._chat_timeout == 600.0
        assert client.research._base_timeout == 600.0

    def test_import_research_timeout_kwarg_is_forwarded(self, auth_tokens):
        client = NotebookLMClient(auth_tokens, timeout=600.0, import_research_timeout=900.0)

        assert client.research._import_research_timeout == 900.0

    def test_the_two_knobs_do_not_leak_into_each_other(self, auth_tokens):
        # A swap in the assembly wiring would otherwise pass every other test
        # here, since both knobs are plumbed through the same function.
        chat_only = NotebookLMClient(auth_tokens, timeout=600.0, chat_timeout=10.0)
        assert chat_only.chat._chat_timeout == 10.0
        assert chat_only.research._import_research_timeout is AUTO_READ_TIMEOUT
        assert chat_only.research._base_timeout == 600.0

        import_only = NotebookLMClient(auth_tokens, import_research_timeout=900.0)
        assert import_only.research._import_research_timeout == 900.0
        assert import_only.chat._chat_timeout == DEFAULT_CHAT_TIMEOUT


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

        assert _read_timeout_for(httpx_mock, _IMPORT_RESEARCH_RPC_ID) == 600.0

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

        assert _read_timeout_for(httpx_mock, _IMPORT_RESEARCH_RPC_ID) == (
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

        assert _read_timeout_for(httpx_mock, _IMPORT_RESEARCH_RPC_ID) == 90.0

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

        assert _read_timeout_for(httpx_mock, "GenerateFreeFormStreamed") == 600.0

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

        assert _read_timeout_for(httpx_mock, "GenerateFreeFormStreamed") == DEFAULT_CHAT_TIMEOUT


class TestRejectsUnusableWindows:
    """A window that could only ever break the call fails at construction."""

    @pytest.mark.parametrize("kwarg", ["chat_timeout", "import_research_timeout"])
    @pytest.mark.parametrize("value", [0, -1.0, float("nan"), float("inf")])
    def test_non_positive_or_non_finite_is_rejected(self, auth_tokens, kwarg, value):
        # httpx.Timeout accepts read=0 / read=-5 without complaint, so without
        # this the only symptom is every affected RPC timing out instantly with
        # a message naming neither the kwarg nor the value.
        with pytest.raises(ValueError, match=kwarg):
            NotebookLMClient(auth_tokens, **{kwarg: value})

    @pytest.mark.parametrize("kwarg", ["chat_timeout", "import_research_timeout"])
    def test_non_numeric_is_rejected(self, auth_tokens, kwarg):
        with pytest.raises(TypeError, match=kwarg):
            NotebookLMClient(auth_tokens, **{kwarg: "600"})

    @pytest.mark.parametrize("kwarg", ["chat_timeout", "import_research_timeout"])
    def test_none_is_still_accepted(self, auth_tokens, kwarg):
        client = NotebookLMClient(auth_tokens, **{kwarg: None})

        resolved = (
            client.chat._chat_timeout
            if kwarg == "chat_timeout"
            else client.research._import_research_timeout
        )
        assert resolved is None

    def test_unresolved_sentinel_cannot_reach_chat(self):
        """The sentinel is resolved once, at the composition root.

        Every layer below guards on ``is not None``, and ``httpx.Timeout``
        accepts the sentinel object without error — so a leak would surface far
        away as a TypeError inside the timeout *error formatter*, destroying the
        real diagnostic. Fail at the boundary instead.
        """
        with pytest.raises(TypeError, match="AUTO_READ_TIMEOUT"):
            WebChatAPI(
                rpc=SimpleNamespace(rpc_call=None),
                transport=SimpleNamespace(),
                reqid=SimpleNamespace(),
                loop_guard=SimpleNamespace(assert_bound_loop=lambda: None),
                notebooks=SimpleNamespace(),
                chat_timeout=AUTO_READ_TIMEOUT,
            )


class TestInheritedWindowsAtTheWire:
    """``None`` takes a structurally different path: no override is built at all.

    ``Kernel.post`` skips constructing a per-request ``httpx.Timeout`` entirely,
    so the promise "inherit ``timeout=`` verbatim" rests on that fall-through
    and on nothing else. Assert it where it is observable.
    """

    @pytest.mark.asyncio
    async def test_chat_none_rides_the_base_timeout(
        self, auth_tokens, httpx_mock, mock_get_conversation_id
    ):
        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth_tokens, timeout=600.0, chat_timeout=None) as client:
            await client.chat.ask(notebook_id="nb_123", question="What is this?", source_ids=["s1"])

        assert _read_timeout_for(httpx_mock, "GenerateFreeFormStreamed") == 600.0


class TestInheritedImportResearchWindow:
    @pytest.mark.asyncio
    async def test_import_research_none_rides_the_base_timeout(
        self, auth_tokens, httpx_mock, build_rpc_response
    ):
        body = build_rpc_response(RPCMethod.IMPORT_RESEARCH, [[[["src_001"], "Web Source"]]])
        httpx_mock.add_response(content=body.encode(), method="POST")

        async with NotebookLMClient(
            auth_tokens, timeout=45.0, import_research_timeout=None
        ) as client:
            await client.research.import_sources(
                notebook_id="nb_123",
                task_id="task_123",
                sources=[{"url": "http://example.com", "title": "Web Source", "result_type": 1}],
            )

        # No per-RPC override at all: the request rides the client's own base.
        assert _read_timeout_for(httpx_mock, _IMPORT_RESEARCH_RPC_ID) == 45.0


def test_minimum_attempt_window_matches_the_connect_timeout():
    """The floor is the connect timeout for a reason, not a magic number.

    A read window that cannot outlast connection establishment can never
    observe its own result.
    """
    assert MIN_IMPORT_RESEARCH_ATTEMPT_TIMEOUT == DEFAULT_CONNECT_TIMEOUT


def test_import_research_forbids_executor_internal_retries():
    """The ``max_elapsed`` clamp bounds the attempt, not a retry multiplier.

    ``read_timeout`` is deliberately re-threaded into the executor's internal
    retries (#2187), so if IMPORT_RESEARCH were internally retryable, a window
    clamped to the remaining budget could still be spent several times over and
    blow the deadline anyway — making the clamp, and the promise in
    docs/configuration.md, false. It is not retryable: the registry's
    NON_IDEMPOTENT_NO_RETRY classification (#808) disables internal retries
    regardless of what the caller passes. Pinned here because the clamp's
    correctness depends on a fact that lives in another module.
    """
    assert (
        resolve_effective_disable_internal_retries(
            IDEMPOTENCY_REGISTRY,
            RPCMethod.IMPORT_RESEARCH,
            caller_disable_internal_retries=False,
            operation_variant=None,
        )
        is True
    )


def _write_storage_state(tmp_path) -> Path:
    """Minimal storage_state.json so ``from_storage`` can build a client."""
    storage_file = tmp_path / "storage_state.json"
    storage_file.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "compose_sid", "domain": ".google.com"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "compose_1psidts",
                        "domain": ".google.com",
                    },
                    {"name": "HSID", "value": "compose_hsid", "domain": ".google.com"},
                ],
                "origins": [],
            }
        )
    )
    return storage_file


@pytest.mark.asyncio
async def test_from_storage_carries_the_composition(tmp_path, httpx_mock):
    """``from_storage`` is the path MCP, the REST server, and profiles all take.

    It has its own copy of the kwarg defaults, so reverting *only* its
    ``chat_timeout`` default would reinstate the entire #2205 bug for every
    profile-based caller while ``__init__``-based tests stayed green.
    """
    httpx_mock.add_response(
        url="https://notebook.google.com/",
        content=b'"SNlM0e":"compose_csrf" "FdrFJe":"compose_session"',
    )

    async with NotebookLMClient.from_storage(
        path=str(_write_storage_state(tmp_path)), timeout=600.0
    ) as client:
        assert client.chat._chat_timeout == 600.0
        assert client.research._base_timeout == 600.0
        assert client.research._import_research_timeout is AUTO_READ_TIMEOUT
