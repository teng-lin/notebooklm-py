"""Focused regression coverage for idempotent-create provenance (#1988)."""

from __future__ import annotations

import traceback
from collections.abc import Awaitable
from types import TracebackType
from typing import Literal
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._idempotency import (
    _CreateResultKind,
    _IdempotentCreateResult,
    call_unconfirmed_on_transport_loss,
    idempotent_create,
    unresolved_commit_error,
)
from notebooklm._web.sources import WebSourcesAPI
from notebooklm._web.sources.add import SourceAddService
from notebooklm.exceptions import NetworkError, RPCError
from notebooklm.rpc import RPCMethod
from notebooklm.types import Source


@pytest.mark.asyncio
async def test_idempotent_create_marks_fresh_create() -> None:
    result = await idempotent_create(
        AsyncMock(return_value="created"), AsyncMock(return_value=None)
    )
    assert result == _IdempotentCreateResult("created", _CreateResultKind.CREATED)


@pytest.mark.asyncio
async def test_idempotent_create_marks_probe_match_without_retry() -> None:
    create = AsyncMock(side_effect=NetworkError("lost response"))
    probe = AsyncMock(return_value="existing")

    result = await idempotent_create(create, probe)

    assert result.value == "existing"
    assert result.kind is _CreateResultKind.PROBED
    create.assert_awaited_once()
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_idempotent_create_retries_after_probe_miss() -> None:
    create = AsyncMock(side_effect=[NetworkError("first"), "second"])
    probe = AsyncMock(return_value=None)

    result = await idempotent_create(create, probe)

    assert result.value == "second"
    assert result.kind is _CreateResultKind.CREATED
    assert create.await_count == 2
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_idempotent_create_reraises_last_exception_by_identity() -> None:
    error = NetworkError("still unavailable")
    create = AsyncMock(side_effect=[NetworkError("first"), error])

    with pytest.raises(NetworkError) as raised:
        await idempotent_create(create, AsyncMock(return_value=None))

    assert raised.value is error


@pytest.mark.asyncio
@pytest.mark.parametrize(("chain", "suppresses_context"), [("exc", False), (None, True)])
async def test_unconfirmed_call_drops_capability_callback_before_exact_reraise(
    chain: Literal["exc"] | None,
    suppresses_context: bool,
) -> None:
    owner = object()
    session = object()
    bearer = object()
    pipeline = object()
    api = object()
    context = RuntimeError("lower transport context")
    error = NetworkError("response lost", method_id="test-write")
    retained_inner_tracebacks: list[TracebackType] = []

    async def fail(
        capability_owner: object,
        capability_session: object,
        capability_bearer: object,
        capability_pipeline: object,
        capability_api: object,
    ) -> None:
        assert capability_owner is owner
        assert capability_session is session
        assert capability_bearer is bearer
        assert capability_pipeline is pipeline
        assert capability_api is api
        try:
            try:
                raise context
            except RuntimeError:
                raise error  # noqa: B904 - exercise implicit Web exception context
        finally:
            captured = error.__traceback__
            assert captured is not None
            retained_inner_tracebacks.append(captured)

    def call() -> Awaitable[None]:
        return fail(owner, session, bearer, pipeline, api)

    sensitive = (call, owner, session, bearer, pipeline, api)

    def retained_by(value: object) -> tuple[object, ...]:
        closure = getattr(value, "__closure__", None)
        return (value, *(cell.cell_contents for cell in closure or ()))

    retained_before_call = retained_by(call)
    assert all(any(value is item for value in retained_before_call) for item in sensitive)

    with pytest.raises(NetworkError) as raised:
        await call_unconfirmed_on_transport_loss(
            call,
            method="test-write",
            what="the test write",
            chain=chain,
        )

    assert raised.value is error
    assert getattr(raised.value, "unconfirmed", False) is True
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is (context if chain == "exc" else None)
    assert raised.value.__suppress_context__ is suppresses_context

    inspected = []
    traceback_names = []
    for frame, _line in traceback.walk_tb(raised.value.__traceback__):
        traceback_names.append(frame.f_code.co_name)
        source_path = frame.f_code.co_filename.replace("\\", "/")
        if not source_path.endswith("/src/notebooklm/_idempotency.py"):
            continue
        inspected.append(frame.f_code.co_name)
        for value in frame.f_locals.values():
            retained = retained_by(value)
            assert all(all(candidate is not item for candidate in retained) for item in sensitive)
    assert "call_unconfirmed_on_transport_loss" in inspected
    assert ("fail" in traceback_names) is (chain == "exc")

    assert len(retained_inner_tracebacks) == 1
    retained_frames = [frame for frame, _line in traceback.walk_tb(retained_inner_tracebacks[0])]
    assert [frame.f_code.co_name for frame in retained_frames] == ["fail"]
    if chain is None:
        assert all(frame.f_locals == {} for frame in retained_frames)
    else:
        retained_locals = tuple(
            value for frame in retained_frames for value in frame.f_locals.values()
        )
        assert all(any(value is item for value in retained_locals) for item in sensitive[1:])


def test_unresolved_commit_error_does_not_trust_upstream_message_prefix() -> None:
    error = NetworkError("UNRESOLVED upstream proxy response", method_id="upstream")

    wrapped = unresolved_commit_error("web-method", "the test write", error)

    assert type(wrapped) is RPCError
    assert wrapped is not error
    assert wrapped.method_id == "web-method"
    assert "the test write may have committed" in str(wrapped)
    assert "UNRESOLVED upstream proxy response" in str(wrapped)
    assert getattr(wrapped, "unconfirmed", False) is True


def test_unresolved_commit_error_preserves_domain_error_only_when_explicit() -> None:
    domain_error = RPCError("domain-specific reconciliation guidance", method_id="domain")

    preserved = unresolved_commit_error(
        "unused-method",
        "unused write",
        domain_error,
        preserve_exception=True,
    )

    assert preserved is domain_error
    assert getattr(preserved, "unconfirmed", False) is True


def test_unresolved_commit_error_normalizes_rpc_method_id_to_builtin_str() -> None:
    wrapped = unresolved_commit_error(
        RPCMethod.COPY_SOURCES,
        "the source copy",
        NetworkError("response lost"),
    )

    assert wrapped.method_id == RPCMethod.COPY_SOURCES.value
    assert type(wrapped.method_id) is str


@pytest.mark.asyncio
async def test_probed_url_result_honors_the_title_but_does_not_re_wait() -> None:
    """A ``PROBED`` ``add_url`` result is renamed, and never waited on twice.

    #1988 skipped the rename for every ``PROBED`` result because a probe match
    could not be proven to be the caller's own — renaming a stranger's source
    would be a surprise. #2204 filters ``add_url`` probe matches against a
    baseline captured before the create, so a match now *is* provably fresh and
    the requested title must win (the same flip #2113 made for ``add_drive``).

    The wait half of the #1988 contract is unchanged: ``wait`` is handled inside
    the service, so ``SourcesAPI`` must not re-await ``wait_until_ready``.
    """
    api = WebSourcesAPI(MagicMock(), supervisor=MagicMock(), uploader=MagicMock())
    existing = Source(id="existing", title="Upstream title", url="https://example.test")
    api._adder.add_url = AsyncMock(
        return_value=_IdempotentCreateResult(existing, _CreateResultKind.PROBED)
    )
    api.wait_until_ready = AsyncMock(  # type: ignore[method-assign]
        return_value=Source(id="existing", title="Ready")
    )
    api.rename = AsyncMock(  # type: ignore[method-assign]
        return_value=Source(id="existing", title="Retitle me")
    )

    result = await api.add_url("nb", existing.url, title="Retitle me", wait=True)

    assert result.title == "Retitle me"
    api.rename.assert_awaited_once_with("nb", "existing", "Retitle me")
    api.wait_until_ready.assert_not_awaited()


@pytest.mark.asyncio
async def test_private_service_default_return_remains_source() -> None:
    service = SourceAddService()
    source = Source(id="fresh", title="Upstream")

    result = await service.add_url(
        "nb",
        "https://example.test",
        add_youtube_source=AsyncMock(),
        add_url_source=AsyncMock(return_value=[[[["fresh"], "Upstream"]]]),
        list_sources=AsyncMock(return_value=[]),
        wait_until_ready=AsyncMock(),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=MagicMock(),
    )

    assert isinstance(result, Source)
    assert result.id == source.id


@pytest.mark.asyncio
async def test_private_service_preserves_probe_provenance_through_wait() -> None:
    service = SourceAddService()
    existing = Source(id="existing", title="Upstream", url="https://example.test")
    ready = Source(id="existing", title="Ready", url=existing.url)

    result = await service.add_url(
        "nb",
        existing.url,
        wait=True,
        add_youtube_source=AsyncMock(),
        add_url_source=AsyncMock(side_effect=NetworkError("lost response")),
        # Baseline (empty) then probe: the source must be absent before the
        # create for the probe to claim it as this call's own (#2204).
        list_sources=AsyncMock(side_effect=[[], [existing]]),
        wait_until_ready=AsyncMock(return_value=ready),
        extract_youtube_video_id=MagicMock(return_value=None),
        is_youtube_url=MagicMock(return_value=False),
        logger=MagicMock(),
        return_result=True,
    )

    assert isinstance(result, _IdempotentCreateResult)
    assert result.kind is _CreateResultKind.PROBED
    assert result.value is ready


@pytest.mark.asyncio
async def test_idempotent_create_aborts_when_the_probe_raises() -> None:
    """A probe that raises stops the loop — no further create attempt (#2220).

    Pinned at the wrapper's own level, not only through the four in-tree probes:
    this is the seam a fifth ``PROBE_THEN_CREATE`` path would rely on, and the
    contract it depends on is that ``None`` is the *only* way to authorize
    another create.
    """
    create = AsyncMock(side_effect=NetworkError("lost response"))
    probe_error = RuntimeError("probe cannot answer")

    with pytest.raises(RuntimeError) as exc_info:
        await idempotent_create(create, AsyncMock(side_effect=probe_error), max_attempts=3)

    assert exc_info.value is probe_error
    # One attempt, despite max_attempts=3: the probe never said "no match".
    assert create.await_count == 1
    # The transport failure that made the probe run is still reachable.
    assert isinstance(probe_error.__context__, NetworkError)
