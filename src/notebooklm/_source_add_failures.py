"""How a hoisted source-add workflow reports one failure across the port.

Two functions the source-add family's service-owned workflows share. They are
here rather than in ``_source_service`` so the batch workflow — which lives in
its own module for the module-size ratchet — reaches them without importing the
service that delegates to it.

Nothing here builds a public exception. A workflow above the port reports
bounded neutral *evidence*, and ``_backend_compat`` replays an equal public
exception at the facade from that evidence alone.
"""

from __future__ import annotations

from types import MappingProxyType

from ._backend import BackendContractError, BackendError, BackendErrorReason
from ._operations import Operation
from ._records import SourceAddFailureRecord


def _source_add_failure(
    operation: Operation,
    record: SourceAddFailureRecord,
    *,
    outcome_unknown: bool = False,
    dispatched: bool = False,
) -> BackendError:
    """Report one source-add failure as bounded neutral evidence.

    ``_backend_compat`` replays an *equal* public exception at the facade from
    ``record`` alone, so a transport-neutral workflow never has to name — or
    construct — a public exception type.
    """
    return BackendError(
        message=record.message,
        operation=operation,
        outcome_unknown=outcome_unknown,
        diagnostics=MappingProxyType({"source_add_failure": record}),
        reason=BackendErrorReason.SOURCE_ADD,
        dispatched=dispatched,
    )


def _leaf_failure_record(error: BackendError) -> SourceAddFailureRecord | None:
    """Return the leaf's captured public graph, if the backend captured one.

    Capturing it is a *web* convention, not a port requirement: another adapter
    may report a closed reason and nothing else, and the compatibility projector
    reconstructs a public exception from the reason alone in that case. ``None``
    therefore means "project by reason", not "malformed". A value of the wrong
    type is malformed, and fails closed.
    """
    record = (error.diagnostics or {}).get("public_error_failure")
    if record is None:
        return None
    if not isinstance(record, SourceAddFailureRecord):
        raise BackendContractError(
            "source registration failure has invalid public-error evidence",
            operation=error.operation,
        ) from error
    return record


__all__ = ["_leaf_failure_record", "_source_add_failure"]
