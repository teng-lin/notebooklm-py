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

from .._url_utils import is_youtube_url
from ..exceptions import DecodingError, SourceNotFoundError, ValidationError
from ..types import CopiedSource, Source, SourceStatus
from .codecs.sources import decode_source
from .session import AndroidSession
from .write_safety import call_unconfirmed_on_transport_loss

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
    """``AddSourcesAsync`` / ``AppendSource`` / ``CopySourcesAsync`` over gRPC."""

    _transport: AndroidSession

    async def add_urls_async(
        self,
        notebook_id: str,
        urls: builtins.list[str],
    ) -> builtins.list[Source]:
        """Queue ``urls`` with one ``AddSourcesAsync`` call and return the stub rows.

        Request is the exact ``AddSourcesRequest`` shape (``AddSources`` and
        ``AddSourcesAsync`` share it); the reply carries the queued ``Source``
        rows at #1 and a per-source ``{source, status}`` acknowledgement list at
        #3. Live the source landed ``READY`` a few seconds later.
        """
        if not urls:
            return []
        if any(not url or not url.strip() for url in urls):
            raise ValidationError("urls must not contain empty entries")
        proto = _write_proto()
        request = proto.AddSourcesRequest(
            user_content=[_url_user_content(url) for url in urls],
            project_id=notebook_id,
            request_context=_request_context(),
        )
        async with self._transport.operation_scope("source.add_urls_async") as lease:
            response = await call_unconfirmed_on_transport_loss(
                lambda: self._transport.unary(
                    ADD_SOURCES_ASYNC_METHOD,
                    request,
                    replay_safe=False,
                    response_type=proto.AddSourcesAsyncResponse,
                    expected_epoch=lease.epoch,
                )
            )
        rows = list(response.sources)
        if not rows:
            raise DecodingError(
                "AddSourcesAsync returned no queued source rows",
                method_id=ADD_SOURCES_ASYNC_METHOD,
            )
        # Queued stub rows carry no settings/status block (read as UNKNOWN by the
        # generic codec); by contract they are still processing.
        sources = [
            _as_processing(decode_source(row, method_id=ADD_SOURCES_ASYNC_METHOD)) for row in rows
        ]
        if any(not source.id for source in sources):
            raise DecodingError(
                "AddSourcesAsync returned a queued source row without an id",
                method_id=ADD_SOURCES_ASYNC_METHOD,
            )
        if len(sources) != len(urls):
            logger.warning(
                "AddSourcesAsync queued %d source(s) for %d URL(s) in notebook %s",
                len(sources),
                len(urls),
                notebook_id,
            )
        for ack in response.acknowledgements:
            if ack.status != 0:
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
        header: str = "",
    ) -> None:
        """Append ``text`` to ``source_id`` in place (``AppendSource``; empty reply)."""
        del notebook_id  # The route is addressed by source id alone.
        if not source_id:
            raise ValidationError("source_id must not be empty")
        if not text:
            raise ValidationError("text must not be empty")
        proto = _write_proto()
        request = proto.AppendSourceRequest(
            source_id=_read_proto().SourceId(id=source_id),
            content=proto.SourceContent(
                plain_text=proto.PlainTextSourceContent(header=header, body=text)
            ),
        )
        async with self._transport.operation_scope("source.append_text") as lease:
            await call_unconfirmed_on_transport_loss(
                lambda: self._transport.unary(
                    APPEND_SOURCE_METHOD,
                    request,
                    replay_safe=False,
                    response_type=_empty_type(),
                    expected_epoch=lease.epoch,
                )
            )

    async def copy(
        self,
        notebook_id: str,
        source_ids: builtins.list[str],
        target_notebook_id: str,
    ) -> builtins.list[CopiedSource]:
        """Copy ``source_ids`` into ``target_notebook_id`` (``CopySourcesAsync``).

        The reply maps each original ``SourceId`` (#1) to the new ``Source`` row
        (#2). An unknown source id or target project draws ``NOT_FOUND``
        (live-verified); an empty mapping on success is treated as
        :class:`SourceNotFoundError` so a no-op never reads as a copy. A partial
        mapping is returned with a warning because those copies have already
        committed.
        """
        del notebook_id  # The route is addressed by source ids + target alone.
        if not source_ids:
            raise ValidationError("source_ids must not be empty")
        if any(not source_id for source_id in source_ids):
            raise ValidationError("source_ids must not contain empty entries")
        if not target_notebook_id:
            raise ValidationError("target_notebook_id must not be empty")
        proto = _write_proto()
        read_proto = _read_proto()
        request = proto.CopySourcesAsyncRequest(
            source_ids=[read_proto.SourceId(id=source_id) for source_id in source_ids],
            target_project_id=target_notebook_id,
        )
        async with self._transport.operation_scope("source.copy") as lease:
            response = await call_unconfirmed_on_transport_loss(
                lambda: self._transport.unary(
                    COPY_SOURCES_ASYNC_METHOD,
                    request,
                    replay_safe=False,
                    response_type=proto.CopySourcesAsyncResponse,
                    expected_epoch=lease.epoch,
                )
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
        if not copied:
            if malformed:
                raise DecodingError(
                    "CopySourcesAsync returned only malformed mapping entries",
                    method_id=COPY_SOURCES_ASYNC_METHOD,
                )
            raise SourceNotFoundError(", ".join(source_ids), method_id=COPY_SOURCES_ASYNC_METHOD)
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


__all__ = [
    "ADD_SOURCES_ASYNC_METHOD",
    "APPEND_SOURCE_METHOD",
    "COPY_SOURCES_ASYNC_METHOD",
    "AndroidSourceTransferMixin",
]
