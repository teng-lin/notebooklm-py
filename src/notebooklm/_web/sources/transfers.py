"""Web ``batchexecute`` source transfer operations (#2283).

Three RPCs that move or extend source content without the probe-then-create
choreography of :mod:`notebooklm._web.sources.add`:

* :meth:`SourceTransferService.add_urls_async` — ``AddSourcesAsync`` (``X1snv``),
  the non-blocking batch add that returns queued stub rows;
* :meth:`SourceTransferService.append_text` — ``AppendSource`` (``QsNTEd``),
  appends a plain-text block to an existing source in place;
* :meth:`SourceTransferService.copy` — ``CopySourcesAsync`` (``R27wvc``),
  copies sources into another notebook and maps each original to its copy.

All three are mutating writes with no client idempotency token and no cheap
post-failure probe, so internal transport retries are disabled and a lost
response is surfaced as an *unconfirmed* error (``mark_unconfirmed``) rather
than replayed — the same contract ``CopyProject`` / the batch add use.

Kept out of ``_web/sources/__init__.py`` so that module stays under the
ADR-0008 size budget; the public :class:`WebSourcesAPI` methods delegate here.
"""

from __future__ import annotations

import builtins
import logging
from collections.abc import Callable
from dataclasses import replace

from ..._idempotency import mark_unconfirmed
from ...exceptions import (
    DecodingError,
    NetworkError,
    RateLimitError,
    RPCError,
    ServerError,
    SourceNotFoundError,
    ValidationError,
)
from ...rpc import RPCMethod
from ...types import CopiedSource, Source, SourceStatus
from ..contracts import RpcCaller
from ..params.sources import (
    build_add_sources_async_params,
    build_append_source_params,
    build_copy_sources_params,
)
from ..rows.transfers import (
    AddSourcesAsyncResponseRow,
    CopiedSourceRow,
    unwrap_mapping_rows,
)
from .batch import _url_spec

__all__ = ["SourceTransferService"]

_UNRESOLVED_HINT = (
    "Do not blindly retry; list the notebook's sources and reconcile first. "
    "No automatic retry was attempted."
)


def _unconfirmed(method: RPCMethod, what: str, exc: Exception) -> RPCError:
    rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
    return mark_unconfirmed(
        RPCError(
            f"UNRESOLVED — {what} may have committed before its response was lost. "
            f"{_UNRESOLVED_HINT} {exc}",
            method_id=method.value,
            rpc_code=rpc_code,
        )
    )


def _as_processing(source: Source) -> Source:
    """Queued stub rows have no status block; they are, by contract, processing."""
    if source.status is SourceStatus.UNKNOWN:
        return replace(source, status=SourceStatus.PROCESSING)
    return source


class SourceTransferService:
    """Issue and decode the three source transfer RPCs."""

    async def add_urls_async(
        self,
        notebook_id: str,
        urls: builtins.list[str],
        *,
        rpc: RpcCaller,
        extract_youtube_video_id: Callable[[str], str | None],
        logger: logging.Logger,
    ) -> builtins.list[Source]:
        """Queue ``urls`` with one ``AddSourcesAsync`` call.

        The reply carries the stub rows at ``[0]`` and a per-source
        acknowledgement list at ``[2]``; the acknowledgement status was ``0``
        on every live observation, so a non-zero value is logged (it is not
        understood) and the row is still returned.
        """
        if not urls:
            return []
        if any(not url or not url.strip() for url in urls):
            raise ValidationError("urls must not contain empty entries")

        params = build_add_sources_async_params(
            [_url_spec(url, youtube=extract_youtube_video_id(url) is not None) for url in urls],
            notebook_id,
        )
        try:
            payload = await rpc.rpc_call(
                RPCMethod.ADD_SOURCES_ASYNC,
                params,
                source_path=f"/notebook/{notebook_id}",
                allow_null=False,
                disable_internal_retries=True,
            )
        except (RateLimitError, ServerError, NetworkError) as exc:
            raise _unconfirmed(RPCMethod.ADD_SOURCES_ASYNC, "AddSourcesAsync", exc) from exc

        view = AddSourcesAsyncResponseRow(payload)
        entries = view.source_entries
        if not entries:
            raise DecodingError(
                "AddSourcesAsync returned no queued source rows",
                raw_response=repr(payload),
                method_id=RPCMethod.ADD_SOURCES_ASYNC.value,
            )
        # The stub rows carry no settings/status block (the sources are queued,
        # not ingested), which the generic decoder reads as UNKNOWN. Project the
        # documented contract — the returned rows are still processing.
        sources = [
            _as_processing(
                Source.from_api_response(entry, method_id=RPCMethod.ADD_SOURCES_ASYNC.value)
            )
            for entry in entries
        ]
        if any(not source.id for source in sources):
            raise DecodingError(
                "AddSourcesAsync returned a queued source row without an id",
                raw_response=repr(payload),
                method_id=RPCMethod.ADD_SOURCES_ASYNC.value,
            )
        if len(sources) != len(urls):
            logger.warning(
                "AddSourcesAsync queued %d source(s) for %d URL(s) in notebook %s",
                len(sources),
                len(urls),
                notebook_id,
            )
        for ack in view.ack_rows:
            if ack.status is not None and not ack.is_ok:
                logger.warning(
                    "AddSourcesAsync acknowledgement carried status %r for notebook %s",
                    ack.status,
                    notebook_id,
                )
        return sources

    async def append_text(
        self,
        notebook_id: str,
        source_id: str,
        text: str,
        *,
        header: str,
        rpc: RpcCaller,
    ) -> None:
        """Append ``text`` to ``source_id`` with one ``AppendSource`` call.

        Success is an empty reply. ``raise_on_null_status`` keeps a rejected
        call (a ``null`` payload carrying a gRPC status) from being read as
        that empty success — the #2290 swallow.
        """
        if not source_id:
            raise ValidationError("source_id must not be empty")
        if not text:
            raise ValidationError("text must not be empty")
        try:
            await rpc.rpc_call(
                RPCMethod.APPEND_SOURCE,
                build_append_source_params(source_id, header=header, body=text),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                raise_on_null_status=True,
                disable_internal_retries=True,
            )
        except (RateLimitError, ServerError, NetworkError) as exc:
            raise _unconfirmed(RPCMethod.APPEND_SOURCE, "AppendSource", exc) from exc

    async def copy(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        target_notebook_id: str,
        *,
        rpc: RpcCaller,
        logger: logging.Logger,
    ) -> builtins.list[CopiedSource]:
        """Copy ``source_ids`` into ``target_notebook_id`` with ``CopySourcesAsync``.

        An unknown source id or target notebook is answered with ``NOT_FOUND``
        (live-verified) and surfaces as ``RPCError``. An *empty* mapping on a
        successful reply is not a documented server behaviour; it is treated as
        :class:`SourceNotFoundError` so a silent no-op can never read as a copy.
        A partial mapping is returned as-is with a warning, because the copies
        it names have already committed.
        """
        if not source_ids:
            raise ValidationError("source_ids must not be empty")
        if any(not source_id for source_id in source_ids):
            raise ValidationError("source_ids must not contain empty entries")
        if not target_notebook_id:
            raise ValidationError("target_notebook_id must not be empty")

        try:
            result = await rpc.rpc_call(
                RPCMethod.COPY_SOURCES,
                build_copy_sources_params(list(source_ids), target_notebook_id),
                source_path=f"/notebook/{notebook_id}",
                allow_null=True,
                raise_on_null_status=True,
                disable_internal_retries=True,
            )
        except (RateLimitError, ServerError, NetworkError) as exc:
            raise _unconfirmed(RPCMethod.COPY_SOURCES, "CopySourcesAsync", exc) from exc

        rows = unwrap_mapping_rows(
            result, method_id=RPCMethod.COPY_SOURCES.value, source="CopySourcesAsync"
        )
        # A malformed entry is logged and skipped rather than aborting the
        # decode: the well-formed entries are the only proof of copies that have
        # already committed, and dropping them would hide committed writes.
        copied: builtins.list[CopiedSource] = []
        malformed = 0
        for raw in rows:
            row = CopiedSourceRow(raw)
            source = (
                Source.from_api_response(row.source_entry, method_id=RPCMethod.COPY_SOURCES.value)
                if row.is_well_formed and row.source_entry is not None
                else None
            )
            if row.original_id is None or source is None or not source.id:
                malformed += 1
                logger.warning(
                    "CopySourcesAsync returned a malformed mapping entry: %s", repr(raw)[:200]
                )
                continue
            copied.append(CopiedSource(original_id=row.original_id, source=source))

        if not copied:
            if malformed:
                raise DecodingError(
                    "CopySourcesAsync returned only malformed mapping entries",
                    raw_response=repr(rows)[:400],
                    method_id=RPCMethod.COPY_SOURCES.value,
                )
            raise SourceNotFoundError(", ".join(source_ids), method_id=RPCMethod.COPY_SOURCES.value)
        missing = set(source_ids) - {item.original_id for item in copied}
        if missing:
            logger.warning(
                "CopySourcesAsync copied %d of %d source(s) into %s; not copied: %s",
                len(copied),
                len(source_ids),
                target_notebook_id,
                ", ".join(sorted(missing)),
            )
        return copied
