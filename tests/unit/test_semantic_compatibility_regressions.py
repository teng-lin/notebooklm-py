"""Focused semantic-backend compatibility and concurrency regressions."""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

import httpx
import pytest

from notebooklm._auth.cookie_types import CookieJar
from notebooklm._backend import BackendError, BackendErrorReason
from notebooklm._chat import notes as chat_notes
from notebooklm._chat import wire as chat_wire
from notebooklm._kernel import Kernel
from notebooklm._records import ARTIFACT_LIST_DEF, ArtifactListInput
from notebooklm._runtime.web_backend_session import WebBackendSession
from notebooklm._web.codec import chat_stream
from notebooklm._web_cookie_provider import WebCookieGeneration
from notebooklm.exceptions import NetworkError
from notebooklm.rpc import RPCMethod
from notebooklm.types import ChatReference, ConnectionLimits
from tests._fixtures.web_backend import build_web_backend


def _generation() -> WebCookieGeneration:
    cookies = httpx.Cookies()
    cookies.set("SID", "private-session", domain=".google.com", path="/")
    return WebCookieGeneration(
        csrf_token="csrf",
        session_id="session",
        authuser=0,
        account_email=None,
        cookies=CookieJar.from_httpx(cookies),
        generation=1,
    )


async def _artifact_rows_after_mind_map_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: Exception,
) -> tuple[object, ...]:
    """Drive the ``ARTIFACT_LIST`` custom row (P9.4b) with a failing mind-map merge."""
    calls: list[RPCMethod] = []

    class _Runtime:
        async def rpc_call(self, method: RPCMethod, _params: list[Any], **_kwargs: Any) -> Any:
            calls.append(method)
            if method is RPCMethod.LIST_ARTIFACTS:
                return []
            raise failure

    backend = build_web_backend(_Runtime())
    result = await backend.invoke(ARTIFACT_LIST_DEF, ArtifactListInput("nb-1"), deadline=None)
    assert calls == [RPCMethod.LIST_ARTIFACTS, RPCMethod.GET_NOTES_AND_MIND_MAPS]
    return result.artifacts


@pytest.mark.asyncio
async def test_artifact_partial_availability_retains_raw_httpx_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed auth refresh can still surface the original HTTP status leaf."""
    request = httpx.Request("POST", "https://notebooklm.google.com/_/rpc")
    failure = httpx.HTTPStatusError(
        "authentication expired",
        request=request,
        response=httpx.Response(401, request=request),
    )

    with caplog.at_level(logging.WARNING, logger="notebooklm._artifact.listing"):
        result = await _artifact_rows_after_mind_map_failure(monkeypatch, failure)

    assert result == ()
    assert "Failed to fetch mind maps" in caplog.text


@pytest.mark.asyncio
async def test_artifact_partial_availability_does_not_swallow_network_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The legacy net caught raw HTTPError, not the public NetworkError family.

    Through ``invoke()`` (P9.4b: the merge lives in the ``ARTIFACT_LIST`` row)
    the untouched ``NetworkError`` reaches the head and is translated, never
    swallowed into a partial catalog.
    """
    with pytest.raises(BackendError) as caught:
        await _artifact_rows_after_mind_map_failure(
            monkeypatch,
            NetworkError("connection reset"),
        )
    assert caught.value.reason is BackendErrorReason.NETWORK
    assert isinstance(caught.value.__cause__, NetworkError)
    assert str(caught.value.__cause__) == "connection reset"


def test_chat_wire_injects_compat_stripper_without_mutating_codec_global(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A compatibility parse cannot race a direct codec parse via global mutation."""
    entered = threading.Event()
    release = threading.Event()
    calls: list[object] = []
    result = chat_stream.StreamingChatParseResult("answer", [], None)
    original_strip = chat_stream.strip_anti_xssi

    def compat_strip(response_text: str) -> str:
        return response_text

    def blocking_parser(
        response_text: str,
        *,
        _strip_anti_xssi: object = None,
    ) -> chat_stream.StreamingChatParseResult:
        calls.extend((response_text, _strip_anti_xssi))
        entered.set()
        assert release.wait(timeout=1.0)
        return result

    monkeypatch.setattr(chat_wire, "strip_anti_xssi", compat_strip)
    monkeypatch.setattr(chat_stream, "parse_streaming_chat_response", blocking_parser)
    observed: list[chat_stream.StreamingChatParseResult] = []
    worker = threading.Thread(
        target=lambda: observed.append(chat_wire.parse_streaming_chat_response("wire"))
    )
    worker.start()
    try:
        assert entered.wait(timeout=1.0)
        assert chat_stream.strip_anti_xssi is original_strip
    finally:
        release.set()
        worker.join(timeout=1.0)

    assert not worker.is_alive()
    assert observed == [result]
    assert calls == ["wire", compat_strip]


def test_chat_reference_compat_mapping_preserves_positional_identity() -> None:
    """Equal unnumbered references still map positional fallback to its own object."""
    first = ChatReference(source_id="source", chunk_id="chunk")
    second = ChatReference(source_id="source", chunk_id="chunk")
    assert first == second and first is not second

    assert chat_notes._resolve_reference([first, second], 2) is second


def test_backend_session_rejects_foreign_loop_open_and_close() -> None:
    """The private HTTP session fails before touching a foreign loop's client/task."""
    session = WebBackendSession(
        kernel=Kernel(),
        timeout=1.0,
        connect_timeout=1.0,
        limits=ConnectionLimits(),
    )
    owner_loop = asyncio.new_event_loop()
    try:
        owner_loop.run_until_complete(session.open(_generation()))

        with pytest.raises(RuntimeError, match="bound to a different event loop"):
            asyncio.run(session.open(_generation()))
        with pytest.raises(RuntimeError, match="bound to a different event loop"):
            asyncio.run(session.close())

        assert session.is_open
        owner_loop.run_until_complete(session.close())
    finally:
        owner_loop.close()
