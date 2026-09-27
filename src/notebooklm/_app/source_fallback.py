"""Opt-in recovery of a conclusively failed URL import as static text."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from ..exceptions import ClientError, RPCError, SourceAddError, ValidationError
from ..outcomes import CommitState
from ..types import SourceStatus
from ..urls import is_youtube_url
from .source_add import SourceAddResult
from .source_fetch import fetch_source, public_fetch_url, require_fetch_dependencies

if TYPE_CHECKING:
    from ..client import NotebookLMClient
    from ..types import Source
    from .source_add import SourceAddExecutionPlan

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FallbackProvenance:
    original_url: str
    final_url: str
    fetched_at: str
    method: str = "curl_cffi"
    refreshable: bool = False
    ghost_candidates: tuple[str, ...] = ()
    cleanup: str = "not_requested"
    warning: str = "Imported as static text; URL refresh is unavailable."


@dataclass(frozen=True)
class RecoveredSourceAddResult(SourceAddResult):
    fallback: FallbackProvenance


def validate_fallback(plan: SourceAddExecutionPlan) -> None:
    if plan.cleanup_on_failure and not plan.fallback_fetch:
        raise ValidationError("cleanup_on_failure requires fallback_fetch")
    if not plan.fallback_fetch:
        return
    if plan.plan.detected_type != "url":
        raise ValidationError("Fallback is supported only for single web-page URLs")
    try:
        public_fetch_url(plan.plan.content)
    except ValueError as exc:
        raise ValidationError(str(exc)) from exc
    if is_youtube_url(plan.plan.content):
        raise ValidationError("Fallback is supported only for single web-page URLs")
    require_fetch_dependencies()


async def capture_recovery_baseline(client: NotebookLMClient, notebook_id: str) -> set[str] | None:
    """Capture recovery evidence without making it a prerequisite for importing."""
    try:
        return {source.id for source in await client.sources.list(notebook_id, strict=True)}
    except Exception:
        logger.warning("URL fallback unavailable: reason=baseline_failed")
        return None


def _same_url(left: str | None, right: str | None) -> bool:
    """Match equivalent URL spellings without decoding paths or reordering queries."""
    if left is None or right is None:
        return left is right
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        normalized_left, _, _ = public_fetch_url(left)
        normalized_right, _, _ = public_fetch_url(right)
    except ValueError:
        return False
    # Hex digit case is insignificant in escapes; decoding escapes themselves
    # would merge distinct paths (e.g. %2F versus /) and is deliberately avoided.
    return re.sub(
        r"%[0-9a-fA-F]{2}", lambda match: match.group(0).upper(), normalized_left
    ) == re.sub(r"%[0-9a-fA-F]{2}", lambda match: match.group(0).upper(), normalized_right)


def _failed_url_import(exc: SourceAddError | RPCError) -> bool:
    # A network timeout, decoding fault, auth failure, or arbitrary source error
    # never authorizes another create. Code 9 alone still needs row evidence.
    if isinstance(exc, SourceAddError):
        cause = exc.cause
    else:
        # Native RPC errors qualify only at the correlated URL commit boundary.
        if not exc.source_id or exc.stage != "source commit":
            return False
        cause = exc
    if exc.commit_state in {CommitState.CONFIRMED, CommitState.NOT_SENT}:
        return False
    return (
        isinstance(cause, RPCError)
        and type(cause) in (RPCError, ClientError)
        and cause.rpc_code in (9, "9")
    )


def _connection_failure(source: Source) -> bool:
    return (
        source.status == SourceStatus.ERROR
        and type(source.experimental_failure_code) is int
        and source.experimental_failure_code == 1
    )


async def _recovery_candidate(
    client: NotebookLMClient,
    plan: SourceAddExecutionPlan,
    before: set[str],
    owned_id: str | None,
) -> Source | None:
    try:
        # Strict full-roster read: an ERROR-only baseline misses old PROCESSING
        # rows that fail during this call, and a filtered after-read hides a
        # concurrent successful import of the same URL.
        after = await client.sources.list(plan.notebook_id, strict=True)
    except Exception:
        logger.warning("URL fallback skipped: reason=readback_failed")
        return None  # Retain the original failure and all of its write evidence.
    # Android's failed tentative row may have no URL. Its registration receipt
    # identifies the row; a conflicting URL still fails closed.
    matches = [
        s
        for s in after
        if s.id not in before
        and (
            s.id == owned_id and (s.url is None or _same_url(s.url, plan.plan.content))
            if owned_id is not None
            else _same_url(s.url, plan.plan.content)
        )
    ]
    if len(matches) != 1:
        logger.warning(
            "URL fallback skipped: reason=%s",
            "candidate_missing" if not matches else "candidate_ambiguous",
        )
        return None
    ghost = matches[0]
    if not _connection_failure(ghost):
        logger.warning("URL fallback skipped: reason=nonconnection_diagnostic")
        return None
    return ghost


async def recover_url(
    client: NotebookLMClient,
    plan: SourceAddExecutionPlan,
    original: SourceAddError | RPCError,
    before: set[str],
) -> SourceAddResult | None:
    if not _failed_url_import(original):
        logger.warning("URL fallback skipped: reason=import_not_eligible")
        return None
    owned_id = getattr(original, "source_id", None)
    ghost = await _recovery_candidate(client, plan, before, owned_id)
    if ghost is None:
        return None
    try:
        fetched = await fetch_source(plan.plan.content)
    except Exception as exc:
        # Exceptions can contain URLs, credentials or response bodies. Report
        # only the class and keep the original source error entirely unchanged.
        logger.warning(
            "URL fallback skipped: reason=fetch_failed error_type=%s", type(exc).__name__
        )
        return None

    # Fetching can take long enough for the roster to change. Require the same
    # eligible row immediately before the replacement write. The backend has no
    # conditional create/delete, so a concurrent change after this read remains
    # possible; this read cannot provide transactional isolation.
    current = await _recovery_candidate(client, plan, before, owned_id)
    if current is None:
        return None
    if current.id != ghost.id:
        logger.warning("URL fallback skipped: reason=candidate_changed")
        return None
    ghost = current

    # Only this second mutation's own receipt can describe its outcome. A lost
    # add_text response must escape unchanged; never retry it or delete the stub.
    fetched_at = datetime.now(timezone.utc).isoformat()
    content = (
        f"Static web copy (URL refresh unavailable)\nOriginal URL: {plan.plan.content}\n"
        f"Fetched URL: {fetched.final_url}\nFetched at: {fetched_at}\n\n{fetched.content}"
    )
    replacement = await client.sources.add_text(
        plan.notebook_id,
        plan.plan.title or fetched.title or "Imported web page (static text)",
        content,
    )
    cleanup = "not_requested"
    warning = "Imported as static text; URL refresh is unavailable."
    if plan.cleanup_on_failure:
        # A URL/time diff is diagnostic, not proof of ownership. Only an exact
        # source_id attached by the creating workflow can authorize deletion.
        if owned_id != ghost.id:
            cleanup = "skipped_unattributed"
            warning += " Ghost retained because this operation cannot prove ownership."
        else:
            cleanup = await _cleanup(client, plan.notebook_id, ghost, replacement)
            if cleanup not in {"deleted", "already_absent"}:
                warning += " Replacement retained; ghost cleanup was not confirmed."

    return RecoveredSourceAddResult(
        source=replacement,
        fallback=FallbackProvenance(
            original_url=plan.plan.content,
            final_url=fetched.final_url,
            fetched_at=fetched_at,
            ghost_candidates=(ghost.id,),
            cleanup=cleanup,
            warning=warning,
        ),
    )


async def _cleanup(
    client: NotebookLMClient, notebook_id: str, ghost: Source, replacement: Source
) -> str:
    try:
        ready = await client.sources.wait_until_ready(notebook_id, replacement.id)
        if not ready.is_ready:
            return "replacement_not_ready"
        current = await client.sources.get_or_none(notebook_id, ghost.id)
        if current is None:
            return "already_absent"
        if not _connection_failure(current) or not _same_url(current.url, ghost.url):
            return "skipped_changed"
        # No conditional delete is available; the final read narrows, but cannot
        # eliminate, the race with a concurrent change to this exact source.
        outcomes = await client.sources.delete_many_with_outcomes(notebook_id, [ghost.id])
        if len(outcomes) == 1 and outcomes[0].outcome.commit_state is CommitState.CONFIRMED:
            return "deleted"
        return "unconfirmed"
    except Exception:
        return "unconfirmed"
