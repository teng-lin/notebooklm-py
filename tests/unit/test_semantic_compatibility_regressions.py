"""Focused semantic-backend compatibility and concurrency regressions."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from notebooklm._auth.cookie_types import CookieJar
from notebooklm._backend import BackendError, BackendErrorReason
from notebooklm._kernel import Kernel
from notebooklm._records import MIND_MAP_LIST_DEF, MindMapListInput
from notebooklm._runtime.web_backend_session import WebBackendSession
from notebooklm._studio import StudioCatalog
from notebooklm._web_cookie_provider import WebCookieGeneration
from notebooklm.exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
)
from notebooklm.rpc import RPCMethod
from notebooklm.types import ConnectionLimits
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


def _catalog_after_mind_map_failure(failure: Exception) -> tuple[StudioCatalog, list[RPCMethod]]:
    """Drive ``StudioCatalog``'s merge (P10 R4.2) with a failing supplemental read."""
    calls: list[RPCMethod] = []

    class _Runtime:
        async def rpc_call(self, method: RPCMethod, _params: list[Any], **_kwargs: Any) -> Any:
            calls.append(method)
            if method is RPCMethod.LIST_ARTIFACTS:
                return []
            raise failure

    return StudioCatalog(build_web_backend(_Runtime())), calls


@pytest.mark.asyncio
async def test_artifact_partial_availability_retains_raw_httpx_compatibility(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed auth refresh can still surface the original HTTP status leaf.

    ``invoke`` translates only ``NotebookLMError``, so the raw ``httpx`` leaf
    the auth-refresh path re-raises leaves the port untranslated. The
    ``supplemental`` discriminator lets ``mind_map.list``'s ``map_error`` tag
    exactly this read, and only this read, so the merge can still swallow it.
    """
    request = httpx.Request("POST", "https://notebooklm.google.com/_/rpc")
    failure = httpx.HTTPStatusError(
        "authentication expired",
        request=request,
        response=httpx.Response(401, request=request),
    )
    catalog, calls = _catalog_after_mind_map_failure(failure)

    with caplog.at_level(logging.WARNING, logger="notebooklm._artifact.listing"):
        result = await catalog.list_records("nb-1")

    assert result == ()
    assert calls == [RPCMethod.LIST_ARTIFACTS, RPCMethod.GET_NOTES_AND_MIND_MAPS]
    assert "Failed to fetch mind maps" in caplog.text


@pytest.mark.asyncio
async def test_mind_map_list_keeps_the_raw_httpx_leaf_for_every_other_caller() -> None:
    """The net is scoped to the merge: an ordinary listing still raises raw ``httpx``.

    This is the guard on the discriminator. A row-level ``map_error`` with no
    caller context would turn this into ``BackendError(NETWORK)`` and, through
    ``project_backend_error``, into a public ``NetworkError`` — the exception-type
    change the scoping exists to prevent.
    """
    request = httpx.Request("POST", "https://notebooklm.google.com/_/rpc")
    failure = httpx.HTTPStatusError(
        "authentication expired",
        request=request,
        response=httpx.Response(401, request=request),
    )

    class _Runtime:
        async def rpc_call(self, _method: RPCMethod, _params: list[Any], **_kwargs: Any) -> Any:
            raise failure

    backend = build_web_backend(_Runtime())

    with pytest.raises(httpx.HTTPStatusError) as caught:
        await backend.invoke(MIND_MAP_LIST_DEF, MindMapListInput("nb-1"), deadline=None)
    assert caught.value is failure


@pytest.mark.asyncio
async def test_artifact_partial_availability_does_not_swallow_network_error() -> None:
    """The legacy net caught raw HTTPError, not the public NetworkError family.

    A ``NetworkError`` is already a reviewed native: it is translated by the
    shared path with no ``supplemental_transport_failure`` tag, so the merge's
    reason set — which deliberately omits ``NETWORK`` — lets it through.
    """
    catalog, _calls = _catalog_after_mind_map_failure(NetworkError("connection reset"))

    with pytest.raises(BackendError) as caught:
        await catalog.list_records("nb-1")
    assert caught.value.reason is BackendErrorReason.NETWORK
    assert isinstance(caught.value.__cause__, NetworkError)
    assert str(caught.value.__cause__) == "connection reset"


@pytest.mark.asyncio
async def test_artifact_partial_availability_still_surfaces_schema_drift() -> None:
    """Drift is not a transient outage (#1344): ``DECODING`` is outside the net."""
    catalog, _calls = _catalog_after_mind_map_failure(
        DecodingError("drift", method_id=RPCMethod.GET_NOTES_AND_MIND_MAPS.value)
    )

    with pytest.raises(BackendError) as caught:
        await catalog.list_records("nb-1")
    assert caught.value.reason is BackendErrorReason.DECODING


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        RPCError("rpc boom"),
        ServerError("server boom", status_code=500),
        RateLimitError("slow down", retry_after=3),
        AuthError("expired"),
    ],
)
async def test_artifact_partial_availability_swallows_the_reviewed_rpc_family(
    failure: Exception,
) -> None:
    """Every ``RPCError`` outside the decoding family still yields a partial listing."""
    catalog, _calls = _catalog_after_mind_map_failure(failure)

    assert await catalog.list_records("nb-1") == ()


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
