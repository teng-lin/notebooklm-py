"""P9.2 neutral commit-uncertainty contracts on the semantic backend port.

``outcome_unknown`` keeps its broad meaning (the workflow's final outcome is
unconfirmed); ``dispatched`` is the narrow reconciliation *trigger*.  The truth
table below mirrors ``test_semantic_outcome_unknown_readback.py`` case for case
and adds the dispatched rows that a service probes on.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from notebooklm._backend import (
    COMMIT_UNCERTAIN_REASONS,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    BackendKind,
    UnsupportedOperationError,
    mark_backend_outcome_unknown,
    may_have_committed,
    rebind_operation,
    require_leaves,
)
from notebooklm._operations import Operation
from notebooklm._records import (
    NOTEBOOK_GET_DEF,
    NOTEBOOK_LIST_DEF,
    NotebookGetResult,
    NotebookListResult,
    NotebookRecord,
)
from tests._fixtures.recording_backend import RecordingBackend, scripted_error

# ---------------------------------------------------------------------------
# Truth table
# ---------------------------------------------------------------------------
#
# Columns: prior write in this workflow · current call dispatched · what the
# probe/readback did · expiry before or after dispatch → outcome_unknown,
# may_have_committed.  The first block reproduces every case of
# ``test_expiry_after_a_write_is_truthfully_unconfirmed`` (a readback or second
# write blocked *before* dispatch after an earlier write landed): the workflow
# is unconfirmed, but the blocked leaf itself cannot have committed.  The second
# block reproduces ``test_expiry_after_a_read_only_preflight_remains_confirmed``
# (nothing written, nothing dispatched).  The remaining rows are the dispatched
# cells a service reconciles on.

_READBACK_CASES = [
    ("sharing-visibility-readback", Operation.SHARING_SET_PUBLIC),
    ("sharing-view-readback", Operation.SHARING_SET_VIEW_LEVEL),
    ("sharing-users-readback", Operation.SHARING_UPDATE_USERS),
    ("artifact-rename-readback", Operation.ARTIFACT_RENAME),
    ("source-update-null-readback", Operation.SOURCE_UPDATE),
    ("collection-create-readback", Operation.COLLECTION_CREATE),
    ("label-field-readback", Operation.LABEL_UPDATE),
    ("collection-field-readback", Operation.COLLECTION_UPDATE),
    ("label-membership-readback", Operation.LABEL_UPDATE),
    ("collection-membership-readback", Operation.COLLECTION_UPDATE),
    ("label-second-membership-write", Operation.LABEL_UPDATE),
    ("collection-second-membership-write", Operation.COLLECTION_UPDATE),
]

_PREFLIGHT_CASES = [
    ("label-preflight", Operation.LABEL_UPDATE),
    ("collection-baseline", Operation.COLLECTION_CREATE),
]


@pytest.mark.parametrize(
    ("case", "operation"),
    [pytest.param(case, operation, id=case) for case, operation in _READBACK_CASES],
)
def test_prior_write_then_expiry_before_dispatch_is_unconfirmed_but_not_committing(
    case: str, operation: Operation
) -> None:
    del case
    error = BackendDeadlineExceededError(operation, outcome_unknown=True, dispatched=False)

    assert error.outcome_unknown is True
    assert error.reason is BackendErrorReason.TIMEOUT
    assert may_have_committed(error) is False


@pytest.mark.parametrize(
    ("case", "operation"),
    [pytest.param(case, operation, id=case) for case, operation in _PREFLIGHT_CASES],
)
def test_read_only_preflight_expiry_is_confirmed_and_not_committing(
    case: str, operation: Operation
) -> None:
    del case
    error = BackendDeadlineExceededError(operation, outcome_unknown=False, dispatched=False)

    assert error.outcome_unknown is False
    assert may_have_committed(error) is False


@pytest.mark.parametrize(
    (
        "prior_write",
        "dispatched",
        "probe_or_readback",
        "expiry",
        "reason",
        "expected_outcome_unknown",
        "expected_may_have_committed",
    ),
    [
        # prior · dispatched · probe/readback · expiry · reason → (unknown, committed)
        pytest.param(
            False,
            True,
            "not run",
            "after dispatch",
            BackendErrorReason.TIMEOUT,
            False,
            True,
            id="mutation-timed-out-after-dispatch",
        ),
        pytest.param(
            False,
            True,
            "not run",
            "none",
            BackendErrorReason.SERVER,
            False,
            True,
            id="mutation-server-error",
        ),
        pytest.param(
            False,
            True,
            "not run",
            "none",
            BackendErrorReason.NETWORK,
            False,
            True,
            id="mutation-network-error",
        ),
        pytest.param(
            False,
            True,
            "not run",
            "none",
            BackendErrorReason.RATE_LIMIT,
            False,
            True,
            id="mutation-rate-limited",
        ),
        pytest.param(
            False,
            True,
            "could not answer",
            "none",
            BackendErrorReason.SERVER,
            True,
            True,
            id="mutation-server-error-probe-unanswered",
        ),
        pytest.param(
            True,
            True,
            "readback expired",
            "after dispatch",
            BackendErrorReason.TIMEOUT,
            True,
            True,
            id="readback-timed-out-after-dispatch",
        ),
        pytest.param(
            True,
            False,
            "readback blocked",
            "before dispatch",
            BackendErrorReason.TIMEOUT,
            True,
            False,
            id="readback-blocked-before-dispatch",
        ),
        pytest.param(
            False,
            False,
            "not run",
            "before dispatch",
            BackendErrorReason.TIMEOUT,
            False,
            False,
            id="mutation-blocked-before-dispatch",
        ),
        pytest.param(
            False,
            True,
            "not run",
            "none",
            BackendErrorReason.AUTH,
            False,
            False,
            id="mutation-rejected-auth",
        ),
        pytest.param(
            False,
            True,
            "not run",
            "none",
            BackendErrorReason.RPC,
            False,
            False,
            id="mutation-rejected-rpc",
        ),
        pytest.param(
            False,
            True,
            "not run",
            "none",
            BackendErrorReason.DECODING,
            False,
            False,
            id="mutation-decoded-badly-after-dispatch",
        ),
        pytest.param(
            False,
            True,
            "not run",
            "none",
            BackendErrorReason.NOTEBOOK_NOT_FOUND,
            False,
            False,
            id="mutation-not-found",
        ),
        pytest.param(
            False,
            False,
            "not run",
            "none",
            BackendErrorReason.SERVER,
            False,
            False,
            id="server-reason-without-dispatch-is-exact",
        ),
    ],
)
def test_may_have_committed_truth_table(
    prior_write: bool,
    dispatched: bool,
    probe_or_readback: str,
    expiry: str,
    reason: BackendErrorReason,
    expected_outcome_unknown: bool,
    expected_may_have_committed: bool,
) -> None:
    del prior_write, expiry
    error = scripted_error(
        reason,
        operation=Operation.NOTEBOOK_CREATE,
        dispatched=dispatched,
        outcome_unknown=expected_outcome_unknown and probe_or_readback != "could not answer",
    )
    if probe_or_readback == "could not answer":
        error = mark_backend_outcome_unknown(error)

    assert error.outcome_unknown is expected_outcome_unknown
    assert may_have_committed(error) is expected_may_have_committed


def test_commit_uncertain_reason_set_is_closed() -> None:
    assert (
        frozenset(
            {
                BackendErrorReason.SERVER,
                BackendErrorReason.NETWORK,
                BackendErrorReason.RATE_LIMIT,
                BackendErrorReason.TIMEOUT,
            }
        )
        == COMMIT_UNCERTAIN_REASONS
    )
    for reason in BackendErrorReason:
        error = BackendError(
            "x", operation=Operation.NOTEBOOK_CREATE, reason=reason, dispatched=True
        )
        assert may_have_committed(error) is (reason in COMMIT_UNCERTAIN_REASONS)


def test_dispatched_defaults_false_and_deadline_error_forwards_it() -> None:
    assert BackendError("x").dispatched is False
    assert BackendDeadlineExceededError(Operation.NOTEBOOK_GET).dispatched is False
    assert BackendDeadlineExceededError(Operation.NOTEBOOK_GET, dispatched=True).dispatched is True


# ---------------------------------------------------------------------------
# mark_backend_outcome_unknown preserves subclass, marker, and message
# ---------------------------------------------------------------------------


def _every_subclass_instance() -> list[BackendError]:
    diagnostics = MappingProxyType({"method_id": "m1"})
    return [
        BackendError(
            "plain",
            operation=Operation.NOTEBOOK_CREATE,
            diagnostics=diagnostics,
            reason=BackendErrorReason.SERVER,
            dispatched=True,
        ),
        BackendContractError("contract", operation=Operation.NOTEBOOK_CREATE, dispatched=True),
        UnsupportedOperationError(Operation.NOTEBOOK_CREATE, BackendKind.WEB),
        BackendDeadlineExceededError(
            Operation.NOTEBOOK_CREATE, diagnostics=diagnostics, dispatched=True
        ),
    ]


@pytest.mark.parametrize("error", _every_subclass_instance(), ids=lambda e: type(e).__name__)
def test_mark_backend_outcome_unknown_preserves_identity_fields(error: BackendError) -> None:
    marked = mark_backend_outcome_unknown(error)

    assert type(marked) is type(error)
    assert marked.outcome_unknown is True
    assert marked.dispatched is error.dispatched
    assert marked.reason is error.reason
    assert marked.operation is error.operation
    assert marked.diagnostics == error.diagnostics
    assert marked.message == error.message
    assert str(marked) == str(error)
    assert marked.args == error.args
    if isinstance(error, UnsupportedOperationError):
        assert isinstance(marked, UnsupportedOperationError)
        assert marked.backend_kind is error.backend_kind
    # Idempotent: an already-unknown error is returned by identity.
    assert mark_backend_outcome_unknown(marked) is marked


# ---------------------------------------------------------------------------
# rebind_operation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error", _every_subclass_instance(), ids=lambda e: type(e).__name__)
def test_rebind_operation_preserves_everything_but_the_operation(error: BackendError) -> None:
    rebound = rebind_operation(error, Operation.LABEL_UPDATE)

    assert type(rebound) is type(error)
    assert rebound.operation is Operation.LABEL_UPDATE
    assert rebound.dispatched is error.dispatched
    assert rebound.outcome_unknown is error.outcome_unknown
    assert rebound.reason is error.reason
    assert rebound.diagnostics is not None
    assert rebound.diagnostics["leaf_operation"] is Operation.NOTEBOOK_CREATE
    for key, value in (error.diagnostics or {}).items():
        assert rebound.diagnostics[key] == value
    assert rebound.args == (rebound.message,)
    assert str(rebound) == rebound.message


def test_rebind_operation_rebuilds_operation_derived_messages() -> None:
    deadline = rebind_operation(
        BackendDeadlineExceededError(Operation.LABEL_GET), Operation.LABEL_UPDATE
    )
    assert deadline.message == BackendDeadlineExceededError(Operation.LABEL_UPDATE).message
    assert deadline.message == "label.update exceeded its deadline"

    unsupported = rebind_operation(
        UnsupportedOperationError(Operation.LABEL_GET, BackendKind.WEB), Operation.LABEL_UPDATE
    )
    assert unsupported.message == "web backend does not support label.update"

    plain = rebind_operation(
        BackendError("kept verbatim", operation=Operation.LABEL_GET), Operation.LABEL_UPDATE
    )
    assert plain.message == "kept verbatim"


def test_rebind_operation_keeps_the_innermost_leaf_and_is_identity_when_bound() -> None:
    leaf = BackendError("x", operation=Operation.LABEL_GET, reason=BackendErrorReason.RPC)
    once = rebind_operation(leaf, Operation.LABEL_UPDATE)
    twice = rebind_operation(once, Operation.COLLECTION_UPDATE)

    assert twice.diagnostics is not None
    assert twice.diagnostics["leaf_operation"] is Operation.LABEL_GET
    assert rebind_operation(twice, Operation.COLLECTION_UPDATE) is twice

    unattributed = rebind_operation(BackendError("x"), Operation.LABEL_UPDATE)
    assert unattributed.operation is Operation.LABEL_UPDATE
    assert "leaf_operation" not in (unattributed.diagnostics or {})


def test_rebound_error_can_be_marked_unknown_and_keeps_both_markers() -> None:
    leaf = BackendDeadlineExceededError(Operation.LABEL_GET, dispatched=True)
    workflow = mark_backend_outcome_unknown(rebind_operation(leaf, Operation.LABEL_UPDATE))

    assert isinstance(workflow, BackendDeadlineExceededError)
    assert workflow.operation is Operation.LABEL_UPDATE
    assert workflow.outcome_unknown is True
    assert workflow.dispatched is True
    assert may_have_committed(workflow) is True


# ---------------------------------------------------------------------------
# require_leaves
# ---------------------------------------------------------------------------


def test_require_leaves_passes_when_every_leaf_is_supported() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, NotebookListResult(notebooks=()))
    backend.set_result(
        NOTEBOOK_GET_DEF,
        NotebookGetResult(notebook=NotebookRecord(id="nb-1", title="t")),
    )

    require_leaves(backend, Operation.NOTEBOOK_LIST, Operation.NOTEBOOK_GET)
    assert backend.invocations == []


def test_require_leaves_raises_for_the_first_unsupported_leaf_before_any_side_effect() -> None:
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_LIST_DEF, NotebookListResult(notebooks=()))

    with pytest.raises(UnsupportedOperationError) as caught:
        require_leaves(
            backend,
            Operation.NOTEBOOK_LIST,
            Operation.NOTEBOOK_GET,
            Operation.NOTEBOOK_CREATE,
        )

    assert caught.value.operation is Operation.NOTEBOOK_GET
    assert caught.value.backend_kind is BackendKind.WEB
    assert backend.invocations == []


def test_scripted_backend_records_shape_expected_by_services() -> None:
    """The fixture's scripted values are ordinary port records."""
    backend = RecordingBackend()
    backend.set_sequence(
        NOTEBOOK_GET_DEF,
        [scripted_error(BackendErrorReason.SERVER, dispatched=True)],
    )
    assert backend.capabilities.supports(Operation.NOTEBOOK_GET)
    assert not backend.capabilities.supports(Operation.NOTEBOOK_LIST)
