"""Private true-batch URL source creation service.

The public single-item :meth:`SourcesAPI.add_url` path deliberately keeps its
probe-then-create retry contract.  This module is only for the already
batch-shaped MCP and REST endpoints: one ``ADD_SOURCE`` request carries every
validated URL, and omitted response rows are projected back into positional
per-item failures.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict, deque
from collections.abc import Awaitable, Callable, Sequence
from ipaddress import IPv6Address, ip_address
from typing import Any, cast
from urllib.parse import urlsplit, urlunsplit

from ..._idempotency import unresolved_commit_error
from ..._source.batch import SourceUrlBatchItem
from ..._types.enums import GrpcStatusCode, SourceStatus, normalize_rpc_code
from ...exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    SourceAddError,
)
from ...rpc import RPCError, RPCMethod
from ...types import Source
from ..contracts import RpcCaller
from ..params.sources import build_template_block
from ..rows.sources import unwrap_add_source_rows

ListSources = Callable[..., Awaitable[list[Source]]]
_PERCENT_ESCAPE = re.compile(r"%[0-9a-fA-F]{2}")


def _url_spec(url: str, *, youtube: bool) -> list[Any]:
    """Build one repeated web-page or YouTube ``UserContent`` entry."""
    if youtube:
        return [None, None, None, None, None, None, None, [url], None, None, 1]
    return [None, None, [url], None, None, None, None, None, None, None, 1]


def _url_identity(
    url: str,
    *,
    extract_youtube_video_id: Callable[[str], str | None],
) -> tuple[str, str]:
    """Return a conservative identity for sparse-response reconciliation."""

    def _normalize_percent_escapes(value: str) -> str:
        return _PERCENT_ESCAPE.sub(lambda match: match.group(0).upper(), value)

    candidate = url.strip()
    video_id = extract_youtube_video_id(candidate)
    if video_id is not None:
        return ("youtube", video_id)

    try:
        parsed = urlsplit(candidate)
        # URL inputs are validated before this private service is called, but a
        # response URL is untrusted wire data. Keep malformed values distinct
        # and let the caller fail closed rather than raising from normalization.
        port = parsed.port
    except (AttributeError, TypeError, ValueError):
        return ("raw", candidate)
    if not parsed.scheme or not parsed.hostname:
        return ("raw", candidate)

    hostname = parsed.hostname.lower()
    is_ipv6 = False
    try:
        address = ip_address(hostname)
    except ValueError:
        pass
    else:
        hostname = address.compressed
        is_ipv6 = isinstance(address, IPv6Address)
    if is_ipv6:
        hostname = f"[{hostname}]"
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    authority = hostname if port is None or default_port else f"{hostname}:{port}"
    if parsed.username is not None:
        userinfo = _normalize_percent_escapes(parsed.username)
        if parsed.password is not None:
            userinfo += f":{_normalize_percent_escapes(parsed.password)}"
        authority = f"{userinfo}@{authority}"
    normalized = urlunsplit(
        (
            parsed.scheme.lower(),
            authority,
            _normalize_percent_escapes(parsed.path or "/"),
            _normalize_percent_escapes(parsed.query),
            "",
        )
    )
    return ("url", normalized)


def _unresolved_batch_error(urls: Sequence[str], message: str, cause: Exception) -> SourceAddError:
    preview = ", ".join(repr(url) for url in urls[:3])
    if len(urls) > 3:
        preview += f", … ({len(urls)} total)"
    return cast(
        SourceAddError,
        unresolved_commit_error(
            RPCMethod.ADD_SOURCE,
            "the batch URL add",
            SourceAddError(
                preview,
                cause=cause,
                message=(
                    "UNRESOLVED — do not blindly retry; check the notebook source list and "
                    f"reconcile these URLs first: {preview}. {message}"
                ),
            ),
            preserve_exception=True,
        ),
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
            exc.args = (
                "UNRESOLVED — do not blindly retry; check the notebook source list and "
                "reconcile the batch URLs first. The batch transport failed and an "
                f"unknown subset may have committed; no automatic retry was attempted. {original}",
            )
            unresolved_commit_error(
                RPCMethod.ADD_SOURCE,
                "the batch URL add",
                exc,
                preserve_exception=True,
            )
            raise
        except RPCError as exc:
            # Live-characterized all-failed URL batches use the same explicit
            # FAILED_PRECONDITION status as a single rejected URL.  A generic
            # decoder/protocol failure is not that evidence: the write may have
            # committed, so fail closed instead of pretending every item failed.
            if normalize_rpc_code(exc.rpc_code) != GrpcStatusCode.FAILED_PRECONDITION.value:
                original = str(exc)
                exc.args = (
                    "UNRESOLVED — do not blindly retry; check the notebook source list and "
                    "reconcile the batch URLs first. The batch RPC failed without the "
                    "documented all-rejected status, so its committed subset is unknown; "
                    f"no automatic retry was attempted. {original}",
                )
                unresolved_commit_error(
                    RPCMethod.ADD_SOURCE,
                    "the batch URL add",
                    exc,
                    preserve_exception=True,
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

        if len(sources) > len(urls):
            error = RPCError(
                f"ADD_SOURCE returned {len(sources)} rows for {len(urls)} requests",
                method_id=RPCMethod.ADD_SOURCE.value,
            )
            raise _unresolved_batch_error(
                urls,
                "The batch response contained more sources than requested, so positional "
                "outcomes cannot be trusted; no automatic retry was attempted.",
                error,
            )

        requested_identities = [
            _url_identity(url, extract_youtube_video_id=extract_youtube_video_id) for url in urls
        ]

        # Live characterization establishes that successful rows retain request
        # order. A complete response is therefore attributable positionally,
        # but every URL-bearing row must still identify the corresponding input
        # (after conservative canonicalization) so an unexpected or duplicated
        # row is never assigned to the wrong request. Legacy short rows without
        # URL metadata retain the documented positional fallback.
        if len(sources) == len(urls):
            for source, requested_identity in zip(sources, requested_identities, strict=True):
                if source.url is None:
                    continue
                response_identity = _url_identity(
                    source.url,
                    extract_youtube_video_id=extract_youtube_video_id,
                )
                if response_identity != requested_identity:
                    error = RPCError(
                        f"ADD_SOURCE returned unexpected positional URL {source.url!r}",
                        method_id=RPCMethod.ADD_SOURCE.value,
                    )
                    raise _unresolved_batch_error(
                        urls,
                        "The complete batch response did not match request order, so positional "
                        "outcomes cannot be trusted; no automatic retry was attempted.",
                        error,
                    )
            return [
                SourceUrlBatchItem(url=url, source=source)
                for url, source in zip(urls, sources, strict=True)
            ]

        requested_counts = Counter(requested_identities)
        identity_to_url = dict(zip(requested_identities, urls, strict=True))
        sources_by_identity: dict[tuple[str, str], deque[Source]] = defaultdict(deque)
        sources_without_url: list[Source] = []
        for source in sources:
            if source.url is None:
                sources_without_url.append(source)
                continue
            identity = _url_identity(
                source.url,
                extract_youtube_video_id=extract_youtube_video_id,
            )
            if identity not in requested_counts:
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
            sources_by_identity[identity].append(source)

        # A sparse response must identify every returned row by URL: response
        # order alone cannot reveal which request positions were silently
        # omitted. Complete responses returned positionally above.
        if sources_without_url:
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

        # Equivalent URL identities are representable when all copies share one
        # outcome. A partial duplicate group is not attributable per position:
        # the response has no request index or idempotency key, so fail closed.
        for identity, count in requested_counts.items():
            success_count = len(sources_by_identity[identity])
            url = identity_to_url[identity]
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

        missing_identities = [
            identity for identity in requested_identities if not sources_by_identity[identity]
        ]

        error_rows_by_identity: dict[tuple[str, str], list[Source]] = defaultdict(list)
        if missing_identities:
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
                    if row_url is None:
                        continue
                    identity = _url_identity(
                        row_url,
                        extract_youtube_video_id=extract_youtube_video_id,
                    )
                    if identity in requested_counts:
                        error_rows_by_identity[identity].append(row)

        outcomes: list[SourceUrlBatchItem] = []
        for url, identity in zip(urls, requested_identities, strict=True):
            if sources_by_identity[identity]:
                outcomes.append(
                    SourceUrlBatchItem(url=url, source=sources_by_identity[identity].popleft())
                )
                continue
            ghosts = error_rows_by_identity[identity]
            ghost_text = ""
            if ghosts:
                ids = ", ".join(source.id for source in ghosts[:3])
                suffix = "" if len(ghosts) <= 3 else f", … ({len(ghosts)} rows)"
                ghost_text = f" Existing matching ERROR source row(s): {ids}{suffix}."
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
