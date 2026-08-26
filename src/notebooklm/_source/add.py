"""Private non-file source creation service."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import replace

from .._idempotency import (
    _CreateResultKind,
    _IdempotentCreateResult,
    idempotent_create,
    transport_may_have_committed,
)
from .._idempotency import (
    mark_unconfirmed as _unconfirmed,
)
from ..exceptions import (
    AuthError,
    NetworkError,
    RateLimitError,
    ServerError,
    SourceAddError,
    ValidationError,
)
from ..rpc import RPCError
from ..types import Source

ListSources = Callable[[str], Awaitable[list[Source]]]
WaitUntilReady = Callable[..., Awaitable[Source]]
DriveSourceCreator = Callable[[str, str, str, str], Awaitable[Source | None]]
RenameSource = Callable[[str, str, str], Awaitable[Source | None]]


def _describe_sources(sources: list[Source]) -> str:
    """Render matched sources as ``id (title)`` for an ambiguity message.

    The ambiguity raises tell the caller to go check the notebook's source
    list; naming the exact rows saves them diffing a list by eye against a URL
    that, by definition, appears in it more than once.
    """
    return ", ".join(f"{source.id} ({source.title!r})" for source in sources)


async def honor_requested_title(
    rename: RenameSource,
    notebook_id: str,
    source: Source,
    requested_title: str | None,
    logger: logging.Logger,
) -> Source:
    """Best-effort post-add rename so an explicit ``title`` survives backend
    re-derivation (#1960).

    YouTube, native Google Drive, and web-page imports re-derive the display
    title server-side (from the video / Drive / page metadata), silently
    discarding the ``title`` sent with the add. Live-verified (URL, YouTube, and
    Drive): the backend derives the title *synchronously* — the added source comes
    back already carrying the re-derived title — so a follow-up ``rename`` lands
    after that derivation and sticks. When an explicit ``title`` differs from the
    one the add returned, issue the rename so the requested title wins.

    Non-fatal by contract: the add already succeeded, so a rename failure keeps
    the added source (with its upstream title) and logs a warning rather than
    raising — callers detect the miss by comparing the returned ``source.title``
    against the title they requested (the MCP tool surfaces this).
    """
    if not requested_title:
        return source
    requested = requested_title.strip()
    if not requested or source.title == requested:
        return source
    try:
        renamed = await rename(notebook_id, source.id, requested)
    except (RPCError, NetworkError):
        logger.warning(
            "Source %s added but rename to %r failed; keeping upstream title %r",
            source.id,
            requested,
            source.title,
            exc_info=True,
        )
        return source
    # UPDATE_SOURCE's echo can be sparse (id + title only), so returning it wholesale
    # would drop url / kind / status. Keep the fully-hydrated added source and swap in
    # just the new title — mirrors the file-upload rename (``_source/upload.py``).
    return replace(source, title=(renamed.title if renamed else None) or requested)


async def honor_requested_title_if_fresh(
    rename: RenameSource,
    notebook_id: str,
    result: Source | _IdempotentCreateResult[Source],
    requested_title: str | None,
    logger: logging.Logger,
    *,
    probe_proves_freshness: bool = False,
) -> Source:
    """Apply a requested title only to a source created by this call.

    A ``PROBED`` result is normally skipped because the probe may have matched a
    source that predates this call, and renaming someone else's source would be
    a surprise. Set ``probe_proves_freshness`` when the caller's probe already
    guarantees the match is new — ``add_drive`` (#2113) and ``add_url`` (#2204)
    both filter probe matches against a baseline captured before the create, so
    their ``PROBED`` value is attributable to this call and must still honor the
    requested title. Without this, an add that commits but loses its response
    silently keeps the backend-derived name instead of the caller's ``title``.

    "Attributable", not "proven": a baseline establishes *when* a source
    appeared, not *who* created it — see the concurrency ``.. warning::`` on
    :meth:`SourceAddService.add_url`.
    """
    if isinstance(result, _IdempotentCreateResult):
        if result.kind is _CreateResultKind.PROBED and not probe_proves_freshness:
            return result.value
        source = result.value
    else:
        source = result
    return await honor_requested_title(rename, notebook_id, source, requested_title, logger)


class SourceAddService:
    """Drive source creation behavior.

    What is left of the pre-P10 URL/YouTube/text/Drive service: P10 R3.2
    hoisted ``add_text`` and R3.3 ``add_url`` into ``SourceService`` over the
    ``source.register`` leaf, and R3.4 takes ``add_drive`` the same way. The
    shared title helpers above stay — ``SOURCE_ADD_FILE`` keeps its row under
    decision D4 and reaches them permanently.
    """

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        *,
        mime_type: str = "application/vnd.google-apps.document",
        wait: bool = False,
        wait_timeout: float = 120.0,
        create_source: DriveSourceCreator,
        list_sources: ListSources,
        wait_until_ready: WaitUntilReady,
        logger: logging.Logger,
        return_result: bool = False,
    ) -> Source | _IdempotentCreateResult[Source]:
        """Add a Google Drive document as a source.

        Drive sources go through the same probe-then-create idempotency
        pattern as ``add_url``: a 5xx / network failure
        between server-side commit and client-side response could
        otherwise duplicate the source on a naive retry. The probe matches
        on :attr:`~notebooklm.types.Source.drive_document_id`, the Drive
        ``documentId`` the backend echoes back in the source metadata.
        Drive-backed sources carry **no** URL (their URL slots are empty),
        so a URL-based probe could never match one — it silently duplicated
        the source on every retry until #2113.

        A ``documentId`` is **not** unique within a notebook: the backend
        happily holds the same Drive file twice. The probe therefore filters
        matches against a baseline of source ids captured before the first
        create attempt, exactly like
        :meth:`~notebooklm._source.upload.SourceUploader.register_file_source`
        does for filenames, so a pre-existing copy is never handed back as if
        it were the one just created. This costs one extra source list per
        call; it is the price of telling "my create landed" apart from "a copy
        was already there".

        .. note::
           **This is a behaviour change.** ``add_drive`` now captures a baseline
           on *every* call; previously it listed sources only inside ``_probe``,
           which ``idempotent_create`` runs only after a transport failure. It
           now matches the shape ``register_file_source`` has always had (an
           unconditional pre-create baseline), so the cost is an extension of an
           existing pattern rather than a wholly new one — but for the Drive path
           it is new, moving from retry-only to every call.

           The concrete cost: that list is a ``GET_NOTEBOOK``, and the backend
           **writes** ``lastViewedTime`` when answering one (#2126), so every
           ``add_drive`` now promotes the notebook to the top of the user's
           *Recent* list in the web UI. No cheaper probe exists — source ids are
           published only inside the ``GET_NOTEBOOK`` payload, and
           ``LIST_NOTEBOOKS`` (which does not bump) does not carry them. The bump
           is accepted: silently returning a pre-existing source and reporting a
           create that never happened is far worse than a reordered Recent list.

           Sibling paths, so the next reader need not re-derive it: ``add_text``
           is ``NON_IDEMPOTENT_NO_RETRY`` and has no probe, so it has no such
           exposure. ``add_url`` shared the un-baselined shape this fix replaced
           — a notebook can hold two sources with the same URL — and was given
           the same pre-create baseline in #2204.

        .. warning::
           The baseline establishes *when* a matching source appeared, not
           *who* created it. If two callers add the same Drive file to one
           notebook concurrently and one create fails before committing, the
           failed caller's probe can attribute the other caller's source to
           itself. A list-based probe cannot close that gap — the wire carries
           no client-supplied idempotency key — so serialize concurrent adds of
           the same file into a notebook if you need that guarantee. The same
           limitation applies to ``register_file_source``'s filename probe.

        .. note::
           The ``title`` is sent on the wire but **ignored** for native Drive
           imports: NotebookLM re-derives the display title from live Drive
           metadata, so the returned source keeps the file's Drive name
           regardless of what you pass here. Call
           :meth:`~notebooklm._sources.SourcesAPI.rename` after the add if you
           need a specific title.
        """
        if not file_id or not file_id.strip():
            # Fail before the write rather than POSTing a blank Drive id. A
            # blank id is also unmatchable by the probe below (a row's
            # ``drive_document_id`` is never ``""``), so without this guard a
            # transport failure would retry the blank add and could leave two
            # garbage sources behind.
            raise ValidationError("Drive file_id cannot be empty or whitespace-only")
        logger.debug("Adding Drive source to notebook %s: %s", notebook_id, title)

        async def _create() -> Source:
            # Preserve transport-level signals so callers can act on the
            # specific type (AuthError -> re-login, RateLimitError -> back-off,
            # ServerError -> transient retry). The retryable transport
            # exceptions must propagate so idempotent_create can catch them
            # and run the probe.
            try:
                source = await create_source(notebook_id, file_id, title, mime_type)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise SourceAddError(title, cause=e) from e

            if source is None:
                raise SourceAddError(
                    title,
                    message=(
                        f"API returned no data for Drive source: {title} "
                        f"(mime_type={mime_type!r}). This Drive file type may not be "
                        "importable via Drive — NotebookLM's Drive import supports "
                        "Google-native Docs/Slides/Sheets + PDF only. If it is an "
                        "upload-only type (e.g. epub/docx/txt/md/rtf/odt/csv), "
                        "download it and add it as a `file` source instead."
                    ),
                )
            return source

        # Capture baseline source ids before the first create attempt so the
        # probe can tell "this Drive add landed" from "the same Drive file was
        # already in the notebook". A ``documentId`` is NOT unique within a
        # notebook — live capture (``tests/cassettes/sources_check_freshness_
        # drive.yaml``) holds two source ids sharing one documentId — so an
        # unfiltered match could hand back a pre-existing copy as if it were the
        # one just created, silently masking a failed create. ``None`` is the
        # "baseline unavailable" sentinel; the probe then refuses to guess.
        # Mirrors ``register_file_source`` in ``_source/upload.py``.
        #
        # NEW on every call (it used to list only inside _probe, i.e. only after
        # a transport failure). This list is a GET_NOTEBOOK, which the backend
        # answers by WRITING lastViewedTime (#2126) — so every add_drive now
        # reshuffles the user's Recent ordering. Unavoidable (source ids live
        # only in that payload; LIST_NOTEBOOKS does not bump but does not carry
        # them) and accepted; see the ``.. note::`` on this method.
        baseline_ids: set[str] | None
        # Retained so the ambiguity raise below can name what went wrong, exactly
        # as ``add_url`` does: the caller sees "baseline snapshot failed" long
        # after this line ran, and without the cause there is nothing left in the
        # process that can explain it.
        baseline_error: Exception | None = None
        try:
            baseline_ids = {source.id for source in await list_sources(notebook_id)}
        except Exception as exc:
            baseline_error = exc
            # WARNING, not DEBUG, for the reason spelled out on ``add_url``'s
            # copy of this block (#2204): the default logger level is WARNING,
            # so a DEBUG record here is dropped before any handler sees it and
            # the call silently runs with its idempotency probe disabled.
            logger.warning(
                "add_drive: baseline list() failed (%s); the idempotency probe can no "
                "longer tell a source this call created from one that was already "
                "there, so a transport failure will surface as an ambiguity error "
                "instead of recovering",
                type(exc).__name__,
                exc_info=True,
            )
            baseline_ids = None

        # A Drive-backed source echoes the requested ``file_id`` back as the
        # ``documentId`` in its metadata (``SourceRow.drive_document_id``);
        # it carries no URL at all, which is why the previous ``/d/<file_id>``
        # URL-segment probe could never match and let every retry duplicate the
        # source (#2113). Exact equality — not a substring test — so neither an
        # interior substring nor a prefix collision (``abc`` vs ``abcdef``) can
        # produce a false positive, and non-Drive rows (``drive_document_id is
        # None``) can never match a requested file_id.
        async def _probe() -> Source | None:
            try:
                sources = await list_sources(notebook_id)
            except (AuthError, RateLimitError, ServerError, NetworkError) as exc:
                # Transport- and auth-level probe failures must propagate
                # — see the rationale in ``add_url._probe``.
                # Mark it UNCONFIRMED before it goes (#2220 review): the create
                # may already have committed and this probe could not say, which
                # is the same predicament as the decode branch below. Without the
                # marker a ServerError/RateLimitError here classifies as the
                # *retriable* SERVER/RATE_LIMITED with the hint "retry after a
                # short delay" — and the caller retries the ADD, not the probe.
                # The underlying type is left intact, so "re-authenticate" /
                # "connectivity" remain readable in the message.
                _unconfirmed(exc)
                raise
            except Exception as exc:
                # Propagate, do not retry (#2220) — see the full rationale on
                # ``add_url._probe``. An unanswered probe is not evidence that
                # the create did not land, and this variant has no internal
                # retries left as a net against the duplicate.
                logger.warning(
                    "add_drive: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    type(exc).__name__,
                    exc_info=True,
                )
                raise _unconfirmed(
                    SourceAddError(
                        title,
                        cause=exc,
                        message=(
                            # Action first — see the note on ``add_url``'s copy.
                            "UNRESOLVED — do not blindly retry; check the notebook "
                            "source list first. Cannot confirm Drive source "
                            f"{file_id!r}: the create failed at the transport level "
                            "and may or may not have committed, and the idempotency "
                            f"probe that would settle it failed too "
                            f"({type(exc).__name__}). No FURTHER attempt was made, because "
                            "retrying on an unanswered probe is how duplicates happen — "
                            "but an earlier attempt in this call may also have committed."
                        ),
                    )
                ) from exc
            matches = [source for source in sources if source.drive_document_id == file_id]
            if baseline_ids is not None:
                matches = [source for source in matches if source.id not in baseline_ids]
            elif matches:
                # Without a baseline a match may predate this add — see the
                # ``baseline_ids`` comment for the failure mode this guards.
                raise _unconfirmed(
                    SourceAddError(
                        title,
                        cause=baseline_error,
                        message=(
                            f"Cannot disambiguate Drive source {file_id!r}: the pre-create "
                            f"baseline snapshot failed ({type(baseline_error).__name__}), so "
                            "a matching source may either predate this add or be the one it "
                            "just created. Check the notebook source list before retrying."
                        ),
                    )
                )
            if len(matches) == 1:
                (match,) = matches  # exactly one (len==1 guard); unpack, not matches[0]
                return match
            if len(matches) > 1:
                raise _unconfirmed(
                    SourceAddError(
                        title,
                        message=(
                            f"Cannot disambiguate Drive source {file_id!r}: probe found "
                            f"{len(matches)} new sources with this documentId after a "
                            "transport failure. Check the notebook source list before retrying."
                        ),
                    )
                )
            return None

        result = await idempotent_create(
            _create,
            _probe,
            may_have_committed=transport_may_have_committed,
            label=f"sources.add_drive[{file_id}]",
        )
        source = result.value

        if wait:
            source = await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)
            result = replace(result, value=source)

        return result if return_result else source


__all__ = ["SourceAddService"]
