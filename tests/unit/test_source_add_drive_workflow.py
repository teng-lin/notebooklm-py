"""P10 R3.4: ``source.add_drive`` is a service-owned workflow over three leaves.

``SourceService.add_drive`` owns what the P9.4b custom row owned: the blank-id
rejection, the unconditional ``source.list`` baseline, one ``source.register``
drive allocation, the reconciling ``source.list`` probe that runs only when
that allocation may have committed, and the best-effort
``source.patch_title`` finalise — which, unlike the URL workflow's, takes a
null echo as the answer instead of hydrating through ``source.get``.

These tests are the hoist's oracles, and the two the plan names as its
acceptance gates come first:

* **exact-id-diff attribution** — a ``documentId`` is not unique inside a
  notebook (the repo's own ``sources_check_freshness_drive`` capture holds two
  source ids sharing one), so a probe match is attributable to this call only
  because it is absent from a baseline captured *before* the first create.
  Every way that diff can fail to answer reports an unresolved failure rather
  than guessing.
* **idempotent-create characterization** — the create is issued once, the probe
  runs only on commit uncertainty, a probe that raises aborts the retry loop,
  and a probe that affirmatively finds nothing is the only thing that lets the
  create be re-issued.

The URL twin lives in ``test_source_add_url_workflow.py``; what is pinned here
is what differs — the ``drive_document_id`` predicate, the Drive wording, the
rejected blank file id, and the un-hydrated finalise.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from scripts._web_policy_intent import SERVICE_OWNED_WORKFLOW_BINDINGS
from scripts.audit_operation_catalog import derive_workflow_natives

from notebooklm._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from notebooklm._idempotency import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm._semantic.backend import (
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    UnsupportedOperationError,
    may_have_committed,
)
from notebooklm._semantic.compat import project_backend_error
from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import (
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REGISTER_DEF,
    SourceAddDriveInput,
    SourceListInput,
    SourceListResult,
    SourcePatchTitleInput,
    SourcePatchTitleResult,
    SourceRecord,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceRegisterResult,
)
from notebooklm._semantic.services.source import SourceService
from notebooklm._web.deadlines import SEMANTIC_DEADLINE_AUTHORITIES
from notebooklm._web.registry import WEB_OPERATION_REGISTRY, WEB_SERVICE_OWNED_OPERATIONS
from notebooklm.exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    RPCError,
    ServerError,
    SourceAddError,
    ValidationError,
)
from notebooklm.rpc import RPCMethod
from tests._fixtures.recording_backend import RecordingBackend, scripted_error
from tests._fixtures.source_add_replay import assert_replays
from tests._fixtures.web_backend import build_web_backend

_NB = "nb"
_ROUTE = f"/notebook/{_NB}"
_FILE_ID = "drive_file_1"
_TITLE = "Drive Doc"
_MIME = "application/vnd.google-apps.document"

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


def _entry(source_id: str, *, file_id: str | None = _FILE_ID, title: str = _TITLE) -> list[Any]:
    """A Drive-backed row, built from the captured wire shape.

    Copied from the live ``GET_NOTEBOOK`` capture in
    ``tests/cassettes/sources_check_freshness_drive.yaml``: the Drive block sits
    at ``metadata[0]`` and **no** URL slot is populated — which is exactly why
    the pre-#2113 URL-based probe could never match one. Decoding a real row
    rather than setting the field keeps this honest: if the ``documentId`` slot
    ever moves, these tests notice.
    """
    metadata: list[Any] = [
        [file_id, "SCRUBBED_AONS", 12] if file_id else None,
        911,
        [1769105469, 316769000],
        ["d4325602-2399-44c2-b45b-9df8f433189f", [1769105982, 178269000]],
        1,  # SourceType.GOOGLE_DOCS
        None,
        1,
    ]
    return [[source_id], title, metadata, [None, 2]]


def _snapshot(*entries: list[Any]) -> list[Any]:
    return [["Notebook", list(entries), _NB]]


def _created(*entries: list[Any]) -> list[Any]:
    return [list(entries)]


def _record(source_id: str, *, title: str = _TITLE, file_id: str | None = _FILE_ID) -> SourceRecord:
    return SourceRecord(
        source_id,
        title,
        kind="google_docs",
        status="ready",
        drive_document_id=file_id,
    )


def _web(*responses: object) -> tuple[SourceService, _RecordingExecutor]:
    executor = _RecordingExecutor(*responses)
    return SourceService(build_web_backend(executor)), executor


def _neutral() -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(SOURCE_LIST_DEF, SourceListResult(()))
    backend.set_result(SOURCE_REGISTER_DEF, SourceRegisterResult(()))
    backend.set_result(SOURCE_PATCH_TITLE_DEF, SourcePatchTitleResult(None))
    backend.set_workflows(Operation.SOURCE_ADD_DRIVE)
    return backend


async def _add(service: SourceService, **kwargs: Any) -> Any:
    defaults: dict[str, Any] = {
        "mime_type": _MIME,
        "wait": False,
        "wait_timeout": 120.0,
    }
    return await service.add_drive(_NB, _FILE_ID, _TITLE, **{**defaults, **kwargs})


# --- partition -------------------------------------------------------------------


def test_add_drive_is_service_owned_over_its_three_leaves() -> None:
    binding = WEB_OPERATION_REGISTRY[Operation.SOURCE_ADD_DRIVE]
    assert binding.service_owned is True and binding.is_supported is False
    assert binding.row is None
    assert Operation.SOURCE_ADD_DRIVE in WEB_SERVICE_OWNED_OPERATIONS

    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_DRIVE]
    # ``source.get`` is absent on purpose: the Drive finalise never hydrated a
    # null ``UPDATE_SOURCE`` echo, so hoisting it must not add that read.
    assert [leaf.operation for leaf in workflow.leaf_operations] == [
        Operation.SOURCE_LIST,
        Operation.SOURCE_REGISTER,
        Operation.SOURCE_PATCH_TITLE,
    ]
    # The registration leaf declares three variants; this workflow reaches
    # exactly one, so the derived native set is still the three the retired row
    # declared — no more, no fewer.
    assert derive_workflow_natives(workflow) == {
        (RPCMethod.ADD_SOURCE, "drive"),
        (RPCMethod.GET_NOTEBOOK, None),
        (RPCMethod.UPDATE_SOURCE, None),
    }


def test_the_hoist_preserves_the_drive_retry_classification() -> None:
    """The create keeps ``PROBE_THEN_CREATE``: it is what makes the probe legal."""
    workflow = SERVICE_OWNED_WORKFLOW_BINDINGS[Operation.SOURCE_ADD_DRIVE]
    (create,) = [
        native for native in workflow.native_bindings if native.method is RPCMethod.ADD_SOURCE
    ]

    assert create.variant == "drive"
    assert create.expected_policy is IdempotencyPolicy.PROBE_THEN_CREATE
    assert (
        IDEMPOTENCY_REGISTRY.get_entry(RPCMethod.ADD_SOURCE, operation_variant="drive").policy
        is IdempotencyPolicy.PROBE_THEN_CREATE
    )


def test_the_workflow_left_the_client_timeout_deadline_ledger() -> None:
    """The ledger names supported operations only; the service owns the budget now."""
    assert Operation.SOURCE_ADD_DRIVE not in SEMANTIC_DEADLINE_AUTHORITIES


@pytest.mark.asyncio
async def test_backend_refuses_the_workflow_directly() -> None:
    backend = _neutral()

    with pytest.raises(UnsupportedOperationError):
        await backend.invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(_NB, _FILE_ID, _TITLE, _MIME),
            deadline=None,
        )


@pytest.mark.asyncio
async def test_the_title_leaf_is_checked_before_any_side_effect() -> None:
    """Including when no rename will be needed: register-then-discover is not allowed."""
    backend = RecordingBackend()
    backend.set_result(SOURCE_LIST_DEF, SourceListResult(()))
    backend.set_result(SOURCE_REGISTER_DEF, SourceRegisterResult(()))

    with pytest.raises(UnsupportedOperationError) as caught:
        await _add(SourceService(backend))

    assert caught.value.operation is Operation.SOURCE_PATCH_TITLE
    assert backend.invocations == []


# --- rejected input --------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("file_id", ["", "   ", "\t\n"], ids=["empty", "spaces", "whitespace"])
async def test_a_blank_file_id_is_refused_before_the_leaf_gate_and_before_any_write(
    file_id: str,
) -> None:
    """A blank id is unmatchable by the probe, so a retry would strand garbage rows."""
    backend = RecordingBackend()  # no leaves registered: the gate is never reached

    with pytest.raises(BackendError) as caught:
        await SourceService(backend).add_drive(
            _NB,
            file_id,
            _TITLE,
            mime_type=_MIME,
            wait=False,
            wait_timeout=120.0,
        )

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is False
    assert backend.invocations == []
    replayed = project_backend_error(error)
    assert type(replayed) is ValidationError
    assert str(replayed) == "Drive file_id cannot be empty or whitespace-only"


# --- the happy path ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_baseline_create_and_rename_phases_forward_identical_kwargs() -> None:
    """Byte-identical requests, and one shared absolute deadline across phases."""
    service, executor = _web(
        _snapshot(),
        _created(_entry("drv", title="Real Drive Name.pdf")),
        [_entry("drv", title=_TITLE)],
    )
    deadline = RuntimeDeadline(timeout=30.0, started_at=10.0, monotonic=lambda: 12.0)

    result = await _add(service, deadline=deadline)

    assert (result.source.id, result.source.title) == ("drv", _TITLE)
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
        # Null echoes stay legal for a Drive allocation. The retired row also
        # passed ``disable_internal_retries=True`` itself; the leaf leaves the
        # caller flag False and lets the reviewed ``(ADD_SOURCE, "drive")``
        # PROBE_THEN_CREATE row force the inner loop off — the same effective
        # dispatch, and the same request body.
        "allow_null": True,
        "operation_variant": "drive",
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
    """A blind re-POST of ADD_SOURCE duplicates; the probe is the only retry."""
    from notebooklm._idempotency import resolve_effective_disable_internal_retries

    assert (
        resolve_effective_disable_internal_retries(
            IDEMPOTENCY_REGISTRY,
            RPCMethod.ADD_SOURCE,
            caller_disable_internal_retries=False,
            operation_variant="drive",
        )
        is True
    )


@pytest.mark.asyncio
async def test_the_workflow_mints_one_deadline_for_every_phase() -> None:
    """The row ran under one client-timeout budget; the workflow owns it now."""
    service, executor = _web(_snapshot(), _created(_entry("drv")))
    timeouts: list[float] = []

    def _timeout() -> float:
        timeouts.append(30.0)
        return 30.0

    service = SourceService(service._backend, deadline_factory=RuntimeDeadlineFactory(_timeout))

    await _add(service)

    assert timeouts == [30.0]
    (shared,) = {id(call.kwargs["_retry_deadline"]) for call in executor.calls}
    assert isinstance(executor.calls[0].kwargs["_retry_deadline"], RuntimeDeadline)
    assert shared


@pytest.mark.asyncio
async def test_a_pre_dispatch_expiry_names_the_workflow_and_is_not_unconfirmed() -> None:
    service, executor = _web()
    expired = RuntimeDeadline(timeout=2.0, started_at=10.0, monotonic=lambda: 12.0)

    with pytest.raises(BackendDeadlineExceededError) as caught:
        await _add(service, deadline=expired)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
    assert error.reason is BackendErrorReason.TIMEOUT
    assert error.dispatched is False
    assert error.outcome_unknown is False
    assert may_have_committed(error) is False
    assert executor.calls == []


@pytest.mark.asyncio
async def test_a_requested_title_is_applied_after_the_backend_re_derives_it() -> None:
    """#1960: Drive re-derives the display title, so the add's own title is ignored."""
    service, executor = _web(
        _snapshot(),
        _created(_entry("drv", title="Real Drive Name.pdf")),
        [_entry("drv", title="Chosen")],
    )

    result = await service.add_drive(
        _NB, _FILE_ID, "  Chosen  ", mime_type=_MIME, wait=False, wait_timeout=120.0
    )

    assert result.source.title == "Chosen"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
        RPCMethod.UPDATE_SOURCE,
    ]


@pytest.mark.asyncio
async def test_wait_defers_the_title_to_the_facade_and_polls_nothing() -> None:
    service, executor = _web(_snapshot(), _created(_entry("drv", title="Upstream")))

    result = await _add(service, wait=True, wait_timeout=17.0)

    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]
    assert result.source.title == "Upstream"


@pytest.mark.asyncio
async def test_finalize_drive_title_renames_the_waited_source_under_the_add_operation() -> None:
    service, executor = _web([_entry("drv", title="Requested")])

    result = await service.finalize_drive_title(_NB, _record("drv", title="Upstream"), "Requested")

    assert result.source.title == "Requested"
    assert [call.method for call in executor.calls] == [RPCMethod.UPDATE_SOURCE]


# --- exact-id-diff attribution ----------------------------------------------------


@pytest.mark.asyncio
async def test_the_probe_reconciles_only_a_source_absent_from_the_baseline() -> None:
    """The gate: a pre-existing row with the same ``documentId`` is never handed back."""
    old = _entry("src-old", title="Old")
    recovered = _entry("src-new", title="Upstream")
    service, executor = _web(
        _snapshot(old),
        ServerError("lost response", status_code=502),
        _snapshot(old, recovered),
        [_entry("src-new", title="Chosen")],
    )

    result = await service.add_drive(
        _NB, _FILE_ID, "Chosen", mime_type=_MIME, wait=False, wait_timeout=120.0
    )

    assert (result.source.id, result.source.title) == ("src-new", "Chosen")
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.UPDATE_SOURCE,
    ]
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 1


@pytest.mark.asyncio
async def test_a_document_already_in_the_notebook_never_satisfies_the_probe() -> None:
    """Baseline-filtered to nothing, the probe reports "did not land" and retries."""
    old = _entry("src-old", title="Old")
    service, executor = _web(
        _snapshot(old),
        ServerError("lost response", status_code=502),
        _snapshot(old),
        _created(_entry("src-new")),
    )

    result = await _add(service)

    assert result.source.id == "src-new"
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 2


@pytest.mark.asyncio
async def test_a_non_drive_row_never_matches_the_requested_file_id() -> None:
    """``drive_document_id is None`` must not collide with any requested id."""
    service, executor = _web(
        _snapshot(),
        ServerError("lost response", status_code=502),
        _snapshot(_entry("web", file_id=None, title="A web page")),
        _created(_entry("src-new")),
    )

    result = await _add(service)

    assert result.source.id == "src-new"
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 2


@pytest.mark.asyncio
async def test_a_prefix_collision_never_matches() -> None:
    """Exact equality, not a substring test: ``abc`` must not match ``abcdef``."""
    service, executor = _web(
        _snapshot(),
        ServerError("lost response", status_code=502),
        _snapshot(_entry("other", file_id=_FILE_ID + "_extra", title="Neighbour")),
        _created(_entry("src-new")),
    )

    result = await _add(service)

    assert result.source.id == "src-new"
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 2


@pytest.mark.asyncio
async def test_two_new_matches_are_unresolved_and_name_the_document() -> None:
    """Two rows sharing a ``documentId`` cannot be told apart — raise, don't pick."""
    create_error = ServerError("lost response", status_code=502)
    service, _executor = _web(
        _snapshot(),
        create_error,
        _snapshot(_entry("src-a", title="A"), _entry("src-b", title="B")),
    )

    with pytest.raises(BackendError) as caught:
        await _add(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is True
    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    # ``SourceAddError``'s first argument was the requested title below the
    # port, while the message names the file id — both are preserved.
    assert replayed.url == _TITLE
    assert f"Cannot disambiguate Drive source {_FILE_ID!r}" in str(replayed)
    assert "probe found 2 new sources with this documentId" in str(replayed)
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
        await _add(service)

    replayed = project_backend_error(caught.value)
    assert type(replayed) is SourceAddError
    assert replayed.url == _TITLE
    assert "the pre-create baseline snapshot failed (RPCError)" in str(replayed)
    assert "Check the notebook source list before retrying." in str(replayed)
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
        _created(_entry("src-new")),
    )

    result = await _add(service)

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
        await _add(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
    assert error.reason is BackendErrorReason.SOURCE_ADD
    assert error.outcome_unknown is True
    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 1

    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    assert replayed.url == _TITLE
    assert str(replayed).startswith("UNRESOLVED — do not blindly retry")
    assert f"Cannot confirm Drive source {_FILE_ID!r}" in str(replayed)
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
        await _add(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
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
        await _add(service)

    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]
    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
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
        await _add(service)

    assert sum(call.method is RPCMethod.ADD_SOURCE for call in executor.calls) == 2
    replayed = project_backend_error(caught.value)
    assert_replays(replayed, second)


# --- failure attribution ----------------------------------------------------------


@pytest.mark.asyncio
async def test_the_residual_rpc_error_wraps_into_source_add_error() -> None:
    native = RPCError("request rejected", method_id=RPCMethod.ADD_SOURCE.value, rpc_code=3)
    service, _executor = _web(_snapshot(), native)

    with pytest.raises(BackendError) as caught:
        await _add(service)

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
    assert error.reason is BackendErrorReason.SOURCE_ADD

    replayed = project_backend_error(error)
    assert type(replayed) is SourceAddError
    # The retired handler raised ``SourceAddError(title, cause=e)`` with no
    # explicit message, so the default template names the *title*.
    assert replayed.url == _TITLE
    assert str(replayed).startswith(f"Failed to add source: {_TITLE}\nPossible causes:")
    assert_replays(replayed.cause, native)
    assert replayed.__cause__ is replayed.cause
    assert replayed.__context__ is replayed.cause
    assert replayed.__suppress_context__ is True


@pytest.mark.asyncio
async def test_a_registration_that_echoes_nothing_steers_at_the_upload_path() -> None:
    """A Drive allocation may legally echo null; for this variant that is a refusal."""
    service, _executor = _web(_snapshot(), None)

    with pytest.raises(BackendError) as caught:
        await service.add_drive(
            _NB, _FILE_ID, _TITLE, mime_type="application/epub+zip", wait=False, wait_timeout=1.0
        )

    replayed = project_backend_error(caught.value)
    assert type(replayed) is SourceAddError
    assert replayed.url == _TITLE
    message = str(replayed)
    assert f"API returned no data for Drive source: {_TITLE}" in message
    assert "mime_type='application/epub+zip'" in message
    assert "may not be importable" in message
    assert "download it and add it as a `file` source instead." in message
    assert replayed.cause is None and replayed.__cause__ is None


@pytest.mark.asyncio
async def test_a_leaf_reason_without_a_captured_graph_is_rebound_not_wrapped() -> None:
    backend = _neutral()
    backend.set_error(
        SOURCE_REGISTER_DEF,
        scripted_error(BackendErrorReason.RPC, operation=Operation.SOURCE_REGISTER),
    )

    with pytest.raises(BackendError) as caught:
        await _add(SourceService(backend))

    error = caught.value
    assert error.operation is Operation.SOURCE_ADD_DRIVE
    assert error.reason is BackendErrorReason.RPC
    assert error.diagnostics is not None
    assert error.diagnostics["leaf_operation"] is Operation.SOURCE_REGISTER
    assert type(project_backend_error(error)) is RPCError


@pytest.mark.asyncio
async def test_no_drive_failure_carries_the_url_workflows_receipt() -> None:
    """Only ``SourceAddUrlResult`` has a receipt; the Drive row attached none."""
    service, _executor = _web(_snapshot(), RPCError("rejected", rpc_code=3))

    with pytest.raises(BackendError) as caught:
        await _add(service)

    assert "receipt" not in (caught.value.diagnostics or {})


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
        await _add(SourceService(backend))


# --- the best-effort title finalise -----------------------------------------------


@pytest.mark.asyncio
async def test_a_null_rename_echo_is_not_hydrated_and_keeps_the_requested_title() -> None:
    """The Drive row never read the renamed source back; the hoist must not start."""
    service, executor = _web(
        _snapshot(),
        _created(_entry("drv", title="Upstream")),
        None,  # UPDATE_SOURCE echoes nothing
    )

    result = await service.add_drive(
        _NB, _FILE_ID, "Requested", mime_type=_MIME, wait=False, wait_timeout=120.0
    )

    assert result.source.title == "Requested"
    # Crucially no trailing GET_NOTEBOOK: hydrating would add a recency write
    # and a SourceNotFoundError branch this phase never had.
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
        RPCMethod.UPDATE_SOURCE,
    ]


@pytest.mark.asyncio
async def test_a_rename_failure_is_non_fatal_and_never_reposts_the_create() -> None:
    service, executor = _web(
        _snapshot(),
        _created(_entry("drv", title="Upstream")),
        ServerError("rename failed", status_code=503),
    )

    result = await service.add_drive(
        _NB, _FILE_ID, "Requested", mime_type=_MIME, wait=False, wait_timeout=120.0
    )

    assert result.source.id == "drv"
    assert result.source.title == "Upstream"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
        RPCMethod.UPDATE_SOURCE,
    ]


@pytest.mark.asyncio
async def test_a_title_the_backend_already_derived_is_not_re_sent() -> None:
    service, executor = _web(_snapshot(), _created(_entry("drv", title="Requested")))

    result = await service.add_drive(
        _NB, _FILE_ID, "Requested", mime_type=_MIME, wait=False, wait_timeout=120.0
    )

    assert result.source.title == "Requested"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]


@pytest.mark.asyncio
async def test_an_empty_title_skips_the_finalise_entirely() -> None:
    service, executor = _web(_snapshot(), _created(_entry("drv", title="Real Drive Name")))

    result = await service.add_drive(
        _NB, _FILE_ID, "", mime_type=_MIME, wait=False, wait_timeout=120.0
    )

    assert result.source.title == "Real Drive Name"
    assert [call.method for call in executor.calls] == [
        RPCMethod.GET_NOTEBOOK,
        RPCMethod.ADD_SOURCE,
    ]


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

    result = await service_add(backend)

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
        SourceRegisterKind.DRIVE,
        title="Requested",
        file_id=_FILE_ID,
        mime_type=_MIME,
    )
    assert patched == SourcePatchTitleInput(_NB, "src-new", "Requested")
    # The record hides the new title from ``repr`` exactly as it always did.
    assert "Requested" not in repr(patched)


async def service_add(backend: RecordingBackend) -> Any:
    return await SourceService(backend).add_drive(
        _NB, _FILE_ID, "Requested", mime_type=_MIME, wait=False, wait_timeout=120.0
    )
