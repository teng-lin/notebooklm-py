"""Closed compatibility projection for neutral semantic-backend failures."""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest

from notebooklm._semantic.backend import (
    BackendContractError,
    BackendError,
    BackendErrorReason,
)
from notebooklm._semantic.compat import (
    project_backend_call,
    project_backend_error,
    project_local_not_found,
)
from notebooklm._semantic.operations import Operation
from notebooklm._semantic.records import SourceAddFailureKind, SourceAddFailureRecord
from notebooklm._web.errors import translate_web_error
from notebooklm.exceptions import (
    ArtifactFeatureUnavailableError,
    ArtifactNotFoundError,
    AuthError,
    ClientError,
    CollectionNotFoundError,
    DecodingError,
    LabelNotFoundError,
    NetworkError,
    NotebookLimitError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    RPCResponseTooLargeError,
    RPCTimeoutError,
    ServerError,
    UnknownRPCMethodError,
)
from notebooklm.rpc import RPCMethod


def test_project_backend_error_has_one_explicit_case_per_closed_reason() -> None:
    """Every neutral reason has one mapping; copied dead branches fail closed."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(project_backend_error)))
    cases = [
        comparator.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Name)
        and node.left.id == "reason"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Is)
        and len(node.comparators) == 1
        and isinstance((comparator := node.comparators[0]), ast.Attribute)
        and isinstance(comparator.value, ast.Name)
        and comparator.value.id == "BackendErrorReason"
    ]

    assert Counter(cases) == Counter({reason.name: 1 for reason in BackendErrorReason})


@pytest.mark.parametrize(
    ("operation", "resource_id", "expected_type", "id_attribute", "method"),
    [
        (
            Operation.LABEL_GET,
            "label-1",
            LabelNotFoundError,
            "label_id",
            RPCMethod.LIST_LABELS,
        ),
        (
            Operation.COLLECTION_GET,
            "collection-1",
            CollectionNotFoundError,
            "collection_id",
            RPCMethod.LIST_LABELS,
        ),
        (
            Operation.ARTIFACT_GET,
            "artifact-1",
            ArtifactNotFoundError,
            "artifact_id",
            RPCMethod.LIST_ARTIFACTS,
        ),
    ],
)
def test_local_not_found_projection_owns_legacy_native_diagnostics(
    operation: Operation,
    resource_id: str,
    expected_type: type[Exception],
    id_attribute: str,
    method: RPCMethod,
) -> None:
    projected = project_local_not_found(operation, resource_id)

    assert type(projected) is expected_type
    assert getattr(projected, id_attribute) == resource_id
    assert projected.method_id == method.value


def test_local_not_found_projection_rejects_unreviewed_operations() -> None:
    with pytest.raises(BackendContractError, match="no local not-found compatibility contract"):
        project_local_not_found(Operation.SETTINGS_GET, "missing")


_COMPATIBILITY_FACADES = (
    "_artifact/generation_workflow.py",
    "_artifacts.py",
    "_chat/api.py",
    "_collections.py",
    "_labels.py",
    "_mind_maps_api.py",
    "_notebooks.py",
    "_notes.py",
    "_semantic/services/research.py",
    "_settings.py",
    "_sharing.py",
    "_sharing_manager.py",
    "_sources.py",
)


def _is_projector_call(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "project_backend_error"
    )


def test_compatibility_facades_raise_projections_outside_backend_handlers() -> None:
    """Private BackendError frames must not rewrite the projected exception graph."""
    package = Path(__file__).parents[2] / "src" / "notebooklm"
    offenders: list[str] = []
    for relative_path in _COMPATIBILITY_FACADES:
        path = package / relative_path
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        projected_names = {
            target.id
            for assignment in ast.walk(tree)
            if isinstance(assignment, ast.Assign) and _is_projector_call(assignment.value)
            for target in assignment.targets
            if isinstance(target, ast.Name)
        }
        for handler in (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.ExceptHandler)
            and isinstance(node.type, ast.Name)
            and node.type.id == "BackendError"
        ):
            for raised in (node for node in ast.walk(handler) if isinstance(node, ast.Raise)):
                if _is_projector_call(raised.exc) or (
                    isinstance(raised.exc, ast.Name) and raised.exc.id in projected_names
                ):
                    offenders.append(f"{relative_path}:{raised.lineno}:inside handler")
        for raised in (node for node in ast.walk(tree) if isinstance(node, ast.Raise)):
            if not (isinstance(raised.cause, ast.Constant) and raised.cause.value is None):
                continue
            if _is_projector_call(raised.exc) or (
                isinstance(raised.exc, ast.Name) and raised.exc.id in projected_names
            ):
                offenders.append(f"{relative_path}:{raised.lineno}:from None")

    assert offenders == []


def _backend_error_with_public_graph(public_failure: SourceAddFailureRecord) -> BackendError:
    return BackendError(
        "rpc failure",
        operation=Operation.NOTEBOOK_GET,
        reason=BackendErrorReason.RPC,
        diagnostics={
            "method_id": "rpc",
            "found_ids": [],
            "public_error_failure": public_failure,
        },
    )


async def _raise_backend_error(error: BackendError) -> None:
    raise error


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_cause", [True, False])
async def test_project_backend_call_preserves_projector_authored_graph(
    explicit_cause: bool,
) -> None:
    leaf = SourceAddFailureRecord(
        SourceAddFailureKind.BUILTIN_VALUE,
        "graph leaf",
        args=("graph leaf",),
    )
    public_failure = SourceAddFailureRecord(
        SourceAddFailureKind.RPC,
        "rpc failure",
        method_id="rpc",
        cause=leaf if explicit_cause else None,
        context=None if explicit_cause else leaf,
        context_is_cause=explicit_cause,
        explicit_cause=explicit_cause,
        suppress_context=explicit_cause,
    )

    with pytest.raises(RPCError) as captured:
        await project_backend_call(
            _raise_backend_error(_backend_error_with_public_graph(public_failure))
        )

    projected = captured.value
    if explicit_cause:
        assert isinstance(projected.__cause__, ValueError)
        assert projected.__context__ is projected.__cause__
        assert projected.__suppress_context__ is True
    else:
        assert projected.__cause__ is None
        assert isinstance(projected.__context__, ValueError)
        assert projected.__suppress_context__ is False
    assert not isinstance(projected.__cause__, BackendError)
    assert not isinstance(projected.__context__, BackendError)


def _round_trip(error: RPCError | NetworkError) -> Exception:
    neutral = translate_web_error(Operation.NOTEBOOK_GET, error)
    return project_backend_error(neutral)


@pytest.mark.parametrize(
    ("original", "expected_type", "assert_diagnostics"),
    [
        (
            AuthError("authenticate", method_id="rpc", rpc_code=16),
            AuthError,
            lambda error: (
                error.method_id == "rpc" and error.rpc_code == 16 and error.recoverable is True
            ),
        ),
        (
            ClientError("client", status_code=404, method_id="rpc", rpc_code=5),
            ClientError,
            lambda error: error.status_code == 404 and error.rpc_code == 5,
        ),
        (
            DecodingError("decode", method_id="rpc", found_ids=["other"]),
            DecodingError,
            lambda error: error.method_id == "rpc" and error.found_ids == ["other"],
        ),
        (
            NetworkError("network", method_id="rpc"),
            NetworkError,
            lambda error: error.method_id == "rpc" and error.original_error is None,
        ),
        (
            RateLimitError("rate", retry_after=7, method_id="rpc"),
            RateLimitError,
            lambda error: error.retry_after == 7 and error.method_id == "rpc",
        ),
        (
            RPCResponseTooLargeError(
                "large",
                limit_bytes=10,
                bytes_read=11,
                method_id="rpc",
            ),
            RPCResponseTooLargeError,
            lambda error: (
                error.limit_bytes == 10 and error.bytes_read == 11 and error.method_id == "rpc"
            ),
        ),
        (
            RPCError(
                "rpc",
                method_id="rpc",
                raw_response="scrubbed",
                rpc_code=13,
                found_ids=["other"],
            ),
            RPCError,
            lambda error: (
                error.method_id == "rpc"
                and error.raw_response == "scrubbed"
                and error.rpc_code == 13
                and error.found_ids == ["other"]
            ),
        ),
        (
            ServerError("server", status_code=503, method_id="rpc"),
            ServerError,
            lambda error: error.status_code == 503 and error.method_id == "rpc",
        ),
        (
            RPCTimeoutError("timeout", timeout_seconds=3.0, method_id="rpc"),
            RPCTimeoutError,
            lambda error: (
                error.timeout_seconds == 3.0
                and error.method_id == "rpc"
                and error.original_error is None
            ),
        ),
        (
            UnknownRPCMethodError(
                "unknown shape",
                method_id=123,
                path=(0, 2),
                source="decoder",
                found_ids=[456, "other"],
                raw_response={"safe": "preview"},
                data_at_failure="safe data",
                rpc_code=13,
            ),
            UnknownRPCMethodError,
            lambda error: (
                error.method_id == 123
                and error.path == (0, 2)
                and error.source == "decoder"
                and error.found_ids == [456, "other"]
                and error.raw_response == {"safe": "preview"}
                and error.data_at_failure == "safe data"
                and error.rpc_code == 13
            ),
        ),
    ],
)
def test_closed_backend_reasons_reconstruct_public_exception_contract(
    original: RPCError | NetworkError,
    expected_type: type[Exception],
    assert_diagnostics: Callable[[object], bool],
) -> None:
    if isinstance(original, AuthError):
        original.recoverable = True

    projected = _round_trip(original)

    assert type(projected) is expected_type
    assert projected.args == original.args
    assert str(projected) == str(original)
    assert assert_diagnostics(projected)
    assert projected is not original


def test_unknown_rpc_base_message_is_not_rendered_with_diagnostics_twice() -> None:
    original = UnknownRPCMethodError(
        "unknown shape",
        method_id="rpc",
        path=(0, 2),
        source="decoder",
    )

    neutral = translate_web_error(Operation.NOTEBOOK_GET, original)
    projected = project_backend_error(neutral)

    assert neutral.message == "unknown shape"
    assert str(projected) == str(original)
    assert str(projected).count("method_id='rpc'") == 1
    assert str(projected).count("path=(0, 2)") == 1


def test_notebook_mutation_specific_errors_reconstruct_from_neutral_evidence() -> None:
    not_found = project_backend_error(
        BackendError(
            "Notebook not found: missing",
            operation=Operation.NOTEBOOK_UPDATE,
            reason=BackendErrorReason.NOTEBOOK_NOT_FOUND,
            diagnostics={"notebook_id": "missing", "method_id": "rpc-get"},
        )
    )
    assert isinstance(not_found, NotebookNotFoundError)
    assert not_found.notebook_id == "missing"
    assert not_found.method_id == "rpc-get"

    limit = project_backend_error(
        BackendError(
            "notebook limit reached",
            operation=Operation.NOTEBOOK_CREATE,
            reason=BackendErrorReason.NOTEBOOK_LIMIT,
            diagnostics={
                "current_count": 499,
                "limit": 500,
                "original_message": "invalid argument",
                "original_reason": BackendErrorReason.RPC.value,
                "original_diagnostics": {
                    "method_id": "rpc-create",
                    "rpc_code": 3,
                    "found_ids": [],
                },
            },
        )
    )
    assert isinstance(limit, NotebookLimitError)
    assert (limit.current_count, limit.limit) == (499, 500)
    assert isinstance(limit.original_error, RPCError)
    assert limit.original_error.method_id == "rpc-create"
    assert limit.original_error.rpc_code == 3
    assert limit.__cause__ is limit.original_error
    assert limit.__context__ is limit.original_error
    assert limit.__suppress_context__ is True


def test_audio_feature_unavailable_reconstructs_exact_public_evidence() -> None:
    projected = project_backend_error(
        BackendError(
            "Audio generation is unavailable",
            operation=Operation.ARTIFACT_GENERATE_AUDIO,
            reason=BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE,
            diagnostics={
                "artifact_type": "audio",
                "method_id": "R7cb6c",
                "raw_response": None,
            },
        )
    )

    assert isinstance(projected, ArtifactFeatureUnavailableError)
    assert projected.artifact_type == "audio"
    assert projected.method_id == "R7cb6c"
    assert projected.raw_response is None


def test_audio_feature_unavailable_requires_closed_artifact_type_evidence() -> None:
    error = BackendError(
        "Audio generation is unavailable",
        operation=Operation.ARTIFACT_GENERATE_AUDIO,
        reason=BackendErrorReason.ARTIFACT_FEATURE_UNAVAILABLE,
        diagnostics={"method_id": "R7cb6c"},
    )

    with pytest.raises(BackendContractError, match="lacks artifact_type"):
        project_backend_error(error)


def test_unknown_mutation_outcome_marker_survives_public_reconstruction() -> None:
    projected = project_backend_error(
        BackendError(
            "network",
            operation=Operation.NOTEBOOK_CREATE,
            reason=BackendErrorReason.NETWORK,
            diagnostics={},
            outcome_unknown=True,
        )
    )

    assert isinstance(projected, NetworkError)
    assert getattr(projected, "unconfirmed", False) is True


def test_url_source_failure_uses_closed_reason_and_generic_projector() -> None:
    projected = project_backend_error(
        BackendError(
            "response lost",
            operation=Operation.SOURCE_ADD_URL,
            reason=BackendErrorReason.SOURCE_ADD,
            diagnostics={
                "source_add_failure": SourceAddFailureRecord(
                    SourceAddFailureKind.RPC,
                    "response lost",
                    method_id="add-source",
                    rpc_code=14,
                )
            },
            outcome_unknown=True,
        )
    )

    assert type(projected) is RPCError
    assert projected.method_id == "add-source"
    assert projected.rpc_code == 14
    assert getattr(projected, "unconfirmed", False) is True


@pytest.mark.parametrize(
    "mutation",
    [
        BackendError("no reason", operation=Operation.NOTEBOOK_GET, diagnostics={}),
        BackendError(
            "no evidence",
            operation=Operation.NOTEBOOK_GET,
            reason=BackendErrorReason.RPC,
        ),
        BackendError(
            "bad evidence",
            operation=Operation.NOTEBOOK_GET,
            reason=BackendErrorReason.CLIENT,
            diagnostics={"status_code": "404"},
        ),
    ],
)
def test_incomplete_or_invalid_compatibility_evidence_fails_closed(
    mutation: BackendError,
) -> None:
    with pytest.raises(BackendContractError):
        project_backend_error(mutation)
