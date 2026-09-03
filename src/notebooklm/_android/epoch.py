"""Task-local Android workflow epoch propagation."""

from __future__ import annotations

from contextvars import ContextVar, Token

_WorkflowEpoch = tuple[object, int]
_workflow_epoch: ContextVar[_WorkflowEpoch | None] = ContextVar(
    "notebooklm_android_workflow_epoch",
    default=None,
)


def _session_identity(session: object) -> object:
    """Return the unique identity token owned by an Android session."""

    return getattr(session, "_workflow_session_id", session)


def bind_workflow_epoch(session: object, epoch: int) -> Token[_WorkflowEpoch | None]:
    """Bind ``epoch`` to ``session`` in the current task context."""

    return _workflow_epoch.set((_session_identity(session), epoch))


def reset_workflow_epoch(token: Token[_WorkflowEpoch | None]) -> None:
    """Restore the context that preceded one workflow scope."""

    _workflow_epoch.reset(token)


def workflow_epoch_for(session: object) -> int | None:
    """Return the task-local epoch only when it belongs to ``session``."""

    tagged = _workflow_epoch.get()
    if tagged is None:
        return None
    session_id, epoch = tagged
    if session_id is not _session_identity(session):
        return None
    return epoch


__all__ = ["bind_workflow_epoch", "reset_workflow_epoch", "workflow_epoch_for"]
