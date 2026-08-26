"""Focused regression coverage for idempotent-create provenance (#1988)."""

from __future__ import annotations

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._backend import BackendError, BackendErrorReason
from notebooklm._idempotency import (
    _CreateResultKind,
    _IdempotentCreateResult,
    transport_may_have_committed,
)
from notebooklm._idempotency import idempotent_create as adapter_idempotent_create
from notebooklm._idempotency_create import (
    idempotent_create,
    semantic_may_have_committed,
)
from notebooklm._operations import Operation
from notebooklm._records import (
    SOURCE_GET_DEF,
    SOURCE_LIST_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REGISTER_DEF,
    SourceGetResult,
    SourceListResult,
    SourcePatchTitleResult,
    SourceRecord,
)
from notebooklm._sources import SourcesAPI
from notebooklm.exceptions import NetworkError, ServerError
from notebooklm.types import Source
from tests._fixtures.recording_backend import RecordingBackend, scripted_error


@pytest.mark.asyncio
async def test_idempotent_create_marks_fresh_create() -> None:
    result = await idempotent_create(
        AsyncMock(return_value="created"),
        AsyncMock(return_value=None),
        may_have_committed=transport_may_have_committed,
    )
    assert result == _IdempotentCreateResult("created", _CreateResultKind.CREATED)


@pytest.mark.asyncio
async def test_idempotent_create_marks_probe_match_without_retry() -> None:
    create = AsyncMock(side_effect=NetworkError("lost response"))
    probe = AsyncMock(return_value="existing")

    result = await idempotent_create(create, probe, may_have_committed=transport_may_have_committed)

    assert result.value == "existing"
    assert result.kind is _CreateResultKind.PROBED
    create.assert_awaited_once()
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_idempotent_create_retries_after_probe_miss() -> None:
    create = AsyncMock(side_effect=[NetworkError("first"), "second"])
    probe = AsyncMock(return_value=None)

    result = await idempotent_create(create, probe, may_have_committed=transport_may_have_committed)

    assert result.value == "second"
    assert result.kind is _CreateResultKind.CREATED
    assert create.await_count == 2
    assert probe.await_count == 1


@pytest.mark.asyncio
async def test_idempotent_create_reraises_last_exception_by_identity() -> None:
    error = NetworkError("still unavailable")
    create = AsyncMock(side_effect=[NetworkError("first"), error])

    with pytest.raises(NetworkError) as raised:
        await idempotent_create(
            create,
            AsyncMock(return_value=None),
            may_have_committed=transport_may_have_committed,
        )

    assert raised.value is error


@pytest.mark.asyncio
async def test_probed_url_result_preserves_one_facade_wait() -> None:
    """A recovered URL add still waits exactly once, and honors the title.

    #2204 flipped ``add_url`` to honoring a requested title on a ``PROBED``
    result, because its baseline diff proves the match is this call's. P10 R3.3
    hoisted the workflow above the port, so the probe is now a ``source.list``
    the service sequences — but the facade contract is unchanged: exactly one
    ``wait_until_ready``, and the deferred title applied through
    ``finalize_title`` afterwards.
    """
    backend = RecordingBackend()
    existing = SourceRecord(id="existing", title="Upstream", url="https://example.test")
    renamed = SourceRecord(id="existing", title="Retitle me", url=existing.url)
    backend.set_sequence(
        SOURCE_LIST_DEF,
        [
            # Baseline (empty) then probe: the source must be absent before the
            # create for the probe to claim it as this call's own (#2204).
            SourceListResult(()),
            SourceListResult((existing,)),
        ],
    )
    backend.set_error(
        SOURCE_REGISTER_DEF,
        scripted_error(
            BackendErrorReason.SERVER,
            operation=Operation.SOURCE_REGISTER,
            dispatched=True,
        ),
    )
    backend.set_result(SOURCE_PATCH_TITLE_DEF, SourcePatchTitleResult(renamed))
    backend.set_result(SOURCE_GET_DEF, SourceGetResult(renamed))
    backend.set_workflows(Operation.SOURCE_ADD_URL)
    api = SourcesAPI(MagicMock(), uploader=MagicMock(), _backend=backend)
    api.wait_until_ready = AsyncMock(  # type: ignore[method-assign]
        return_value=Source(id="existing", title="Upstream")
    )

    result = await api.add_url(
        "nb",
        "https://example.test",
        title="Retitle me",
        wait=True,
    )

    assert result.title == "Retitle me"
    assert [invocation.operation for invocation in backend.invocations] == [
        Operation.SOURCE_LIST,
        Operation.SOURCE_REGISTER,
        Operation.SOURCE_LIST,
        Operation.SOURCE_PATCH_TITLE,
    ]
    api.wait_until_ready.assert_awaited_once_with("nb", "existing", timeout=120.0)


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
        await idempotent_create(
            create,
            AsyncMock(side_effect=probe_error),
            may_have_committed=transport_may_have_committed,
            max_attempts=3,
        )

    assert exc_info.value is probe_error
    # One attempt, despite max_attempts=3: the probe never said "no match".
    assert create.await_count == 1
    # The transport failure that made the probe run is still reachable.
    assert isinstance(probe_error.__context__, NetworkError)


# ---------------------------------------------------------------------------
# One implementation, two predicates (P9.2 contract 2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_predicate_probes_on_a_dispatched_commit_uncertain_backend_error() -> None:
    create = AsyncMock(
        side_effect=BackendError("5xx", reason=BackendErrorReason.SERVER, dispatched=True)
    )
    probe = AsyncMock(return_value="existing")

    result = await idempotent_create(create, probe, may_have_committed=semantic_may_have_committed)

    assert result.value == "existing"
    assert result.kind is _CreateResultKind.PROBED
    probe.assert_awaited_once()


@pytest.mark.asyncio
async def test_semantic_retry_warning_reports_the_reviewed_public_cause(
    caplog: pytest.LogCaptureFixture,
) -> None:
    native = ServerError("server response lost")
    backend_error = BackendError(
        "server response lost",
        reason=BackendErrorReason.SERVER,
        dispatched=True,
    )
    try:
        raise backend_error from native
    except BackendError as chained:
        backend_error = chained

    with caplog.at_level(logging.WARNING, logger="notebooklm._idempotency"):
        result = await idempotent_create(
            AsyncMock(side_effect=backend_error),
            AsyncMock(return_value="existing"),
            may_have_committed=semantic_may_have_committed,
            label="notebook.create['Daily News']",
        )

    assert result.value == "existing"
    warning = next(
        record.getMessage()
        for record in caplog.records
        if "failed with transport error" in record.getMessage()
    )
    assert warning == (
        "notebook.create['Daily News'] attempt 1/2 failed with transport error "
        "(ServerError); probing for server-side commit before retry"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(
            BackendError("pre-dispatch", reason=BackendErrorReason.SERVER, dispatched=False),
            id="not-dispatched",
        ),
        pytest.param(
            BackendError("rejected", reason=BackendErrorReason.RPC, dispatched=True),
            id="dispatched-but-rejected",
        ),
        pytest.param(NetworkError("raw transport error"), id="raw-transport-error"),
    ],
)
async def test_semantic_predicate_does_not_probe_when_nothing_could_have_committed(
    error: Exception,
) -> None:
    create = AsyncMock(side_effect=error)
    probe = AsyncMock(return_value="existing")

    with pytest.raises(type(error)) as raised:
        await idempotent_create(create, probe, may_have_committed=semantic_may_have_committed)

    assert raised.value is error
    create.assert_awaited_once()
    probe.assert_not_awaited()


@pytest.mark.asyncio
async def test_adapter_predicate_ignores_backend_records_and_keeps_the_class_tuple() -> None:
    backend_error = BackendError("5xx", reason=BackendErrorReason.SERVER, dispatched=True)
    probe = AsyncMock(return_value="existing")

    with pytest.raises(BackendError):
        await idempotent_create(
            AsyncMock(side_effect=backend_error),
            probe,
            may_have_committed=transport_may_have_committed,
        )
    probe.assert_not_awaited()

    assert transport_may_have_committed(NetworkError("x")) is True
    assert semantic_may_have_committed(NetworkError("x")) is False


@pytest.mark.asyncio
async def test_adapter_entry_point_requires_a_named_predicate() -> None:
    """``_idempotency.idempotent_create`` has no default predicate (P9.3).

    Every caller names its commit-uncertainty predicate; the adapter entry
    point with the transport tuple keeps today's class-tuple behaviour.
    """
    create = AsyncMock(side_effect=NetworkError("lost response"))
    probe = AsyncMock(return_value="existing")

    with pytest.raises(TypeError, match="may_have_committed"):
        await adapter_idempotent_create(create, probe)  # type: ignore[call-arg]
    probe.assert_not_awaited()

    result = await adapter_idempotent_create(
        create, probe, may_have_committed=transport_may_have_committed
    )

    assert result.kind is _CreateResultKind.PROBED
    probe.assert_awaited_once()
