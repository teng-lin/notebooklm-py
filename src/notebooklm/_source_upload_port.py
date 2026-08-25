"""The neutral port the file-upload (Scotty) pipeline is reached through.

The ``source.add_file`` workflow is permanently adapter-owned (ADR-0035
addendum D4), so the web backend keeps driving a file-upload pipeline. This
module carries the two contracts that crossing costs, so neither the backend
head nor the client runtime has to name the ``_source`` package:

* :class:`SourceUploadBackend` — the callbacks one ``source.add_file``
  invocation supplies to the pipeline (P9.4 open item 1). The binding row
  builds one over its own row-scoped invoker; the backend installs one as the
  pipeline default at construction.
* :class:`UploadLifecycleHooks` — the loop-affinity and configuration hooks the
  client lifecycle and the backend head call on the pipeline. It is
  deliberately *narrow*: nothing here exposes the upload choreography, which
  reaches the row only as its declared ``source_uploader`` collaborator.

Both are structural, so the pipeline satisfies them without importing this
module's protocol; ``_source.upload`` imports the callback contract because it
stores one.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from ._records import SourceFileRegistrationRecord
from .types import Source

#: One recency-writing notebook snapshot, projected to public sources.
ListSources = Callable[[str], Awaitable[list[Source]]]
#: ``(notebook_id, filename)`` → the neutral registration record.
RegisterFileSource = Callable[[str, str], Awaitable[SourceFileRegistrationRecord]]
#: ``(notebook_id, source_id, new_title)`` → the renamed source, or a null echo.
RenameSource = Callable[[str, str, str], Awaitable[Source | None]]
#: The account's advertised per-notebook source limit, for registration hints.
GetSourceLimit = Callable[[], Awaitable[int | None]]


@dataclass(frozen=True, slots=True)
class SourceUploadBackend:
    """The transport-neutral callbacks one ``source.add_file`` invocation supplies.

    P9.4 (plan open item 1): the ``SOURCE_ADD_FILE`` binding row binds closures
    over its own row-scoped invoker for exactly the duration of its invocation
    through ``SourceUploadPipeline.bind_backend``, so every registration,
    listing, rename and limit lookup the pipeline performs for that upload runs
    under the row's declared natives and failure tagging.
    ``configure_source_backend`` remains for callers that own the pipeline
    directly; a bound backend always wins over the configured callbacks.
    """

    list_sources: ListSources
    register_file_source: RegisterFileSource
    rename_source: RenameSource
    get_source_limit: GetSourceLimit | None = None


class UploadLifecycleHooks(Protocol):
    """The upload pipeline as the client lifecycle and the backend head see it.

    Loop affinity (ADR-0004) plus the two configuration seams the composition
    root drives. Upload choreography is deliberately absent: it is reachable
    only through the ``SOURCE_ADD_FILE`` row's declared collaborator.
    """

    def set_bound_loop(self, loop: asyncio.AbstractEventLoop | None) -> None:
        """Capture or clear the event-loop binding for the affinity guard."""
        ...

    def reset_after_open(self) -> None:
        """Discard loop-bound primitives so a reopened client rebinds them."""
        ...

    def configure_source_limit_lookup(self, get_source_limit: GetSourceLimit | None) -> None:
        """Set the optional source-limit lookup used in registration hints."""
        ...

    def configure_source_backend(
        self,
        *,
        list_sources: ListSources,
        register_file_source: RegisterFileSource,
        rename_source: RenameSource,
    ) -> None:
        """Attach the default source callbacks owned by the backend."""
        ...


__all__ = [
    "GetSourceLimit",
    "ListSources",
    "RegisterFileSource",
    "RenameSource",
    "SourceUploadBackend",
    "UploadLifecycleHooks",
]
