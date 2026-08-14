"""Private true-batch URL source creation service.

The public single-item :meth:`SourcesAPI.add_url` path deliberately keeps its
probe-then-create retry contract.  This module is only for the already
batch-shaped MCP and REST endpoints: one ``ADD_SOURCE`` request carries every
validated URL, and omitted response rows are projected back into positional
per-item failures.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any

from .._idempotency import mark_unconfirmed
from .._row_adapters.sources import unwrap_add_source_rows
from .._runtime.contracts import RpcCaller
from ..exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    SourceAddError,
)
from ..rpc import RPCError, RPCMethod
from ..rpc.types import GrpcStatusCode, SourceStatus, normalize_rpc_code
from ..types import Source
from .upload_payloads import build_template_block

ListSources = Callable[..., Awaitable[list[Source]]]


@dataclass(frozen=True)
class SourceUrlBatchItem:
    """One positional outcome from a true-batch URL add."""

    url: str
    source: Source | None = None
    error: SourceAddError | None = None

    def __post_init__(self) -> None:
        if (self.source is None) == (self.error is None):
            raise ValueError("exactly one of source or error must be set")


def _url_spec(url: str, *, youtube: bool) -> list[Any]:
    """Build one repeated web-page or YouTube ``UserContent`` entry."""
    if youtube:
        return [None, None, None, None, None, None, None, [url], None, None, 1]
    return [None, None, [url], None, None, None, None, None, None, None, 1]


def _unresolved_batch_error(urls: Sequence[str], message: str, cause: Exception) -> SourceAddError:
    preview = ", ".join(repr(url) for url in urls[:3])
    if len(urls) > 3:
        preview += f", … ({len(urls)} total)"
    return mark_unconfirmed(
        SourceAddError(
            preview,
            cause=cause,
            message=(
                "UNRESOLVED — do not blindly retry; check the notebook source list and "
                f"reconcile these URLs first: {preview}. {message}"
            ),
        )
    )


class SourceBatchAddService:
    """Issue and reconcile one multi-entry ``ADD_SOURCE`` request."""

    async def add_urls(
        self,
        notebook_id: str,
        urls: Sequence[str],
        *,
        rpc: RpcCaller,
        list_sources: ListSources,
        extract_youtube_video_id: Callable[[str], str | None],
        logger: logging.Logger,
    ) -> list[SourceUrlBatchItem]:
        """Add URL entries once and return positional typed outcomes.

        The batch write is never automatically replayed.  A transport failure
        can occur after an arbitrary subset committed, so retrying the entire
        request would duplicate that subset.  A protocol-level rejection, by
        contrast, is the backend's documented all-failed result; it is converted
        into per-item ``SourceAddError`` values after the same ERROR-row
        reconciliation used for partial success.
        """
        if not urls:
            return []

        params = [
            [_url_spec(url, youtube=extract_youtube_video_id(url) is not None) for url in urls],
            notebook_id,
            build_template_block(),
        ]
        rpc_error: RPCError | None = None
        try:
            payload = await rpc.rpc_call(
                RPCMethod.ADD_SOURCE,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=False,
                disable_internal_retries=True,
                # Reuse the established URL idempotency-registry variant while
                # explicitly disabling its internal replay above.  The outer
                # single-item probe loop is intentionally not used here.
                operation_variant="url",
            )
        except AuthError:
            # Authentication rejection cannot have committed the write and
            # keeps its normal adapter contract (401 / re-auth guidance).
            raise
        except (RateLimitError, ServerError, NetworkError) as exc:
            # Preserve the typed transport exception (ADR-0019) while making
            # the write-uncertainty dominate retry classification (#2220).
            original = str(exc)
            mark_unconfirmed(exc)
            exc.args = (
                "UNRESOLVED — do not blindly retry; check the notebook source list and "
                "reconcile the batch URLs first. The batch transport failed and an "
                f"unknown subset may have committed; no automatic retry was attempted. {original}",
            )
            raise
        except RPCError as exc:
            # Live-characterized all-failed URL batches use the same explicit
            # FAILED_PRECONDITION status as a single rejected URL.  A generic
            # decoder/protocol failure is not that evidence: the write may have
            # committed, so fail closed instead of pretending every item failed.
            if normalize_rpc_code(exc.rpc_code) != GrpcStatusCode.FAILED_PRECONDITION.value:
                original = str(exc)
                mark_unconfirmed(exc)
                exc.args = (
                    "UNRESOLVED — do not blindly retry; check the notebook source list and "
                    "reconcile the batch URLs first. The batch RPC failed without the "
                    "documented all-rejected status, so its committed subset is unknown; "
                    f"no automatic retry was attempted. {original}",
                )
                raise
            # Preserve the existing per-item adapter contract instead of
            # turning an all-bad input batch into a top-level failure.
            rpc_error = exc
            payload = []

        try:
            rows = [] if rpc_error is not None else unwrap_add_source_rows(payload)
            sources = [
                Source.from_api_response(row, method_id=RPCMethod.ADD_SOURCE.value) for row in rows
            ]
        except Exception as exc:  # noqa: BLE001 - strict decode boundary; fail closed
            raise _unresolved_batch_error(
                urls,
                "The batch response could not be decoded, so its committed subset is unknown; "
                "no automatic retry was attempted.",
                exc,
            ) from exc

        # ``Source.from_api_response`` is deliberately tolerant on listing
        # paths, but a mutating response cannot use that degradation policy:
        # outside the explicit FAILED_PRECONDITION branch, an empty/malformed
        # payload does not prove that every write was rejected. Treat it as an
        # unresolved write, and never surface an id-less Source as success.
        if rpc_error is None and any(not source.id for source in sources):
            error = RPCError(
                "ADD_SOURCE returned no decodable source rows with non-empty ids",
                method_id=RPCMethod.ADD_SOURCE.value,
            )
            raise _unresolved_batch_error(
                urls,
                "The batch response did not identify its successful writes, so the "
                "committed subset is unknown; no automatic retry was attempted.",
                error,
            )

        requested_counts = Counter(urls)
        sources_by_url: dict[str, deque[Source]] = defaultdict(deque)
        sources_without_url: list[Source] = []
        for source in sources:
            if source.url is None:
                sources_without_url.append(source)
                continue
            if source.url not in requested_counts:
                error = RPCError(
                    f"ADD_SOURCE returned an unrequested URL {source.url!r}",
                    method_id=RPCMethod.ADD_SOURCE.value,
                )
                raise _unresolved_batch_error(
                    urls,
                    "The batch response contained an unrequested source, so positional "
                    "outcomes cannot be trusted; no automatic retry was attempted.",
                    error,
                )
            sources_by_url[source.url].append(source)

        # A sparse response must identify rows by URL: response order alone
        # cannot reveal which request positions were silently omitted.  A full
        # response is safe to zip positionally even when the legacy short row
        # carries only an id.
        if sources_without_url:
            if len(sources) != len(urls) or sources_by_url:
                error = RPCError(
                    "ADD_SOURCE returned sparse source rows without URL metadata",
                    method_id=RPCMethod.ADD_SOURCE.value,
                )
                raise _unresolved_batch_error(
                    urls,
                    "The partial batch response omitted URL metadata needed to match rows "
                    "back to inputs; no automatic retry was attempted.",
                    error,
                )
            return [
                SourceUrlBatchItem(url=url, source=source)
                for url, source in zip(urls, sources, strict=True)
            ]

        # Exact duplicate URLs are representable when all copies share one
        # outcome.  A partial duplicate group is not attributable per position:
        # the response has no request index or idempotency key, so fail closed.
        for url, count in requested_counts.items():
            success_count = len(sources_by_url[url])
            if success_count > count:
                error = RPCError(
                    f"ADD_SOURCE returned {success_count} rows for {count} request(s) of {url!r}",
                    method_id=RPCMethod.ADD_SOURCE.value,
                )
                raise _unresolved_batch_error(
                    urls,
                    "The batch response contained more sources than requested, so positional "
                    "outcomes cannot be trusted; no automatic retry was attempted.",
                    error,
                )
            if count > 1 and 0 < success_count < count:
                error = RPCError(
                    f"ADD_SOURCE partially admitted duplicate URL {url!r}",
                    method_id=RPCMethod.ADD_SOURCE.value,
                )
                raise _unresolved_batch_error(
                    [url] * count,
                    "The backend partially admitted identical URLs without returning request "
                    "positions, so the successful copies are ambiguous; no automatic retry "
                    "was attempted.",
                    error,
                )

        missing_urls: list[str] = []
        for url in urls:
            if not sources_by_url[url]:
                missing_urls.append(url)

        error_rows_by_url: dict[str, list[Source]] = defaultdict(list)
        if missing_urls:
            try:
                error_rows = await list_sources(notebook_id, statuses={SourceStatus.ERROR})
            except Exception:
                # The success response already proves which entries were
                # admitted (or rpc_error proves none were).  Reconciliation only
                # enriches failure diagnostics with ghost ids, so its own failure
                # must not discard otherwise-known positional outcomes.
                logger.warning(
                    "add_urls_batch: failed to list ERROR rows after ADD_SOURCE; "
                    "returning per-item failures without ghost ids",
                    exc_info=True,
                )
            else:
                for row in error_rows:
                    row_url = row.url
                    if row_url is not None and row_url in requested_counts:
                        error_rows_by_url[row_url].append(row)

        outcomes: list[SourceUrlBatchItem] = []
        for url in urls:
            if sources_by_url[url]:
                outcomes.append(SourceUrlBatchItem(url=url, source=sources_by_url[url].popleft()))
                continue
            ghosts = error_rows_by_url[url]
            ghost_text = ""
            if ghosts:
                ids = ", ".join(source.id for source in ghosts[:3])
                suffix = "" if len(ghosts) <= 3 else f", … ({len(ghosts)} rows)"
                ghost_text = f" Matching ERROR source row(s): {ids}{suffix}."
            outcomes.append(
                SourceUrlBatchItem(
                    url=url,
                    error=SourceAddError(
                        url,
                        cause=rpc_error,
                        message=(
                            f"Failed to add URL source {url!r}: the backend omitted it from "
                            f"the batch success response.{ghost_text}"
                        ),
                    ),
                )
            )
        return outcomes


__all__ = ["SourceBatchAddService", "SourceUrlBatchItem"]
