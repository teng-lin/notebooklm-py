"""The service-owned ``source.add_url_batch`` workflow (P10 R3.5).

One non-replayed ``source.register`` url write carrying every validated entry,
the positional attribution of its response, and — only when that response omits
entries — one ``source.list`` of ERROR rows to name the ghosts behind them.

Deliberately *not* the single-item ``source.add_url`` contract, which
:class:`~notebooklm._source_service.SourceService` owns next door. The batch
write is never automatically replayed: a transport failure can occur after an
arbitrary subset committed, so retrying the request would duplicate it.

``SourceService.add_urls_batch`` is still the one entry point — the facade and
the MCP/REST adapters reach the workflow only through it. The body lives here
because ``_source_service`` is at the module-size budget with four hoisted
workflows in it (ADR-0008), not because the batch is a second service. The two
neutral failure-report builders it shares with them come from
``_source_add_reports``, which the same budget split off.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import replace

from ._backend import (
    BackendAdapter,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    rebind_operation,
)
from ._deadline import RuntimeDeadline
from ._operations import Operation
from ._records import (
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_LIST_DEF,
    SOURCE_REGISTER_DEF,
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceAddUrlBatchResult,
    SourceListInput,
    SourceRecord,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceUrlBatchItemRecord,
)
from ._source_add_reports import leaf_failure_record, source_add_failure
from ._url_utils import extract_youtube_video_id, url_identity

# The same logger name and level the retired row logged under.
_source_logger = logging.getLogger("notebooklm").getChild("_sources")

#: The batch write's first two ``except`` clauses after the auth one, as neutral
#: reasons: ``RateLimitError``/``ServerError``/``NetworkError`` plus ``TIMEOUT``
#: (``RPCTimeoutError`` is a ``NetworkError`` subclass). All four keep their
#: public type and only have their message rewritten (#2220).
_BATCH_UNRESOLVED_TRANSPORT_REASONS = frozenset(
    {
        BackendErrorReason.NETWORK,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.SERVER,
        BackendErrorReason.TIMEOUT,
    }
)

#: ``except DecodingError`` — and its ``UnknownRPCMethodError`` subclass, which
#: reached the same clause below the port.
_BATCH_UNDECODABLE_REASONS = frozenset(
    {
        BackendErrorReason.DECODING,
        BackendErrorReason.UNKNOWN_RPC_METHOD,
    }
)

#: What the batch write's final ``except RPCError`` still saw once auth, the
#: transport three and the decoding family had been taken by earlier clauses.
#: This is the one clause that can answer "the backend rejected every URL".
_BATCH_RESIDUAL_RPC_REASONS = frozenset(
    {
        BackendErrorReason.CLIENT,
        BackendErrorReason.RESPONSE_TOO_LARGE,
        BackendErrorReason.RPC,
    }
)

#: gRPC ``FAILED_PRECONDITION`` (``google.rpc.Code`` 9) — the explicit status a
#: fully rejected URL batch shares with a single rejected URL, and the only
#: evidence that turns a create failure into per-item results instead of one
#: unresolved write. Spelled as the literal because ``rpc.types`` is wire
#: vocabulary a semantic service may not import (P10 invariant I1);
#: ``test_source_add_url_batch_workflow`` pins it against ``GrpcStatusCode``.
_ALL_REJECTED_RPC_CODE = 9

#: ``SourceRecord.status`` for a failed source row, i.e. what the web adapter's
#: ``source_status_to_str(SourceStatus.ERROR)`` renders. Same rationale and same
#: pin as ``_ALL_REJECTED_RPC_CODE``; ``_app/source_clean.py`` names it the same
#: way above the port.
_ERROR_SOURCE_STATUS = "error"


def _normalized_rpc_code(code: str | int | None) -> int | None:
    """Coerce a captured ``rpc_code`` to an ``int``, or ``None``.

    The neutral twin of the web adapter's ``normalize_rpc_code``: the field is
    typed ``str | int | None`` and carries a numeric status, a non-numeric label
    such as ``"USER_DISPLAYABLE_ERROR"``, or nothing at all, so ``"9"`` has to
    compare equal to ``9`` and a label has to answer ``None`` rather than raise.
    ``bool`` is rejected explicitly — it is an ``int`` subclass, so ``True``
    would otherwise coerce to ``1``.
    """
    if code is None or isinstance(code, bool):
        return None
    try:
        return int(code)
    except (TypeError, ValueError):
        return None


async def run_url_batch_registration(
    backend: BackendAdapter,
    notebook_id: str,
    urls: tuple[str, ...],
    *,
    deadline: RuntimeDeadline | None,
) -> SourceAddUrlBatchResult:
    """Register many URLs in one write and attribute the response positionally.

    The workflow the P9.4b ``source.add_url_batch`` row owned, sequenced over
    typed leaves instead: one ``source.register`` url allocation carrying
    every validated URL, and — only when the echo omits some of them — one
    reconciling ``source.list`` of ERROR rows to name the ghosts.

    Deliberately *not* the single-item ``SourceService.add_url`` contract. The batch
    write is never automatically replayed: a transport failure can occur
    after an arbitrary subset committed, so retrying the whole request would
    duplicate that subset. A protocol-level rejection is the backend's
    documented all-failed result, and is converted into per-item failures
    after the same ERROR-row reconciliation partial success uses.

    Attribution is exact or it is refused. A complete echo is positional,
    but every URL-bearing row must still identify its input (after
    conservative canonicalization) so an unexpected or duplicated row is
    never assigned to the wrong request; a sparse echo is matched by URL
    identity alone, because response order cannot reveal which positions
    were silently omitted. Every way that match can fail to answer — an
    id-less row, more rows than requests, an unrequested or URL-less row, a
    partially admitted duplicate — reports one unresolved failure for the
    whole write rather than guessing at a position.
    """
    workflow = SOURCE_ADD_URL_BATCH_DEF.key
    rejection: SourceAddFailureRecord | None = None
    sources: tuple[SourceRecord, ...] = ()
    try:
        registered = await backend.invoke(
            SOURCE_REGISTER_DEF,
            SourceRegisterInput(
                notebook_id,
                SourceRegisterKind.URL,
                urls=tuple(urls),
                youtube_flags=tuple(
                    extract_youtube_video_id(url, logger=_source_logger) is not None for url in urls
                ),
            ),
            deadline=deadline,
        )
    except BackendError as leaf_error:
        rejection = _batch_registration_rejection(workflow, urls, leaf_error)
    else:
        sources = registered.sources

    # ``decode_add_source_records`` is deliberately tolerant on listing
    # paths, but a mutating response cannot use that degradation policy:
    # outside the explicit all-rejected branch, an empty/malformed payload
    # does not prove that every write was rejected. Treat it as an
    # unresolved write, and never surface an id-less source as success.
    if rejection is None and any(not source.id for source in sources):
        raise _unresolved_batch_error(
            workflow,
            urls,
            "The batch response did not identify its successful writes, so the "
            "committed subset is unknown; no automatic retry was attempted.",
            "ADD_SOURCE returned no decodable source rows with non-empty ids",
        )
    if len(sources) > len(urls):
        raise _unresolved_batch_error(
            workflow,
            urls,
            "The batch response contained more sources than requested, so positional "
            "outcomes cannot be trusted; no automatic retry was attempted.",
            f"ADD_SOURCE returned {len(sources)} rows for {len(urls)} requests",
        )

    requested_identities = [url_identity(url, logger=_source_logger) for url in urls]

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
            if url_identity(source.url, logger=_source_logger) != requested_identity:
                raise _unresolved_batch_error(
                    workflow,
                    urls,
                    "The complete batch response did not match request order, so positional "
                    "outcomes cannot be trusted; no automatic retry was attempted.",
                    f"ADD_SOURCE returned unexpected positional URL {source.url!r}",
                )
        return SourceAddUrlBatchResult(
            tuple(
                SourceUrlBatchItemRecord(url=url, source=source)
                for url, source in zip(urls, sources, strict=True)
            )
        )

    requested_counts = Counter(requested_identities)
    identity_to_url = dict(zip(requested_identities, urls, strict=True))
    sources_by_identity: dict[tuple[str, str], deque[SourceRecord]] = defaultdict(deque)
    sources_without_url: list[SourceRecord] = []
    for source in sources:
        if source.url is None:
            sources_without_url.append(source)
            continue
        identity = url_identity(source.url, logger=_source_logger)
        if identity not in requested_counts:
            raise _unresolved_batch_error(
                workflow,
                urls,
                "The batch response contained an unrequested source, so positional "
                "outcomes cannot be trusted; no automatic retry was attempted.",
                f"ADD_SOURCE returned an unrequested URL {source.url!r}",
            )
        sources_by_identity[identity].append(source)

    # A sparse response must identify every returned row by URL: response
    # order alone cannot reveal which request positions were silently
    # omitted. Complete responses returned positionally above.
    if sources_without_url:
        raise _unresolved_batch_error(
            workflow,
            urls,
            "The partial batch response omitted URL metadata needed to match rows "
            "back to inputs; no automatic retry was attempted.",
            "ADD_SOURCE returned sparse source rows without URL metadata",
        )

    # Equivalent URL identities are representable when all copies share one
    # outcome. A partial duplicate group is not attributable per position:
    # the response has no request index or idempotency key, so fail closed.
    for identity, count in requested_counts.items():
        success_count = len(sources_by_identity[identity])
        url = identity_to_url[identity]
        if success_count > count:
            raise _unresolved_batch_error(
                workflow,
                urls,
                "The batch response contained more sources than requested, so positional "
                "outcomes cannot be trusted; no automatic retry was attempted.",
                f"ADD_SOURCE returned {success_count} rows for {count} request(s) of {url!r}",
            )
        if count > 1 and 0 < success_count < count:
            raise _unresolved_batch_error(
                workflow,
                [url] * count,
                "The backend partially admitted identical URLs without returning request "
                "positions, so the successful copies are ambiguous; no automatic retry "
                "was attempted.",
                f"ADD_SOURCE partially admitted duplicate URL {url!r}",
            )

    error_rows_by_identity = await _batch_error_rows(
        backend,
        notebook_id,
        requested_counts,
        missing=any(not sources_by_identity[identity] for identity in requested_identities),
        deadline=deadline,
    )

    items: list[SourceUrlBatchItemRecord] = []
    for url, identity in zip(urls, requested_identities, strict=True):
        if sources_by_identity[identity]:
            items.append(
                SourceUrlBatchItemRecord(
                    url=url,
                    source=sources_by_identity[identity].popleft(),
                )
            )
            continue
        ghosts = error_rows_by_identity.get(identity, ())
        ghost_text = ""
        if ghosts:
            ids = ", ".join(source.id for source in ghosts[:3])
            suffix = "" if len(ghosts) <= 3 else f", … ({len(ghosts)} rows)"
            ghost_text = f" Existing matching ERROR source row(s): {ids}{suffix}."
        message = (
            f"Failed to add URL source {url!r}: the backend omitted it from "
            f"the batch success response.{ghost_text}"
        )
        items.append(
            SourceUrlBatchItemRecord(
                url=url,
                error=SourceAddFailureRecord(
                    kind=SourceAddFailureKind.SOURCE_ADD,
                    message=message,
                    args=(message,),
                    url=url,
                    # The per-item failure is *constructed*, never raised, so
                    # the all-rejected leaf is its ``cause`` attribute only:
                    # ``__cause__``/``__context__`` stay unset.
                    cause=rejection,
                ),
            )
        )
    return SourceAddUrlBatchResult(tuple(items))


async def _batch_error_rows(
    backend: BackendAdapter,
    notebook_id: str,
    requested_counts: Mapping[tuple[str, str], int],
    *,
    missing: bool,
    deadline: RuntimeDeadline | None,
) -> Mapping[tuple[str, str], Sequence[SourceRecord]]:
    """Name the ghost ERROR rows behind the entries the echo omitted.

    Diagnostics only, and swallowed on failure: the success response already
    proves which entries were admitted (or the all-rejected leaf proves none
    were), so this read enriches the per-item failures and must never
    discard otherwise-known positional outcomes by raising.
    """
    rows: defaultdict[tuple[str, str], list[SourceRecord]] = defaultdict(list)
    if not missing:
        return rows
    try:
        listed = await backend.invoke(
            SOURCE_LIST_DEF,
            SourceListInput(notebook_id, statuses=frozenset({_ERROR_SOURCE_STATUS})),
            deadline=deadline,
        )
    except Exception:
        _source_logger.warning(
            "add_urls_batch: failed to list ERROR rows after ADD_SOURCE; "
            "returning per-item failures without ghost ids",
            exc_info=True,
        )
        return rows
    for row in listed.sources:
        if row.url is None:
            continue
        identity = url_identity(row.url, logger=_source_logger)
        if identity in requested_counts:
            rows[identity].append(row)
    return rows


def _batch_registration_rejection(
    workflow: Operation,
    urls: Sequence[str],
    error: BackendError,
) -> SourceAddFailureRecord:
    """Answer "the backend rejected every URL", or raise the batch's failure.

    The one branch that *returns* is the documented all-rejected result: an
    ``RPCError`` carrying the same explicit ``FAILED_PRECONDITION`` status a
    single rejected URL gets. Its record becomes every item's cause, so the
    adapter contract stays a per-item result array instead of a top-level
    failure. Everything else raises, in the pre-P10 catch order:

    * an auth rejection cannot have committed and keeps its normal adapter
      contract (401 / re-auth guidance), so it is re-reported unchanged;
    * the transport three (plus ``RPCTimeoutError``, a ``NetworkError``
      subclass) keep their own type and fields (ADR-0019) with only the
      message rewritten, because the write-uncertainty has to dominate
      retry classification (#2220);
    * an undecodable response, and any other ``RPCError``, becomes the
      unresolved ``SOURCE_ADD`` report — the first with the leaf as its
      explicit cause, the second with the leaf's own rewritten message.
    """
    if isinstance(error, BackendContractError) or error.reason is None:
        raise error
    if isinstance(error, BackendDeadlineExceededError):
        # An expiry is the caller's own budget answering, not a source-add
        # failure: keep the subclass and only re-attribute it.
        raise rebind_operation(error, workflow)
    leaf = leaf_failure_record(error)
    if leaf is None:
        # Nothing to replay: re-attribute the reason and let the
        # compatibility projector build the public exception from it.
        raise rebind_operation(error, workflow)
    if error.reason in _BATCH_UNRESOLVED_TRANSPORT_REASONS:
        raise _rewritten_batch_leaf(
            workflow,
            error,
            leaf,
            "The batch transport failed and an "
            "unknown subset may have committed; no automatic retry was attempted.",
        )
    if error.reason in _BATCH_UNDECODABLE_REASONS:
        raise _unresolved_batch_error(
            workflow,
            urls,
            "The create response could not be decoded, so the committed subset is unknown.",
            cause=leaf,
        )
    if error.reason in _BATCH_RESIDUAL_RPC_REASONS:
        # Live-characterized all-failed URL batches use the same explicit
        # FAILED_PRECONDITION status as a single rejected URL. A generic
        # decoder/protocol failure is not that evidence: the write may have
        # committed, so fail closed instead of pretending every item failed.
        if _normalized_rpc_code(leaf.rpc_code) == _ALL_REJECTED_RPC_CODE:
            return leaf
        raise _rewritten_batch_leaf(
            workflow,
            error,
            leaf,
            "The batch RPC failed without the "
            "documented all-rejected status, so its committed subset is unknown; "
            "no automatic retry was attempted.",
        )
    # Every other reviewed public leaf escaped the pre-P10 service uncaught
    # and was captured whole by the row's ``except NotebookLMError``.
    raise source_add_failure(
        workflow,
        leaf,
        outcome_unknown=error.outcome_unknown,
        dispatched=error.dispatched,
    )


def _rewritten_batch_leaf(
    workflow: Operation,
    error: BackendError,
    leaf: SourceAddFailureRecord,
    detail: str,
) -> BackendError:
    """Re-report one leaf under the unresolved-write message, type intact.

    ADR-0019: the caller still needs the transport type to classify the
    failure, so only ``args`` are rewritten — around the leaf's own rendered
    message, which becomes the tail of the new one. None of the types that
    reach here render diagnostic fields in ``__str__``, so the leaf's
    captured base message *is* that rendering.
    """
    message = (
        "UNRESOLVED — do not blindly retry; check the notebook source list and "
        f"reconcile the batch URLs first. {detail} {leaf.message}"
    )
    return source_add_failure(
        workflow,
        replace(leaf, message=message, args=(message,), unconfirmed=True),
        outcome_unknown=True,
        dispatched=error.dispatched,
    )


def _unresolved_batch_error(
    workflow: Operation,
    urls: Sequence[str],
    detail: str,
    rpc_message: str | None = None,
    *,
    cause: SourceAddFailureRecord | None = None,
) -> BackendError:
    """The batch's "cannot attribute this write" report, chain and all.

    ``rpc_message`` is the manufactured protocol complaint the pre-P10
    service raised as an in-process ``RPCError`` purely to carry as the
    ``cause`` attribute; above the port it is a nested record instead, and
    ``_backend_compat`` rebuilds the identical two-node graph. ``cause``
    supplies a *real* leaf's record instead, for the one branch that had a
    live exception to point at (``raise ... from exc``) — which is also the
    only branch whose report has an explicit ``__cause__``.
    """
    preview = ", ".join(repr(url) for url in urls[:3])
    if len(urls) > 3:
        preview += f", … ({len(urls)} total)"
    message = (
        "UNRESOLVED — do not blindly retry; check the notebook source list and "
        f"reconcile these URLs first: {preview}. {detail}"
    )
    explicit = cause is not None
    if not explicit and rpc_message is not None:
        cause = SourceAddFailureRecord(
            kind=SourceAddFailureKind.RPC,
            message=rpc_message,
            args=(rpc_message,),
        )
    return source_add_failure(
        workflow,
        SourceAddFailureRecord(
            kind=SourceAddFailureKind.SOURCE_ADD,
            message=message,
            args=(message,),
            url=preview,
            unconfirmed=True,
            cause=cause,
            # ``raise SourceAddError(...) from exc`` inside ``except
            # DecodingError as exc``: the leaf is the explicit cause, the
            # implicit context, and the reason the context is suppressed.
            # The manufactured-cause branches are a bare ``raise`` outside
            # any handler, so all three stay false there.
            context_is_cause=explicit,
            explicit_cause=explicit,
            suppress_context=explicit,
        ),
        outcome_unknown=True,
        # ``dispatched`` stays False: the report is a *new* object, and only
        # a leaf the transport itself tagged carries that flag.
    )


__all__ = ["run_url_batch_registration"]
