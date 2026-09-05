"""Commit evidence and replay-decision contract tests."""

from notebooklm._idempotency import (
    ReplayGrant,
    mark_commit_state,
    mark_unconfirmed,
    replay_allowed,
)
from notebooklm.exceptions import RateLimitError
from notebooklm.outcomes import CommitState


def test_commit_state_has_stable_public_values() -> None:
    assert [state.value for state in CommitState] == [
        "not_sent",
        "rejected",
        "unknown",
        "confirmed",
    ]


def test_absent_evidence_never_authorizes_refusal_retry() -> None:
    assert not replay_allowed(
        RateLimitError("unclassified"),
        grant=ReplayGrant.REFUSAL_RETRY_AUTHORIZED,
        disabled=False,
        remaining=1,
    )


def test_refusal_retry_requires_positive_rejected_or_not_sent_evidence() -> None:
    for state in (CommitState.REJECTED, CommitState.NOT_SENT):
        error = mark_commit_state(RateLimitError("safe"), state)
        assert replay_allowed(
            error,
            grant=ReplayGrant.REFUSAL_RETRY_AUTHORIZED,
            disabled=False,
            remaining=1,
        )


def test_unknown_evidence_is_not_regressed_by_a_later_not_sent_marker() -> None:
    error = mark_unconfirmed(RateLimitError("lost"))
    mark_commit_state(error, CommitState.NOT_SENT)
    assert error.commit_state is CommitState.UNKNOWN  # type: ignore[attr-defined]
    assert error.unconfirmed is True  # type: ignore[attr-defined]


def test_replay_safe_still_honors_disable_and_budget() -> None:
    error = RateLimitError("read")
    assert replay_allowed(
        error,
        grant=ReplayGrant.REPLAY_SAFE,
        disabled=False,
        remaining=None,
    )
    assert not replay_allowed(
        error,
        grant=ReplayGrant.REPLAY_SAFE,
        disabled=True,
        remaining=None,
    )
    assert not replay_allowed(
        error,
        grant=ReplayGrant.REPLAY_SAFE,
        disabled=False,
        remaining=0,
    )
