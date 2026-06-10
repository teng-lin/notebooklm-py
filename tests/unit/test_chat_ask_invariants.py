"""Regression tests for ``ChatAPI.ask`` after the chat-transport refactor.

These assertions pin down the new contract:

- ``ask`` uses ``self._reqid.next_reqid()`` for the URL ``_reqid`` param (the
  ``_reqid_counter`` property + deprecation gesture were retired in the
  session-shrink arc; this test now guards against any new
  ``DeprecationWarning`` escaping ``_chat/api.py``).
- ``authuser=`` is present on the chat URL when ``account_email`` is set on
  the auth tokens, mirroring the batchexecute path in
  ``RpcExecutor.build_url``. Previously omitted entirely on the chat endpoint.
- Concurrent ``asyncio.gather(ask*3)`` produces three distinct reqid values.
- 401 mid-chat triggers a refresh, and the post-refresh attempt's body
  carries the refreshed CSRF token (snapshot-per-attempt invariant).
- ``NOTEBOOKLM_BL`` env override still works after the move to
  :mod:`notebooklm._env`.
"""

from __future__ import annotations

import asyncio
import json
import re
import warnings
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, unquote, urlparse

import httpx
import pytest

from notebooklm import NotebookLMClient
from notebooklm._chat import ChatAPI
from notebooklm._request_types import AuthSnapshot
from notebooklm.auth import AuthTokens
from notebooklm.exceptions import ChatError
from tests._helpers.client_factory import build_client_shell_for_tests
from tests.unit.conftest import install_post_as_stream

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_answer_response_body(
    answer: str = "Refactor answer is long enough.",
    *,
    server_conv_id: str = "server-refactor-conv",
) -> bytes:
    """Build a minimal valid streaming chat response.

    A ``server_conv_id`` is always present at ``first[2][0]`` because
    ``ChatAPI.ask`` requires the server to assign the id for new
    conversations (issue #659); responses lacking one raise ``ChatError``.
    """
    inner_json = json.dumps([[answer, None, [server_conv_id, 12345], None, [1]]])
    chunk_json = json.dumps([["wrb.fr", None, inner_json]])
    return f")]}}'\n{len(chunk_json)}\n{chunk_json}\n".encode()


def _extract_query_param(url: str, key: str) -> str | None:
    qs = parse_qs(urlparse(url).query, keep_blank_values=True)
    values = qs.get(key)
    return values[0] if values else None


# ---------------------------------------------------------------------------
# chat timeout routing
# ---------------------------------------------------------------------------


class TestChatTimeoutRouting:
    def test_client_uses_chat_specific_timeout_by_default(self):
        auth = AuthTokens(cookies={"SID": "x"}, csrf_token="csrf", session_id="sid")
        client = NotebookLMClient(auth, timeout=75.0)

        assert client.chat._chat_timeout == 180.0

    def test_client_chat_timeout_none_inherits_transport_timeout(self):
        auth = AuthTokens(cookies={"SID": "x"}, csrf_token="csrf", session_id="sid")
        client = NotebookLMClient(auth, timeout=75.0, chat_timeout=None)

        assert client.chat._chat_timeout is None

    def test_client_chat_timeout_override_wins(self):
        auth = AuthTokens(cookies={"SID": "x"}, csrf_token="csrf", session_id="sid")
        client = NotebookLMClient(auth, timeout=75.0, chat_timeout=180.0)

        assert client.chat._chat_timeout == 180.0

    @pytest.mark.asyncio
    async def test_ask_passes_chat_read_timeout_and_disables_timeout_retry(self):
        """``ask`` uses the chat-specific read window without retrying timed-out streams."""
        transport = SimpleNamespace(
            perform_authed_post=AsyncMock(
                return_value=httpx.Response(
                    200,
                    request=httpx.Request("POST", "https://example.test/chat"),
                    content=_make_answer_response_body(),
                )
            )
        )
        chat = ChatAPI(
            rpc=SimpleNamespace(),
            transport=transport,
            reqid=SimpleNamespace(next_reqid=AsyncMock(return_value=100000)),
            loop_guard=SimpleNamespace(assert_bound_loop=lambda: None),
            chat_timeout=45.0,
        )

        result = await chat.ask(
            "nb-1",
            "Q?",
            source_ids=["s1"],
            conversation_id="conv-1",
        )

        assert result.answer == "Refactor answer is long enough."
        assert transport.perform_authed_post.await_args.kwargs.get("read_timeout") == 45.0
        assert (
            transport.perform_authed_post.await_args.kwargs.get("disable_read_timeout_retries")
            is True
        )


# ---------------------------------------------------------------------------
# authuser= URL parameter
# ---------------------------------------------------------------------------


class TestChatAuthuserParam:
    """``authuser=`` was previously omitted entirely on the chat endpoint."""

    @pytest.mark.asyncio
    async def test_authuser_set_when_account_email_provided(
        self, httpx_mock, mock_get_conversation_id
    ):
        """When ``account_email`` is set on auth, chat URL carries authuser=email."""
        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="csrf",
            session_id="sid",
            authuser=2,
            account_email="user@example.com",
        )

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_make_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()  # issue #659 post-ask round-trip

        async with NotebookLMClient(auth) as client:
            await client.chat.ask("nb_x", "Q?", source_ids=["s1"])

        # Filter for the chat-ask request — the post-ask hPTbtc request also
        # lands on the same authuser query, but we want to assert the chat
        # leg specifically.
        request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        # Email is preferred over the integer index because it survives
        # browser-account reordering.
        assert _extract_query_param(str(request.url), "authuser") == "user@example.com"

    @pytest.mark.asyncio
    async def test_authuser_set_when_only_authuser_index(
        self, httpx_mock, mock_get_conversation_id
    ):
        """When only ``authuser`` is non-zero (no email), still emit the index."""
        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="csrf",
            session_id="sid",
            authuser=3,
        )

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_make_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth) as client:
            await client.chat.ask("nb_x", "Q?", source_ids=["s1"])

        request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        assert _extract_query_param(str(request.url), "authuser") == "3"

    @pytest.mark.asyncio
    async def test_authuser_absent_for_default_profile(self, httpx_mock, mock_get_conversation_id):
        """No ``authuser=`` on the URL when authuser=0 and no email — matches the
        previous-contract default-profile behavior (don't churn the existing single-account
        contract)."""
        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="csrf",
            session_id="sid",
            # authuser defaults to 0, account_email defaults to None
        )

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_make_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth) as client:
            await client.chat.ask("nb_x", "Q?", source_ids=["s1"])

        request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        assert _extract_query_param(str(request.url), "authuser") is None


# ---------------------------------------------------------------------------
# next_reqid + DeprecationWarning silence
# ---------------------------------------------------------------------------


class TestChatReqid:
    """``ChatAPI.ask`` must call ``self._reqid.next_reqid()`` — not poke
    ``_reqid_counter`` directly, which would emit ``DeprecationWarning``."""

    @pytest.mark.asyncio
    async def test_ask_uses_next_reqid_no_deprecation_warning(
        self, httpx_mock, mock_get_conversation_id
    ):
        """No ``DeprecationWarning`` is emitted by ``_chat/api.py`` during ask()."""
        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="csrf",
            session_id="sid",
        )

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_make_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            async with NotebookLMClient(auth) as client:
                await client.chat.ask("nb_x", "Q?", source_ids=["s1"])

        chat_dep_warnings = [
            w
            for w in caught
            if issubclass(w.category, DeprecationWarning)
            and "_reqid_counter" in str(w.message)
            and "_chat" in str(w.filename)
            and "api.py" in str(w.filename)
        ]
        assert chat_dep_warnings == [], (
            f"_chat/api.py must not emit _reqid_counter DeprecationWarning; "
            f"got: {[(str(w.filename), str(w.message)) for w in chat_dep_warnings]}"
        )

    @pytest.mark.asyncio
    async def test_concurrent_asks_produce_distinct_reqids(
        self, httpx_mock, mock_get_conversation_id
    ):
        """``asyncio.gather(ask*3)`` → three distinct ``_reqid`` URL values.

        Previously, the body did ``self._core._collaborators.reqid_counter += 100000`` under
        a read-modify-write race (``self._core`` was the pre-Phase-2 attribute
        name, now ``self._runtime``); under concurrent gather() this collapsed
        to a single reqid value. ``runtime.next_reqid()`` serializes the
        increment under an asyncio.Lock, restoring monotonic distinct ids.
        """
        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="csrf",
            session_id="sid",
        )

        # One response per gathered chat-ask. pytest_httpx replays in order.
        for _ in range(3):
            httpx_mock.add_response(
                url=re.compile(r".*GenerateFreeFormStreamed.*"),
                content=_make_answer_response_body(),
                method="POST",
            )
        # Plus the three post-ask hPTbtc round-trips (issue #659).
        mock_get_conversation_id(reusable=True)

        async with NotebookLMClient(auth) as client:
            await asyncio.gather(
                client.chat.ask("nb_x", "Q1", source_ids=["s1"]),
                client.chat.ask("nb_x", "Q2", source_ids=["s1"]),
                client.chat.ask("nb_x", "Q3", source_ids=["s1"]),
            )

        reqids = [
            _extract_query_param(str(req.url), "_reqid")
            for req in httpx_mock.get_requests()
            if "GenerateFreeFormStreamed" in str(req.url)
        ]
        assert len(reqids) == 3
        assert all(r is not None for r in reqids)
        assert len(set(reqids)) == 3, f"reqids must be distinct, got {reqids}"


# ---------------------------------------------------------------------------
# 401 mid-chat → snapshot regenerated for retry
# ---------------------------------------------------------------------------


class TestChatRefreshRetry:
    """Snapshot-per-attempt invariant: ``build_request`` is invoked once
    per attempt with a *fresh* ``AuthSnapshot``, so the retry body carries
    the post-refresh CSRF token rather than replaying the stale pre-refresh
    body."""

    @pytest.mark.asyncio
    async def test_post_refresh_retry_uses_fresh_csrf_in_body(self, monkeypatch):
        """401 → refresh callback rotates CSRF → retry body contains new token."""
        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="OLD_CSRF",
            session_id="OLD_SID",
        )

        async def refresh() -> AuthTokens:
            # Mutate the live auth tokens — the next snapshot picks this up.
            auth.csrf_token = "NEW_CSRF"
            auth.session_id = "NEW_SID"
            return auth

        core = build_client_shell_for_tests(
            auth=auth, refresh_callback=refresh, refresh_retry_delay=0.0
        )
        await core.__aenter__()
        try:
            observed_bodies: list[str] = []
            call_count = {"n": 0}

            async def fake_post(url, *args, **kwargs):  # type: ignore[no-untyped-def]
                # The post-ask ``hPTbtc`` request from issue #659 also goes
                # through this fake_post. Identify it by URL and return a
                # minimal RPC response that decodes to a valid conv_id.
                if "batchexecute" in str(url):
                    rpc_body = (
                        ")]}'\n"
                        '63\n[["wrb.fr","hPTbtc","[[[\\"real-conv-id-from-hptbtc\\"]]]",null,null]]'
                    )
                    return httpx.Response(
                        200,
                        request=httpx.Request("POST", url),
                        content=rpc_body.encode(),
                    )

                # Chat-ask path: capture the body and exercise the retry contract.
                body = kwargs.get("content")
                if isinstance(body, bytes):
                    body = body.decode()
                observed_bodies.append(body or "")

                call_count["n"] += 1
                if call_count["n"] == 1:
                    # First attempt: 401 → triggers refresh path.
                    response = httpx.Response(401, request=httpx.Request("POST", url), content=b"")
                    raise httpx.HTTPStatusError("401", request=response.request, response=response)
                # Second attempt (after refresh): return a valid answer.
                return httpx.Response(
                    200,
                    request=httpx.Request("POST", url),
                    content=_make_answer_response_body(),
                )

            assert core._collaborators.kernel.http_client is not None
            install_post_as_stream(
                monkeypatch, core._collaborators.kernel.get_http_client(), fake_post
            )

            # Wave 8 of session-decoupling (ADR-0014 Rule 2 Corollary):
            # ``ChatAPI`` takes its four direct collaborators by keyword
            # arg. Wired here from the real ``Session`` under test so the
            # refresh path exercises the production transport/rpc/reqid
            # collaborators end-to-end.
            # Stage B1 PR 2 deleted the Stage A accessors
            # (``Session.session_transport`` / ``Session.collaborators``);
            # read the private slots directly instead.
            api = ChatAPI(
                rpc=core._rpc_executor,
                transport=core._composed.transport,
                reqid=core._collaborators.reqid,
                loop_guard=core._collaborators.lifecycle,
            )
            result = await api.ask("nb_x", "Q?", source_ids=["s1"])

            assert call_count["n"] == 2
            assert "Refactor answer is long enough." in result.answer

            # First attempt body carries OLD_CSRF (pre-refresh snapshot).
            assert "at=OLD_CSRF" in observed_bodies[0]
            assert "at=NEW_CSRF" not in observed_bodies[0]
            # Second attempt body carries NEW_CSRF (post-refresh snapshot)
            # — this is the snapshot-per-attempt contract surfacing
            # through chat_aware_authed_post.
            assert "at=NEW_CSRF" in observed_bodies[1]
            assert "at=OLD_CSRF" not in observed_bodies[1]
        finally:
            await core.close()


# ---------------------------------------------------------------------------
# NOTEBOOKLM_BL override still works after the move to _env.py
# ---------------------------------------------------------------------------


class TestChatBlOverride:
    """Single-source-of-truth for the ``bl`` parameter lives in ``_env.py``.
    The ``NOTEBOOKLM_BL`` override must still flow through to the chat URL.
    """

    @pytest.mark.asyncio
    async def test_custom_bl_env_appears_in_url(
        self, httpx_mock, monkeypatch, mock_get_conversation_id
    ):
        monkeypatch.setenv("NOTEBOOKLM_BL", "boq_labs-custom_99999999.00_p0")

        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="csrf",
            session_id="sid",
        )

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_make_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth) as client:
            await client.chat.ask("nb_x", "Q?", source_ids=["s1"])

        request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        assert _extract_query_param(str(request.url), "bl") == "boq_labs-custom_99999999.00_p0"

    @pytest.mark.asyncio
    async def test_default_bl_is_pinned_constant(
        self, httpx_mock, monkeypatch, mock_get_conversation_id
    ):
        """With ``NOTEBOOKLM_BL`` unset, the URL falls back to the pinned default.

        The expected literal is duplicated here on purpose: importing
        ``DEFAULT_BL`` from the SUT and asserting equality would be a
        tautology — any wrong-value edit to ``_env.DEFAULT_BL`` would still
        pass. The literal pin catches that.
        """
        monkeypatch.delenv("NOTEBOOKLM_BL", raising=False)

        auth = AuthTokens(
            cookies={"SID": "x"},
            csrf_token="csrf",
            session_id="sid",
        )

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=_make_answer_response_body(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth) as client:
            await client.chat.ask("nb_x", "Q?", source_ids=["s1"])

        request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        assert (
            _extract_query_param(str(request.url), "bl")
            == "boq_labs-tailwind-frontend_20260301.03_p0"
        )


# ---------------------------------------------------------------------------
# _build_chat_request direct unit-level coverage
# ---------------------------------------------------------------------------


class TestChatNewConversationLocks:
    """Notebook-scoped locks use the same per-key cache style as conversation locks."""

    def _factory(self) -> ChatAPI:
        from unittest.mock import MagicMock

        loop_guard = MagicMock()
        loop_guard.assert_bound_loop = MagicMock()
        return ChatAPI(
            rpc=MagicMock(),
            transport=MagicMock(),
            reqid=MagicMock(),
            loop_guard=loop_guard,
        )

    def test_same_notebook_reuses_new_conversation_lock(self):
        chat = self._factory()

        lock_a = chat._get_new_conversation_lock("nb-1")
        lock_b = chat._get_new_conversation_lock("nb-1")

        assert lock_a is lock_b

    def test_different_notebooks_get_distinct_new_conversation_locks(self):
        chat = self._factory()

        lock_a = chat._get_new_conversation_lock("nb-1")
        lock_b = chat._get_new_conversation_lock("nb-2")

        assert lock_a is not lock_b

    @pytest.mark.asyncio
    async def test_failed_post_ask_hptbtc_lookup_releases_new_conversation_lock(self):
        class HptbtcFailureChatAPI(ChatAPI):
            def __init__(self, *, lookup_results: list[str | ChatError], **kwargs: Any) -> None:
                super().__init__(**kwargs)
                self._lookup_results = iter(lookup_results)
                self.lookup_count = 0

            async def get_conversation_id(self, notebook_id: str) -> str | None:
                self.lookup_count += 1
                result = next(self._lookup_results)
                if isinstance(result, ChatError):
                    raise result
                return result

        async def fake_perform_authed_post(*args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(
                200,
                request=httpx.Request("POST", "https://notebooklm.google.com/_/LabsTailwindUi"),
                content=_make_answer_response_body(),
            )

        chat = HptbtcFailureChatAPI(
            rpc=SimpleNamespace(),
            transport=SimpleNamespace(
                perform_authed_post=AsyncMock(side_effect=fake_perform_authed_post)
            ),
            reqid=SimpleNamespace(next_reqid=AsyncMock(side_effect=[100000, 200000])),
            loop_guard=SimpleNamespace(assert_bound_loop=lambda: None),
            lookup_results=[ChatError("hPTbtc lookup failed"), "conv-after-failure"],
        )
        new_conversation_lock = chat._get_new_conversation_lock("nb-1")

        with pytest.raises(ChatError, match="hPTbtc lookup failed"):
            await chat.ask("nb-1", "first ask", source_ids=["s1"])

        assert not new_conversation_lock.locked()

        result = await asyncio.wait_for(
            chat.ask("nb-1", "second ask", source_ids=["s1"]),
            timeout=1.0,
        )

        assert result.conversation_id == "conv-after-failure"
        assert result.answer == "Refactor answer is long enough."
        assert chat.lookup_count == 2


class TestBuildChatRequestFactory:
    """Direct unit tests for the new ``ChatAPI._build_chat_request`` factory.

    Bypassing the full ``ask`` plumbing keeps these checks focused on the
    URL/body assembly contract that ``chat_aware_authed_post`` relies on.
    """

    def _factory(self) -> ChatAPI:
        # Wave 8 of session-decoupling (ADR-0014 Rule 2 Corollary):
        # ``ChatAPI`` takes direct collaborators by keyword arg. Pure
        # ``_build_chat_request`` exercise — none of these collaborators
        # are touched, so they are bare ``MagicMock()`` placeholders.
        from unittest.mock import MagicMock

        return ChatAPI(
            rpc=MagicMock(),
            transport=MagicMock(),
            reqid=MagicMock(),
            loop_guard=MagicMock(),
        )

    def test_build_request_omits_authuser_for_default_profile(self):
        chat = self._factory()
        snapshot = AuthSnapshot(
            csrf_token="csrf",
            session_id="sid",
            authuser=0,
            account_email=None,
        )
        url, body, headers = chat._build_chat_request(
            snapshot=snapshot,
            notebook_id="nb_x",
            question="Q?",
            source_ids=["s1"],
            conversation_history=None,
            conversation_id="conv-1",
            reqid=200000,
        )
        assert _extract_query_param(url, "authuser") is None
        assert _extract_query_param(url, "_reqid") == "200000"
        assert "at=csrf" in body
        assert headers == {}

    def test_build_request_authuser_email_wins_over_index(self):
        chat = self._factory()
        snapshot = AuthSnapshot(
            csrf_token="csrf",
            session_id="sid",
            authuser=5,
            account_email="me@example.com",
        )
        url, _, _ = chat._build_chat_request(
            snapshot=snapshot,
            notebook_id="nb_x",
            question="Q?",
            source_ids=["s1"],
            conversation_history=None,
            conversation_id="conv-1",
            reqid=300000,
        )
        # Email is preferred when present — matches ``format_authuser_value``.
        assert _extract_query_param(url, "authuser") == "me@example.com"

    def test_build_request_omits_at_when_csrf_blank(self):
        chat = self._factory()
        snapshot = AuthSnapshot(
            csrf_token="",
            session_id="sid",
            authuser=0,
            account_email=None,
        )
        _, body, _ = chat._build_chat_request(
            snapshot=snapshot,
            notebook_id="nb_x",
            question="Q?",
            source_ids=["s1"],
            conversation_history=None,
            conversation_id="conv-1",
            reqid=400000,
        )
        assert "at=" not in body

    def test_build_request_source_encoding_is_triple_nested(self):
        chat = self._factory()
        snapshot = AuthSnapshot(
            csrf_token="csrf",
            session_id="sid",
            authuser=0,
            account_email=None,
        )
        _, body, _ = chat._build_chat_request(
            snapshot=snapshot,
            notebook_id="nb_x",
            question="Q?",
            source_ids=["s1", "s2"],
            conversation_history=None,
            conversation_id="conv-1",
            reqid=500000,
        )
        match = re.search(r"f\.req=([^&]+)", body)
        assert match is not None
        f_req_data: list[Any] = json.loads(unquote(match.group(1)))
        params: list[Any] = json.loads(f_req_data[1])
        assert params[0] == [[["s1"]], [["s2"]]]
