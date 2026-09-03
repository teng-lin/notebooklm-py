"""Android gRPC source transfers: AddSourcesAsync, AppendSource, CopySourcesAsync.

The #2283 source transfer family, live-validated over native Android gRPC on
2026-09-01 (``docs/android/copy-append-suggestion-evidence.md``). Every route is
served to the Android bearer directly — the "rejected by impersonation policy"
result recorded for ``AddSourcesAsync`` in ``web-compat-seam-closure.md`` was
the *web* upload-finalize path called with an Android bearer, not this one.

Kept as a mixin so ``_android/sources.py`` stays under the ADR-0008 module-size
budget; :class:`AndroidSourcesAPI` inherits it and supplies ``_transport``.
"""

from __future__ import annotations

import builtins
import logging
from dataclasses import replace
from typing import Any, cast

from .._idempotency import call_unconfirmed_on_transport_loss
from .._sources import _TransferResult
from .._url_utils import is_youtube_url
from ..types import CopiedSource, Source, SourceStatus
from .codecs.sources import decode_source
from .session import AndroidSession

logger = logging.getLogger(__name__)

_SERVICE = "google.internal.labs.tailwind.orchestration.v1.LabsTailwindOrchestrationService"
ADD_SOURCES_ASYNC_METHOD = f"/{_SERVICE}/AddSourcesAsync"
APPEND_SOURCE_METHOD = f"/{_SERVICE}/AppendSource"
COPY_SOURCES_ASYNC_METHOD = f"/{_SERVICE}/CopySourcesAsync"


def _read_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import read_pb2

    return cast(Any, read_pb2)


def _write_proto() -> Any:
    from .proto.google.internal.labs.tailwind.orchestration.v1 import sources_pb2

    return cast(Any, sources_pb2)


def _empty_type() -> Any:
    from google.protobuf.empty_pb2 import Empty

    return Empty


def _request_context() -> Any:
    from .upload import android_request_context

    return android_request_context()


def _url_user_content(url: str) -> Any:
    """Build one ``UserContent`` for a web page or YouTube URL (no tentative id)."""
    proto = _write_proto()
    if is_youtube_url(url):
        return proto.UserContent(video_content=proto.VideoContent(youtube_url=url))
    return proto.UserContent(web_content=proto.WebContent(url=url))


def _as_processing(source: Source) -> Source:
    if source.status is SourceStatus.UNKNOWN:
        return replace(source, status=SourceStatus.PROCESSING)
    return source


class AndroidSourceTransferMixin:
    """``AddSourcesAsync`` / ``AppendSource`` / ``CopySourcesAsync`` over gRPC.

    ``AndroidSourcesAPI._operation_scope`` binds a lease epoch task-locally
    before calling these hooks. ``AndroidSession.unary`` resolves each omitted
    ``expected_epoch`` from that binding before dispatch.
    """

    _transport: AndroidSession

    async def _send_add_urls_async(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> _TransferResult[Source]:
        """Queue ``urls`` with one ``AddSourcesAsync`` call and return the stub rows.

        Request is the exact ``AddSourcesRequest`` shape (``AddSources`` and
        ``AddSourcesAsync`` share it); the reply carries the queued ``Source``
        rows at #1 and a per-source ``{source, status}`` acknowledgement list at
        #3. Live the source landed ``READY`` a few seconds later.
        """
        proto = _write_proto()
        request = proto.AddSourcesRequest(
            user_content=[_url_user_content(url) for url in urls],
            project_id=notebook_id,
            request_context=_request_context(),
        )
        response = await call_unconfirmed_on_transport_loss(
            lambda: self._transport.unary(
                ADD_SOURCES_ASYNC_METHOD,
                request,
                replay_safe=False,
                response_type=proto.AddSourcesAsyncResponse,
            ),
            method=ADD_SOURCES_ASYNC_METHOD,
            what="AddSourcesAsync",
            chain=None,
        )
        rows = list(response.sources)
        # Queued stub rows carry no settings/status block (read as UNKNOWN by the
        # generic codec); by contract they are still processing.
        sources = [
            _as_processing(decode_source(row, method_id=ADD_SOURCES_ASYNC_METHOD)) for row in rows
        ]
        for ack in response.acknowledgements:
            if ack.status != 0:
                logger.warning(
                    "AddSourcesAsync acknowledgement carried status %r for notebook %s",
                    ack.status,
                    notebook_id,
                )
        return _TransferResult(sources, ADD_SOURCES_ASYNC_METHOD)

    async def _send_append_text(
        self,
        notebook_id: str,
        source_id: str,
        text: str,
        *,
        header: str = "",
    ) -> None:
        """Append ``text`` to ``source_id`` in place (``AppendSource``; empty reply)."""
        del notebook_id  # The route is addressed by source id alone.
        proto = _write_proto()
        request = proto.AppendSourceRequest(
            source_id=_read_proto().SourceId(id=source_id),
            content=proto.SourceContent(
                plain_text=proto.PlainTextSourceContent(header=header, body=text)
            ),
        )
        await call_unconfirmed_on_transport_loss(
            lambda: self._transport.unary(
                APPEND_SOURCE_METHOD,
                request,
                replay_safe=False,
                response_type=_empty_type(),
            ),
            method=APPEND_SOURCE_METHOD,
            what="AppendSource",
            chain=None,
        )

    async def _send_copy(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> _TransferResult[CopiedSource]:
        """Copy ``source_ids`` into ``target_notebook_id`` (``CopySourcesAsync``).

        The reply maps each original ``SourceId`` (#1) to the new ``Source`` row
        (#2). An unknown source id or target project draws ``NOT_FOUND``
        (live-verified). The neutral facade interprets empty and partial
        mappings after this wire decoder returns.
        """
        del notebook_id  # The route is addressed by source ids + target alone.
        proto = _write_proto()
        read_proto = _read_proto()
        request = proto.CopySourcesAsyncRequest(
            source_ids=[read_proto.SourceId(id=source_id) for source_id in source_ids],
            target_project_id=target_notebook_id,
        )
        response = await call_unconfirmed_on_transport_loss(
            lambda: self._transport.unary(
                COPY_SOURCES_ASYNC_METHOD,
                request,
                replay_safe=False,
                response_type=proto.CopySourcesAsyncResponse,
            ),
            method=COPY_SOURCES_ASYNC_METHOD,
            what="CopySourcesAsync",
            chain=None,
        )
        # Malformed entries are skipped, not fatal: the well-formed ones are the
        # only proof of copies that have already committed.
        copied: builtins.list[CopiedSource] = []
        malformed = 0
        for entry in response.copied_sources:
            original_id = entry.source_id.id
            source = (
                decode_source(entry.source, method_id=COPY_SOURCES_ASYNC_METHOD)
                if entry.HasField("source")
                else None
            )
            if not original_id or source is None or not source.id:
                malformed += 1
                logger.warning("CopySourcesAsync returned a malformed mapping entry")
                continue
            copied.append(CopiedSource(original_id=original_id, source=source))
        return _TransferResult(
            copied,
            COPY_SOURCES_ASYNC_METHOD,
            malformed_count=malformed,
        )


__all__ = [
    "ADD_SOURCES_ASYNC_METHOD",
    "APPEND_SOURCE_METHOD",
    "COPY_SOURCES_ASYNC_METHOD",
    "AndroidSourceTransferMixin",
]
