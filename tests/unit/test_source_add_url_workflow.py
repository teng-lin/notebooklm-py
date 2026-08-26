"""P10 R3.3: ``source.add_url`` is a service-owned workflow over four leaves.

``SourceService.add_url`` owns what the P9.4b custom row owned: the
unconditional ``source.list`` baseline, the YouTube/generic routing, one
``source.register`` url allocation issued with the inner retry loop off, the
reconciling ``source.list`` probe that runs only when that allocation may have
committed, and the best-effort ``source.patch_title`` finalise (hydrating a
null echo through ``source.get``).

These tests are the hoist's oracles, and the two the plan names as its
acceptance gates come first:

* **exact-id-diff attribution** — a probe match is attributable to this call
  only because it is absent from a baseline captured *before* the first create.
  Every way that diff can fail to answer (no baseline, several new matches, a
  probe that could not read) reports an unresolved failure rather than
  guessing, and every one of those reports still names the URL and carries the
  create it could not settle as its context.
* **idempotent-create characterization** — the create is issued once, the probe
  runs only on commit uncertainty, a probe that raises aborts the retry loop,
  and a probe that affirmatively finds nothing is the only thing that lets the
  create be re-issued.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
    may_have_committed,
)
from notebooklm._backend_compat import project_backend_error
from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm._operations import Operation
from notebooklm._records import (
    SOURCE_ADD_URL_DEF,
    SOURCE_GET_DEF,
    SOURCE_LIST_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REGISTER_DEF,
    SourceAddCommitState,
    SourceAddTitleState,
    SourceAddUrlInput,
    SourceAddUrlReceipt,
    SourceGetResult,
    SourceListInput,
    SourceListResult,
    SourcePatchTitleInput,
    SourcePatchTitleResult,
    SourceRecord,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceRegisterResult,
)
from notebooklm._source.upload_payloads import build_template_block
from notebooklm._source_service import SourceService
from notebooklm._web.deadlines import SEMANTIC_DEADLINE_AUTHORITIES
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RPCError,
    ServerError,
    SourceAddError,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.source_add_replay import assert_replays
from tests._fixtures.web_backend import build_web_backend

_NB = "nb"
_ROUTE = f"/notebook/{_NB}"
_URL = "https://example.com/article"
_YOUTUBE = "https://youtu.be/dQw4w9WgXcQ"

_BASE_KWARGS: dict[str, Any] = {
    "_is_retry": False,
    "_retry_deadline": None,
    "allow_null": False,
    "disable_internal_retries": False,
    "operation_variant": None,
    "raise_on_null_status": False,
    "read_timeout": None,
}


@dataclass
class _Call:
    method: RPCMethod
    params: list[Any]
    kwargs: dict[str, Any]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[_Call] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append(_Call(method=method, params=params, kwargs=kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _entry(
    source_id: str,
    *,
    title: str,
    url: str | None = _URL,
    status: int = 2,
    kind: int = 5,
) -> list[Any]:
    metadata = [None, 11, [1704067200, 0], None, kind, None, None, [url] if url else None]
    return [[source_id], title, metadata, [None, status]]


def _snapshot(*entries: list[Any]) -> list[Any]:
    return [["Notebook", list(entries), _NB]]


def _created(*entries: list[Any]) -> list[Any]:
    return [list(entries)]


def _record(source_id: str, *, title: str, url: str | None = _URL) -> SourceRecord:
    return SourceRecord(source_id, title, url=url, kind="web_page", status="ready")


def _web(*responses: object) -> tuple[SourceService, _RecordingExecutor]:
    executor = _RecordingExecutor(*responses)
    return SourceService(build_web_backend(executor)), executor


def _neutral() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(SOURCE_LIST_DEF, SourceListResult(()))
    backend.set_result(SOURCE_REGISTER_DEF, SourceRegisterResult(()))
    backend.set_result(SOURCE_PATCH_TITLE_DEF, SourcePatchTitleResult(None))
    backend.set_result(SOURCE_GET_DEF, SourceGetResult(None))
    backend.set_workflows(Operation.SOURCE_ADD_URL)
    return backend


# --- partition -------------------------------------------------------------------


def test_add_url_is_service_owned_over_its_four_leaves() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SOURCE_ADD_URL]
    assert binding.service_owned is True and binding.is_supported is False
    assert binding.row is None
    assert Operation.SOURCE_ADD_URL in WEB_SERVICE_OWNED_OPERATIONS

    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_URL]
    assert [leaf.operation for leaf in workflow.leaf_operations] == [
        Operation.SOURCE_LIST,
        Operation.SOURCE_REGISTER,
        Operation.SOURCE_PATCH_TITLE,
        Operation.SOURCE_GET,
    ]
    # The registration leaf declares three variants; this workflow reaches
    # exactly one, so the derived native set is still the three the retired row
    # declared — no more, no fewer.
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.ADD_SOURCE, "url"),
        (RPCMethod.GET_NOTEBOOK, None),
        (RPCMethod.UPDATE_SOURCE, None),
    }


def test_the_hoist_preserves_the_url_retry_classification() -> None:
    """The create keeps ``PROBE_THEN_CREATE``: it is what makes the probe legal."""
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_URL]
    (create,) = [
        native for native in workflow.native_bindings if native.method is RPCMethod.ADD_SOURCE
    ]

    assert create.variant == "url"
    assert create.expected_policy is IdempotencyPolicy.PROBE_THEN_CREATE
    assert (
        IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE, operation_variant="url").policy
        is IdempotencyPolicy.PROBE_THEN_CREATE
    )


def test_the_workflow_left_the_client_timeout_deadline_ledger() -> None:
    """The ledger names supported operations only; the service owns the budget now."""
    assert Operation.SOURCE_ADD_URL not in SEMANTIC_DEADLINE_AUTHORITIES


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = _neutral()

    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(SOURCE_ADD_URL_DEF, SourceAddUrlInput(_NB, _URL), deadline=None)


@pytest.mark.asyncio
async def test_an_unsupported_leaf_is_rejected_before_any_side_effect() -> None:
    """Including the title leaves: the gate is checked before the registration."""
    backend = RecordingBackend()
    backend.set_result(SOURCE_LIST_DEF, SourceListResult(()))
    backend.set_result(SOURCE_REGISTER_DEF, SourceRegisterResult(()))

    with pytest.raises(UnsupportedOperationError) as caught:
        await SourceService(backend).add_url(_NB, _URL)

    assert caught.value.operation is Operation.SOURCE_PATCH_TITLE
    assert backend.invocations == []


# --- the happy path ---------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("url", "kind", "source_spec"),
    [
        (
            _URL,
            5,
            [None, None, [_URL], None, None, None, None, None, None, None, 1],
        ),
        (
            _YOUTUBE,
            9,
            [None, None, None, None, None, None, None, [_YOUTUBE], None, None, 1],
        ),
    ],
    ids=["generic", "youtube"],
)
async def test_the_hidden_youtube_dispatch_stays_inside_one_operation(
    url: str,
    kind: int,
    source_spec: list[object],
) -> None:
    service, executor = _web(
        _snapshot(),
        _created(_entry("src-new", title="Upstream", url=url, kind=kind)),
    )

    result = await service.add_url(_NB, url)

    assert (result.source.id, result.source.url) == ("src-new", url)
    assert result.receipt == SourceAddUrlReceipt(
        SourceAddCommitState.CREATED,
        SourceAddTitleState.NOT_REQUESTED,
    )
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]
    assert executor.calls[1].params == [[source_spec], _NB, build_template_block()]


@pytest.mark.asyncio
async def test_the_baseline_create_and_rename_phases_forward_identical_kwargs() -> None:
    """Byte-identical requests, and one shared absolute deadline across phases."""
    service, executor = _web(
        _snapshot(),
        _created(_entry("src", title="Upstream")),
        [_entry("src", title="Requested")],
    )
    deadline = RuntimeDeadline(timeout=30.0, started_at=10.0, monotonic=lambda: 12.0)

    result = await service.add_url(_NB, _URL, requested_title="Requested", deadline=deadline)

    assert result.source.id == "src"
    assert result.receipt.commit_state is SourceAddCommitState.CREATED
    assert result.receipt.title_state is SourceAddTitleState.RENAMED
    baseline, create, rename = executor.calls
    assert baseline.method is RPCMethod.GET_NOTEBOOK
    assert baseline.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "read_timeout": 28.0,
        "_retry_deadline": deadline,
    }
    assert create.method is RPCMethod.ADD_SOURCE
    assert create.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        # The retired row passed ``disable_internal_retries=True`` itself; the
        # leaf leaves the caller flag False and lets the reviewed
        # ``(ADD_SOURCE, "url")`` PROBE_THEN_CREATE row force the inner loop
        # off, which is the same effective dispatch.
        "operation_variant": "url",
        "read_timeout": 28.0,
        "_retry_deadline": deadline,
    }
    assert rename.method is RPCMethod.UPDATE_SOURCE
    assert rename.kwargs == {
        **_BASE_KWARGS,
        "source_path": _ROUTE,
        "allow_null": True,
        "read_timeout": 28.0,
        "_retry_deadline": deadline,
    }


def test_the_create_dispatches_with_internal_retries_effectively_disabled() -> None:
    """A blind re-POST of ADD_SOURCE duplicates; the probe is the only retry.

    The retired row set ``disable_internal_retries=True`` itself. The leaf
    leaves the caller flag False on purpose, because the reviewed
    ``(ADD_SOURCE, "url")`` classification already forces the inner loop off —
    and leaving it False keeps that resolution's unknown-variant guard live.
    """
    from notebooklm._idempotency import resolve_effective_disable_internal_retries

    assert (
        resolve_effective_disable_internal_retries(
            IDEMPOTENCY_REGISTRY,
            RPCMethod.ADD_SOURCE,
            caller_disable_internal_retries=False,
            operation_variant="url",
        )
        is True
    )


@pytest.mark.asyncio
async def test_the_workflow_mints_one_deadline_for_every_phase() -> None:
    """The row ran under one client-timeout budget; the workflow owns it now."""
    service, executor = _web(
        _snapshot(),
        _created(_entry("src", title="Upstream")),
    )
    timeouts: list[float] = []

    def _timeout() -> float:
        timeouts.append(30.0)
        return 30.0

    service = SourceService(service._backend, deadline_factory=RuntimeDeadlineFactory(_timeout))

    await service.add_url(_NB, _URL)

    # One provider read, one absolute identity, threaded through every phase.
    assert timeouts == [30.0]
    (shared,) = {id(call.kwargs["_retry_deadline"]) for call in executor.calls}
    assert isinstance(executor.calls[0].kwargs["_retry_deadline"], RuntimeDeadline)
    assert shared


@pytest.mark.asyncio
async def test_an_explicitly_supplied_deadline_is_never_replaced() -> None:
    service, executor = _web(
        _snapshot(),
        _created(_entry("src", title="Upstream")),
    )
    caller = RuntimeDeadline(timeout=30.0, started_at=10.0, monotonic=lambda: 12.0)
    service = SourceService(
        service._backend,
        deadline_factory=RuntimeDeadlineFactory(lambda: pytest.fail("factory was called")),
    )

    await service.add_url(_NB, _URL, deadline=caller)

    assert {call.kwargs["_retry_deadline"] for call in executor.calls} == {caller}


@pytest.mark.asyncio
async def test_a_pre_dispatch_expiry_names_the_workflow_and_is_not_unconfirmed() -> None:
    service, executor = _web()
    expired = RuntimeDeadline(timeout=2.0, started_at=10.0, monotonic=lambda: 12.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await service.add_url(_NB, _URL, deadline=expired)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_URL
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.dispatched is False
    assert error.outcome_unknown is False
    assert may_have_committed(error) is False
    assert executor.calls == []


@pytest.mark.asyncio
async def test_wait_defers_the_title_to_the_facade_and_polls_nothing() -> None:
    service, executor = _web(
        _snapshot(),
        _created(_entry("src-new", title="Upstream")),
    )

    result = await service.add_url(
        _NB,
        _URL,
        wait=True,
        wait_timeout=17.0,
        requested_title="Requested",
    )

    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]
    assert result.source.title == "Upstream"
    assert result.receipt.title_state is SourceAddTitleState.NOT_ATTEMPTED


@pytest.mark.asyncio
async def test_finalize_title_renames_the_waited_source_under_the_add_operation() -> None:
    service, executor = _web([_entry("src-new", title="Requested")])

    result = await service.finalize_title(_NB, _record("src-new", title="Upstream"), "Requested")

    assert result.source.title == "Requested"
    assert result.receipt == SourceAddUrlReceipt(
        SourceAddCommitState.CREATED,
        SourceAddTitleState.RENAMED,
    )
    assert [call.method for call in executor.calls] == [RPCMethod.UPDATE_SOURCE]


# --- exact-id-diff attribution ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_probe_reconciles_only_a_source_absent_from_the_baseline() -> None:
    """The gate: a pre-existing row with the same URL is never handed back."""
    old = _entry("src-old", title="Old")
    recovered = _entry("src-new", title="Upstream")
    service, executor = _web(
        _snapshot(old),
        ServerError("lost response", status_code=502),
        _snapshot(old, recovered),
        [["src-new"], "Requested"],
    )

    result = await service.add_url(_NB, _URL, requested_title="  Requested  ")

    assert (result.source.id, result.source.title, result.source.url) == (
        "src-new",
        "Requested",
        _URL,
    )
    assert result.receipt == SourceAddUrlReceipt(
        SourceAddCommitState.RECONCILED,
        SourceAddTitleState.RENAMED,
    )
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.UPDATE_SOURCE,
    ]
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 1


@pytest.mark.asyncio
async def test_a_url_already_in_the_notebook_never_satisfies_the_probe() -> None:
    """Baseline-filtered to nothing, the probe reports "did not land" and retries."""
    old = _entry("src-old", title="Old")
    service, executor = _web(
        _snapshot(old),
        ServerError("lost response", status_code=502),
        _snapshot(old),
        _created(_entry("src-new", title="Upstream")),
    )

    result = await service.add_url(_NB, _URL)

    assert result.source.id == "src-new"
    assert result.receipt.commit_state is SourceAddCommitState.CREATED
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 2


@pytest.mark.asyncio
async def test_two_new_matches_are_unresolved_and_name_both_rows() -> None:
    create_error = ServerError("lost response", status_code=502)
    service, _executor = _web(
        _snapshot(),
        create_error,
        _snapshot(_entry("a", title="A"), _entry("b", title="B")),
    )

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_URL
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is True
    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    assert replayed.url == _URL
    assert "probe found 2 new sources with this URL" in str(replayed)
    assert "a ('A'), b ('B')" in str(replayed)
    assert replayed.cause is None and replayed.__cause__ is None
    # The create it could not settle stays the implicit context.
    assert_replays(replayed.__context__, create_error)
    assert replayed.__suppress_context__ is False
    assert getattr(replayed, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_an_unavailable_baseline_makes_any_match_ambiguous() -> None:
    """The baseline read degrades, but the probe then refuses to attribute."""
    baseline_error = RPCError("baseline drift", method_id=RPCMethod.GET_NOTEBOOK.value)
    create_error = ServerError("lost response", status_code=502)
    service, _executor = _web(
        baseline_error,
        create_error,
        _snapshot(_entry("pre-existing", title="Old")),
    )

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    replayed = project_backend_error(caught.value)
    assert type(replayed) is SourceAddError
    assert replayed.url == _URL
    assert "the pre-create baseline snapshot failed (RPCError)" in str(replayed)
    assert "pre-existing ('Old')" in str(replayed)
    # The baseline failure is the *attribute* cause only — the below-port raise
    # had no ``from``, so ``__cause__`` stays unset and the create is context.
    assert_replays(replayed.cause, baseline_error)
    assert replayed.__cause__ is None
    assert_replays(replayed.__context__, create_error)
    assert replayed.__suppress_context__ is False
    assert getattr(replayed, "unconfirmed", False) is True


@pytest.mark.asyncio
async def test_a_degraded_baseline_still_lets_a_clean_create_succeed() -> None:
    service, executor = _web(
        RPCError("baseline drift", method_id=RPCMethod.GET_NOTEBOOK.value),
        _created(_entry("src-new", title="Upstream")),
    )

    result = await service.add_url(_NB, _URL)

    assert result.source.id == "src-new"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]


# --- idempotent-create characterization -------------------------------------------


@pytest.mark.asyncio
async def test_a_probe_that_cannot_read_aborts_instead_of_re_issuing_the_create() -> None:
    """#2220: retrying on an unanswered probe is how duplicates happen."""
    create_error = ServerError("lost response", status_code=502)
    probe_error = DecodingError("probe could not decode", method_id=RPCMethod.GET_NOTEBOOK.value)
    service, executor = _web(_snapshot(), create_error, probe_error)

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_URL
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is True
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 1

    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    assert replayed.url == _URL
    assert str(replayed).startswith("UNRESOLVED — do not blindly retry")
    assert "(DecodingError)" in str(replayed)
    # ``raise SourceAddError(...) from probe`` — the probe is the explicit
    # cause and the suppressed context, and the create sits one level down as
    # the probe's own implicit context.
    assert replayed.__cause__ is replayed.cause
    assert replayed.__context__ is replayed.cause
    assert replayed.__suppress_context__ is True
    assert type(replayed.cause) is DecodingError
    assert_replays(replayed.cause.__context__, create_error)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "probe_error",
    [
        ServerError("probe 503", status_code=503),
        NetworkError("probe disconnected", method_id=RPCMethod.GET_NOTEBOOK.value),
        AuthError("csrf token expired"),
    ],
    ids=["server", "network", "auth"],
)
async def test_a_transport_probe_failure_keeps_its_type_and_is_marked_unconfirmed(
    probe_error: Exception,
) -> None:
    """ADR-0019: callers act on the specific type, so it must not collapse."""
    create_error = ServerError("lost response", status_code=502)
    service, executor = _web(_snapshot(), create_error, probe_error)

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_URL
    assert error.outcome_unknown is True
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 1

    replayed = project_backend_error(error)
    assert type(replayed) is type(probe_error)
    assert not isinstance(replayed, SourceAddError)
    assert replayed.args == probe_error.args
    assert getattr(replayed, "unconfirmed", False) is True
    assert replayed.__cause__ is None
    assert_replays(replayed.__context__, create_error)


@pytest.mark.asyncio
async def test_a_failure_that_cannot_have_committed_never_runs_the_probe() -> None:
    """``AuthError`` on the create: the write never reached a committable state."""
    service, executor = _web(_snapshot(), AuthError("csrf token expired"))

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]
    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_URL
    assert may_have_committed(error) is False
    assert type(project_backend_error(error)) is AuthError


@pytest.mark.asyncio
async def test_an_exhausted_retry_re_raises_the_last_create_failure() -> None:
    second = ServerError("second create lost", status_code=503)
    service, executor = _web(
        _snapshot(),
        ServerError("first create lost", status_code=503),
        _snapshot(),
        second,
        _snapshot(),
    )

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 2
    replayed = project_backend_error(caught.value)
    assert_replays(replayed, second)


# --- failure attribution ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_residual_rpc_error_wraps_into_source_add_error() -> None:
    native = RPCError("request rejected", method_id=RPCMethod.ADD_SOURCE.value, rpc_code=3)
    service, _executor = _web(_snapshot(), native)

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_URL
    assert error.reason is BackendErrorReason.SOURCE_ADD

    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    assert replayed.url == _URL
    assert str(replayed).startswith(f"Failed to add source: {_URL}\nPossible causes:")
    assert_replays(replayed.cause, native)
    assert replayed.__cause__ is replayed.cause
    assert replayed.__context__ is replayed.cause
    assert replayed.__suppress_context__ is True


@pytest.mark.asyncio
async def test_a_re_attributed_create_failure_keeps_its_whole_public_graph() -> None:
    """The reason alone cannot carry every field the retired row preserved.

    A transport create failure must keep its *reason* while the probe is still
    pending — that is what ``semantic_may_have_committed`` reads. Once the probe
    has finished, the workflow reports the leaf's captured graph instead, so
    ``original_error`` and the ``source_id``/``stage`` tags a partial failure
    carries still reach the facade.
    """
    original = httpx.ConnectError("connection reset", request=httpx.Request("POST", "https://x/y"))
    native = NetworkError(
        "create lost",
        method_id=RPCMethod.ADD_SOURCE.value,
        original_error=original,
    )
    native.source_id = "src-maybe"  # type: ignore[attr-defined]
    native.stage = "url-create"  # type: ignore[attr-defined]
    service, executor = _web(_snapshot(), native, _snapshot(), native, _snapshot())

    with pytest.raises(BackendError) as caught:
        await service.add_url(_NB, _URL)

    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 2
    replayed = project_backend_error(caught.value)
    assert type(replayed) is NetworkError
    assert type(replayed.original_error) is httpx.ConnectError
    assert replayed.source_id == "src-maybe"  # type: ignore[attr-defined]
    assert replayed.stage == "url-create"  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_registration_that_names_no_source_reports_the_url() -> None:
    backend = _neutral()
    backend.set_result(SOURCE_REGISTER_DEF, SourceRegisterResult(()))

    with pytest.raises(BackendError) as caught:
        await SourceService(backend).add_url(_NB, _URL)

    replayed = project_backend_error(caught.value)
    assert type(replayed) is SourceAddError
    assert str(replayed) == f"API returned no data for URL: {_URL}"
    assert replayed.url == _URL
    assert replayed.cause is None and replayed.__cause__ is None


@pytest.mark.asyncio
async def test_every_failure_carries_the_commit_and_title_receipt() -> None:
    unknown, _executor = _web(
        _snapshot(),
        ServerError("lost response", status_code=502),
        DecodingError("probe could not decode", method_id=RPCMethod.GET_NOTEBOOK.value),
    )
    with pytest.raises(BackendError) as caught:
        await unknown.add_url(_NB, _URL)
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["receipt"] == SourceAddUrlReceipt(
        SourceAddCommitState.UNKNOWN,
        SourceAddTitleState.NOT_ATTEMPTED,
        outcome_unknown=True,
    )

    failed, _executor = _web(_snapshot(), RPCError("rejected", rpc_code=3))
    with pytest.raises(BackendError) as caught:
        await failed.add_url(_NB, _URL)
    assert caught.value.diagnostics is not None
    assert caught.value.diagnostics["receipt"] == SourceAddUrlReceipt(
        SourceAddCommitState.FAILED,
        SourceAddTitleState.NOT_ATTEMPTED,
        outcome_unknown=False,
    )


@pytest.mark.asyncio
async def test_a_leaf_reason_without_a_captured_graph_is_rebound_not_wrapped() -> None:
    backend = _neutral()
    backend.set_error(
        SOURCE_REGISTER_DEF,
        scripted_error(BackendErrorReason.RPC, operation=Operation.SOURCE_REGISTER),
    )

    with pytest.raises(BackendError) as caught:
        await SourceService(backend).add_url(_NB, _URL)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_URL
    assert error.reason is BackendErrorReason.RPC
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.SOURCE_REGISTER
    assert type(project_backend_error(error)) is RPCError


@pytest.mark.asyncio
async def test_invalid_public_error_evidence_fails_closed() -> None:
    backend = _neutral()
    backend.set_error(
        SOURCE_REGISTER_DEF,
        scripted_error(
            BackendErrorReason.RPC,
            operation=Operation.SOURCE_REGISTER,
            diagnostics={"public_error_failure": "not a record"},
        ),
    )

    with pytest.raises(BackendError, match="invalid public-error evidence"):
        await SourceService(backend).add_url(_NB, _URL)


# --- the best-effort title finalise -----------------------------------------------


@pytest.mark.asyncio
async def test_a_null_rename_echo_is_hydrated_by_id() -> None:
    service, executor = _web(
        _snapshot(),
        _created(_entry("src-new", title="Upstream")),
        None,
        _snapshot(_entry("src-new", title="Requested")),
    )

    result = await service.add_url(_NB, _URL, requested_title="Requested")

    assert result.source.title == "Requested"
    assert result.receipt.title_state is SourceAddTitleState.RENAMED
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
        RPCMethod.UPDATE_SOURCE,
        RPCMethod.GET_NOTEBOOK,
    ]


@pytest.mark.asyncio
async def test_a_rename_failure_is_non_fatal_and_never_reposts_the_create() -> None:
    service, executor = _web(
        _snapshot(),
        _created(_entry("src-new", title="Upstream")),
        ServerError("rename failed", status_code=503),
    )

    result = await service.add_url(_NB, _URL, requested_title="Requested")

    assert result.source.id == "src-new"
    assert result.source.title == "Upstream"
    assert result.receipt.commit_state is SourceAddCommitState.CREATED
    assert result.receipt.title_state is SourceAddTitleState.RENAME_FAILED
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
        RPCMethod.UPDATE_SOURCE,
    ]


@pytest.mark.asyncio
async def test_a_hydration_that_finds_nothing_keeps_the_upstream_title() -> None:
    service, _executor = _web(
        _snapshot(),
        _created(_entry("src-new", title="Upstream")),
        None,
        _snapshot(),
    )

    result = await service.add_url(_NB, _URL, requested_title="Requested")

    assert result.source.title == "Upstream"
    assert result.receipt.title_state is SourceAddTitleState.RENAME_FAILED


@pytest.mark.asyncio
async def test_a_title_the_backend_already_derived_is_not_re_sent() -> None:
    service, executor = _web(
        _snapshot(),
        _created(_entry("src-new", title="Requested")),
    )

    result = await service.add_url(_NB, _URL, requested_title="Requested")

    assert result.receipt.title_state is SourceAddTitleState.UNCHANGED
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]


def test_only_rpc_and_network_shaped_reasons_are_swallowed_by_the_rename() -> None:
    """The swallowed set is exactly ``except (RPCError, NetworkError)``.

    Every member must replay as one of those two families, or the non-fatal
    title phase would be absorbing a failure the caller needed to see.
    """
    from notebooklm._source_add_reports import RENAME_SWALLOWED_REASONS

    # The two not-found reasons name their subject in diagnostics; the rest
    # project from the reason alone.
    evidence: dict[BackendErrorReason, dict[str, object]] = {
        BackendErrorReason.SOURCE_NOT_FOUND: {"source_id": "src-new"},
        BackendErrorReason.NOTEBOOK_NOT_FOUND: {"notebook_id": _NB},
    }
    for reason in RENAME_SWALLOWED_REASONS:
        projected = project_backend_error(
            scripted_error(
                reason,
                operation=Operation.SOURCE_PATCH_TITLE,
                diagnostics=evidence.get(reason),
            )
        )
        assert isinstance(projected, (RPCError, NetworkError)), reason
    # The families the add itself owns are emphatically not swallowed.
    assert BackendErrorReason.SOURCE_ADD not in RENAME_SWALLOWED_REASONS


# --- the neutral request ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_leaf_requests_are_typed_and_hide_nothing_sensitive() -> None:
    backend = _neutral()
    backend.set_result(SOURCE_LIST_DEF, SourceListResult((_record("src-old", title="Old"),)))
    backend.set_result(
        SOURCE_REGISTER_DEF,
        SourceRegisterResult((_record("src-new", title="Upstream"),)),
    )
    backend.set_result(
        SOURCE_PATCH_TITLE_DEF,
        SourcePatchTitleResult(_record("src-new", title="Requested")),
    )

    result = await SourceService(backend).add_url(_NB, _YOUTUBE, requested_title="Requested")

    assert result.source.title == "Requested"
    assert [invocation.operation for invocation in backend.invocations] == [
        Operation.SOURCE_LIST,
        Operation.SOURCE_REGISTER,
        Operation.SOURCE_PATCH_TITLE,
    ]
    listed, registered, patched = (invocation.value for invocation in backend.invocations)
    assert listed == SourceListInput(_NB)
    assert registered == SourceRegisterInput(
        _NB,
        SourceRegisterKind.URL,
        urls=(_YOUTUBE,),
        youtube_flags=(True,),
    )
    assert patched == SourcePatchTitleInput(_NB, "src-new", "Requested")
    # The record hides the new title from ``repr`` exactly as it always did.
    assert "Requested" not in repr(patched)
