"""P10 R3.2: ``source.add_text`` is a service-owned workflow over one leaf.

``SourceService.add_text`` owns what the P9.4b custom row owned: the
non-idempotent refusal, one ``source.register`` text allocation, and the two
ways that write can fail to name a source. It is the source-add family's one
workflow with **no** probe, baseline, reconcile or title finalise — text has no
dedupe key, so there is nothing to reconcile against.

These tests are the hoist's oracles. The three that matter most are the ones
the plan names as its acceptance gates:

* the refusal still reaches the caller as ``NonIdempotentRetryError`` before
  any write — the "created twice" failure mode;
* the transport four-tuple still propagates with its own public type and
  fields (ADR-0019), and only the residual ``RPCError`` wraps into
  ``SourceAddError`` with the original on both ``cause`` and ``__cause__`` —
  the "attributed to the wrong failure" mode;
* the leaf keeps ``(ADD_SOURCE, "text")``'s own ``NON_IDEMPOTENT_NO_RETRY``
  classification, which is what makes collapsing five ledger rows onto one
  keyed leaf safe.
"""

from __future__ import annotations

from typing import Any

import pytest

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
    SOURCE_ADD_TEXT_DEF,
    SOURCE_REGISTER_DEF,
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceAddTextInput,
    SourceRecord,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceRegisterResult,
)
from notebooklm._source_service import SourceService
from notebooklm._web.deadlines import SEMANTIC_DEADLINE_AUTHORITIES
from notebooklm._web.policy import SERVICE_OWNED_WORKFLOW_BINDINGS, derive_workflow_natives
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import (
    AuthError,
    NetworkError,
    NonIdempotentRetryError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.source_add_replay import assert_replays
from tests._fixtures.web_backend import build_web_backend

_NB = "nb_1"
_ROUTE = f"/notebook/{_NB}"
_SOURCE = SourceRecord(id="txt", title="Pasted")


def _source_entry(source_id: str, *, title: str) -> list[Any]:
    metadata = [None, 11, [1704067200, 0], None, 4, None, None, None]
    return [[source_id], title, metadata, [None, 2]]


class _RecordingExecutor:
    def __init__(self, *responses: object) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[RPCMethod, list[Any], dict[str, Any]]] = []

    async def rpc_call(self, method: RPCMethod, params: list[Any], **kwargs: Any) -> Any:
        self.calls.append((method, params, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def _service(*responses: object) -> tuple[SourceService, RecordingBackend]:
    backend = RecordingBackend()
    backend.set_sequence(SOURCE_REGISTER_DEF, list(responses))
    backend.set_workflows(Operation.SOURCE_ADD_TEXT)
    return SourceService(backend), backend


async def _add_text(service: SourceService, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "wait": False,
        "wait_timeout": 120.0,
        "idempotent": False,
    }
    kwargs.update(overrides)
    return await service.add_text(_NB, "Title", "content", **kwargs)


# --- partition -------------------------------------------------------------------


def test_add_text_is_service_owned_over_the_text_registration_leaf() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SOURCE_ADD_TEXT]
    assert binding.service_owned is True and binding.is_supported is False
    assert binding.row is None
    assert Operation.SOURCE_ADD_TEXT in WEB_SERVICE_OWNED_OPERATIONS

    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_TEXT]
    assert [leaf.operation for leaf in workflow.leaf_operations] == [Operation.SOURCE_REGISTER]
    # The leaf declares three registration variants; this workflow reaches
    # exactly one of them, so the derived native set is still the single
    # ``(ADD_SOURCE, "text")`` row the retired handler executed.
    assert derive_workflow_natives(workflow) == {(RPCMethod.ADD_SOURCE, "text")}


def test_the_hoist_preserves_the_text_retry_classification() -> None:
    """The whole point of a per-``NativeChoice`` leaf: text keeps NO_RETRY.

    Flattening ``SOURCE_REGISTER`` to one policy would silently give a create
    path with no dedupe key the inner 5xx/429/network retry loop back.
    """
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_TEXT]
    (native,) = workflow.native_bindings

    assert (native.method, native.variant) == (RPCMethod.ADD_SOURCE, "text")
    assert native.expected_policy is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    assert (
        IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE, operation_variant="text").policy
        is IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY
    )


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = RecordingBackend()
    backend.set_workflows(Operation.SOURCE_ADD_TEXT)

    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(SOURCE_ADD_TEXT_DEF, SourceAddTextInput(_NB, "T", "b"), deadline=None)


@pytest.mark.asyncio
async def test_unsupported_leaf_is_rejected_before_any_side_effect() -> None:
    backend = RecordingBackend()

    with pytest.raises(UnsupportedOperationError) as caught:
        await _add_text(SourceService(backend))

    assert caught.value.operation is Operation.SOURCE_REGISTER
    assert backend.invocations == []


# --- the happy path ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_one_text_registration_returns_the_first_echoed_source() -> None:
    service, backend = _service(SourceRegisterResult((_SOURCE,)))

    result = await _add_text(service)

    assert result.source is _SOURCE
    (invocation,) = backend.invocations
    assert invocation.operation is Operation.SOURCE_REGISTER
    assert invocation.value == SourceRegisterInput(
        _NB, SourceRegisterKind.TEXT, title="Title", content="content"
    )


@pytest.mark.asyncio
async def test_the_wait_flags_start_no_deadline_and_take_no_poll_below_the_port() -> None:
    """Readiness polling stayed the facade's; the workflow only forwards nothing."""
    service, backend = _service(SourceRegisterResult((_SOURCE,)))

    await _add_text(service, wait=True, wait_timeout=9.0)

    (invocation,) = backend.invocations
    assert invocation.deadline is None
    assert invocation.value.kind is SourceRegisterKind.TEXT


@pytest.mark.asyncio
async def test_the_workflow_mints_no_deadline_of_its_own() -> None:
    """``source.add_text`` is deliberately absent from the deadline ledger.

    Every operation with an aggregate budget is named in
    ``SEMANTIC_DEADLINE_AUTHORITIES``; this one is not, and the retired row ran
    under whatever the caller supplied — which the facade leaves ``None``.
    Starting a factory deadline here would invent an expiry path on a
    ``NON_IDEMPOTENT_NO_RETRY`` create.
    """
    assert Operation.SOURCE_ADD_TEXT not in SEMANTIC_DEADLINE_AUTHORITIES

    service = SourceService(RecordingBackend(), deadline_factory=RuntimeDeadlineFactory.fixed(30.0))
    backend = service._backend
    assert isinstance(backend, RecordingBackend)
    backend.set_sequence(SOURCE_REGISTER_DEF, [SourceRegisterResult((_SOURCE,))])
    backend.set_workflows(Operation.SOURCE_ADD_TEXT)

    await _add_text(service)

    assert backend.invocations[0].deadline is None


@pytest.mark.asyncio
async def test_an_explicitly_supplied_deadline_is_threaded_and_its_expiry_rebound() -> None:
    """A caller-owned budget still reaches the leaf, and expiry keeps its subclass."""
    service, backend = _service(SourceRegisterResult((_SOURCE,)))
    caller = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    await _add_text(service, deadline=caller)
    assert backend.invocations[0].deadline is caller

    expired, _ = _service(
        BackendDeadlineExceededError(Operation.SOURCE_REGISTER, outcome_unknown=True)
    )
    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _add_text(expired, deadline=caller)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_TEXT
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.outcome_unknown is True
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.SOURCE_REGISTER


# --- the refusal ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_idempotent_true_is_refused_before_the_leaf_gate_and_any_write() -> None:
    """The duplicate-source failure mode: nothing is dispatched, ever."""
    backend = RecordingBackend()  # no leaf registered at all

    with pytest.raises(BackendError) as caught:
        await _add_text(SourceService(backend), idempotent=True)

    error = caught.value
    assert backend.invocations == []
    assert error.operation is Operation.SOURCE_ADD_TEXT
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.dispatched is False
    assert may_have_committed(error) is False

    replayed = project_backend_error(error)
    assert type(replayed) is NonIdempotentRetryError
    assert "no reliable server-side dedupe key" in str(replayed)
    assert "docs/python-api.md#idempotency" in str(replayed)
    assert replayed.__cause__ is None and replayed.__context__ is None


# --- failure attribution ----------------------------------------------------------


@pytest.mark.parametrize(
    "native",
    [
        RateLimitError("quota exceeded", retry_after=30),
        AuthError("csrf token expired"),
        ServerError("upstream 503"),
        NetworkError("connection reset"),
        RPCTimeoutError("slow"),
    ],
    ids=["rate_limit", "auth", "server", "network", "timeout"],
)
@pytest.mark.asyncio
async def test_transport_failures_reach_the_caller_with_their_own_public_type(
    native: Exception,
) -> None:
    """ADR-0019 catch ordering, preserved through the port as neutral evidence.

    Callers act on the specific type — ``AuthError`` -> re-login,
    ``RateLimitError`` -> back off on ``retry_after``, ``ServerError`` ->
    transient retry — so none of these may collapse into ``SourceAddError``.
    """
    executor = _RecordingExecutor(native)
    service = SourceService(build_web_backend(executor))

    with pytest.raises(BackendError) as caught:
        await _add_text(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_TEXT
    assert error.reason is BackendErrorReason.SOURCE_ADD
    replayed = project_backend_error(error)
    assert not isinstance(replayed, SourceAddError)
    assert_replays(replayed, native)


@pytest.mark.asyncio
async def test_the_residual_rpc_error_wraps_into_source_add_error() -> None:
    """The one leaf the retired ``except RPCError`` wrapped, chain included."""
    native = RPCError("text add failed", method_id=RPCMethod.ADD_SOURCE.value)
    executor = _RecordingExecutor(native)
    service = SourceService(build_web_backend(executor))

    with pytest.raises(BackendError) as caught:
        await _add_text(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_TEXT
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.message == "Failed to add text source 'Title'"

    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    assert replayed.url == "Title"
    assert str(replayed) == "Failed to add text source 'Title'"
    # ``raise SourceAddError(..., cause=e) from e``: the original is on the
    # attribute, the explicit cause and the implicit (suppressed) context.
    assert_replays(replayed.cause, native)
    assert replayed.__cause__ is replayed.cause
    assert replayed.__context__ is replayed.cause
    assert replayed.__suppress_context__ is True


@pytest.mark.asyncio
async def test_an_empty_echo_names_the_title_that_was_not_created() -> None:
    service, _backend = _service(SourceRegisterResult(()))

    with pytest.raises(BackendError) as caught:
        await _add_text(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_TEXT
    assert error.reason is BackendErrorReason.SOURCE_ADD
    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    assert replayed.url == "Title"
    assert str(replayed) == "API returned no data for text source: Title"
    assert replayed.cause is None
    assert replayed.__cause__ is None


def test_only_the_residual_rpc_family_is_wrapped() -> None:
    """The wrapped set is the reviewed catch, not "everything RPC-shaped"."""
    from notebooklm._source_service import _TEXT_WRAPPED_FAILURE_KINDS

    assert {
        SourceAddFailureKind.RPC,
        SourceAddFailureKind.CLIENT,
        SourceAddFailureKind.DECODING,
        SourceAddFailureKind.RESPONSE_TOO_LARGE,
        SourceAddFailureKind.UNKNOWN_RPC_METHOD,
    } == _TEXT_WRAPPED_FAILURE_KINDS
    for unwrapped in (
        SourceAddFailureKind.AUTH,
        SourceAddFailureKind.RATE_LIMIT,
        SourceAddFailureKind.SERVER,
        SourceAddFailureKind.NETWORK,
        SourceAddFailureKind.RPC_TIMEOUT,
    ):
        assert unwrapped not in _TEXT_WRAPPED_FAILURE_KINDS


@pytest.mark.asyncio
async def test_a_leaf_reason_without_a_captured_graph_is_rebound_not_wrapped() -> None:
    """Capturing the public graph is a web convention, not a port requirement.

    Another adapter may report a closed reason and nothing else; the projector
    reconstructs from the reason. The workflow re-attributes it rather than
    inventing a ``SourceAddError`` around evidence it does not have.
    """
    service, _backend = _service(
        scripted_error(BackendErrorReason.SERVER, operation=Operation.SOURCE_REGISTER)
    )

    with pytest.raises(BackendError) as caught:
        await _add_text(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_TEXT
    assert error.reason is BackendErrorReason.SERVER
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.SOURCE_REGISTER
    assert type(project_backend_error(error)) is ServerError


@pytest.mark.asyncio
async def test_invalid_public_error_evidence_fails_closed() -> None:
    """A ``public_error_failure`` of the wrong type is malformed, not absent."""
    service, _backend = _service(
        scripted_error(
            BackendErrorReason.SERVER,
            operation=Operation.SOURCE_REGISTER,
            diagnostics={"public_error_failure": "not a record"},
        )
    )

    with pytest.raises(BackendError, match="invalid public-error evidence"):
        await _add_text(service)


# --- web parity -------------------------------------------------------------------


@pytest.mark.asyncio
async def test_web_request_is_byte_identical_to_the_retired_row() -> None:
    executor = _RecordingExecutor([[_source_entry("txt", title="Pasted")]])
    service = SourceService(build_web_backend(executor))

    result = await _add_text(service)

    assert result.source.id == "txt"
    (method, params, kwargs) = executor.calls[0]
    assert method is RPCMethod.ADD_SOURCE
    assert params == [
        [[None, ["Title", "content"], None, 2, None, None, None, None, None, None, 1]],
        _NB,
        [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
    ]
    assert kwargs == {
        "_is_retry": False,
        "_retry_deadline": None,
        "allow_null": False,
        "disable_internal_retries": False,
        "operation_variant": "text",
        "raise_on_null_status": False,
        "read_timeout": None,
        "source_path": _ROUTE,
    }


@pytest.mark.asyncio
async def test_the_neutral_evidence_never_carries_the_native_object() -> None:
    native = ServerError("down", method_id=RPCMethod.ADD_SOURCE.value)
    executor = _RecordingExecutor(native)
    service = SourceService(build_web_backend(executor))

    with pytest.raises(BackendError) as caught:
        await _add_text(service)

    error = caught.value
    assert error.diagnostics is not None
    record = error.diagnostics["source_add_failure"]
    assert isinstance(record, SourceAddFailureRecord)
    assert project_backend_error(error) is not native
    assert native.dispatched is True  # type: ignore[attr-defined]
    assert native.binding_native.method is RPCMethod.ADD_SOURCE  # type: ignore[attr-defined]
