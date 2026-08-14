"""Private non-file source creation service."""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any
from urllib.parse import parse_qs

from .._idempotency import (
    _CreateResultKind,
    _IdempotentCreateResult,
    idempotent_create,
)
from .._idempotency import (
    mark_unconfirmed as _unconfirmed,
)
from .._runtime.contracts import RpcCaller
from ..exceptions import (
    AuthError,
    NetworkError,
    NonIdempotentRetryError,
    RateLimitError,
    ServerError,
    SourceAddError,
    ValidationError,
)
from ..rpc import RPCError, RPCMethod
from ..types import Source
from .upload_payloads import build_template_block

ListSources = Callable[[str], Awaitable[list[Source]]]
WaitUntilReady = Callable[..., Awaitable[Source]]
RawSourceAdder = Callable[[str, str], Awaitable[Any]]
RenameSource = Callable[[str, str, str], Awaitable[Source | None]]
ParseUrl = Callable[[str], Any]
ExtractVideoId = Callable[[Any, str], str | None]
ValidateVideoId = Callable[[str], bool]
YoutubeDetector = Callable[[str], bool]


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
    """URL, YouTube, text, and Drive source creation behavior."""

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        add_youtube_source: RawSourceAdder,
        add_url_source: RawSourceAdder,
        list_sources: ListSources,
        wait_until_ready: WaitUntilReady,
        extract_youtube_video_id: Callable[[str], str | None],
        is_youtube_url: YoutubeDetector,
        logger: logging.Logger,
        return_result: bool = False,
    ) -> Source | _IdempotentCreateResult[Source]:
        """Add a URL source to a notebook.

        Runs the probe-then-create idempotency pattern: the create is issued
        with internal retries disabled, and a 5xx / network failure that may
        have committed server-side is followed by a probe for the committed
        source before any retry. The probe matches ``source.url == url``.

        A URL is **not** unique within a notebook — the backend happily holds
        the same URL twice (live-verified on #2204: two adds of
        ``https://example.com/`` produced two distinct source ids), and
        ``SourceLister.list`` dedupes by source id, not by URL. So a bare
        URL match cannot tell *"the create I just issued landed"* from *"a
        source with this URL was already here"*. Probe matches are therefore
        filtered against a baseline of source ids captured **before** the
        first create attempt, exactly like
        :meth:`~notebooklm._source.upload.SourceUploader.register_file_source`
        does for filenames and ``add_drive`` does for Drive ``documentId``\\ s.
        An unavailable baseline or an ambiguous multi-match raises
        :class:`~notebooklm.exceptions.SourceAddError` rather than guessing.

        .. note::
           **A probe that cannot answer aborts the add (#2220).** If the
           probe's own ``list()`` fails for a non-transport reason — a drifted
           ``GET_NOTEBOOK`` making the strict decoder raise ``RPCError`` is the
           realistic case — this raises :class:`~notebooklm.exceptions.SourceAddError`
           instead of reporting "no match" and letting the create be re-issued.

           The probe returns ``None`` only when it has affirmatively
           established that no matching source exists; ``None`` is read by
           ``idempotent_create`` as evidence the create did not land, and acted
           on by repeating it. A broken probe is not that evidence. Retrying
           anyway would silently turn a ``PROBE_THEN_CREATE`` operation into an
           at-least-once one at the exact moment its guarantee matters — and
           this codebase makes at-least-once an explicit, named opt-in
           (:attr:`~notebooklm._idempotency.IdempotencyPolicy.AT_LEAST_ONCE_ACCEPTED`).

           The cost is real and was weighed: a decode blip on a create that
           never landed now surfaces as a hard failure the caller must retry by
           hand, where before it recovered silently. That is accepted because
           the two outcomes are not symmetric in *detectability*. A caller who
           is told "unresolved, go look" can act; a caller handed a silent
           duplicate — or the wrong source id — has nothing to act on and no
           reason to look. The baseline read, by contrast, still degrades
           rather than failing: it runs before anything is written, so
           proceeding is safe there.

        .. note::
           **This is a behaviour change (#2204).** ``add_url`` used to list
           sources only inside ``_probe``, which ``idempotent_create`` runs
           only after a transport failure; it now takes that snapshot on
           *every* call, matching the shape ``add_file`` has always had and
           ``add_drive`` adopted in #2113.

           The concrete cost: that list is a ``GET_NOTEBOOK``, and the backend
           **writes** ``lastViewedTime`` when answering one (#2126), so every
           ``add_url`` now promotes the notebook to the top of the user's
           *Recent* list in the web UI. ``ADD_SOURCE`` alone does not — the
           #2204 live probe held ``last_viewed_at`` pinned at ``1786617745``
           across an ``add_url`` — so this is a genuinely new side effect on
           the highest-traffic add path, not a pre-existing one. It is
           accepted because the alternative is what the same probe run
           demonstrated: a create that *did* land as ``df618843-…`` returned
           the pre-existing ``0d2c15a1-…`` instead, so the caller held the
           wrong source id **and** an unreported duplicate.

           No cheaper *id-based* probe exists: source ids are published only
           inside the ``GET_NOTEBOOK`` payload, and ``LIST_NOTEBOOKS`` (which
           does not bump) does not carry them. Two non-id alternatives were
           weighed and rejected. A lazily captured baseline is not merely
           cheaper but wrong — the probe runs *after* the create, so a list
           taken there already contains the just-created source. Filtering by
           :attr:`~notebooklm.types.Source.created_at` would need no pre-create
           read at all, but it compares a second-resolution *server* timestamp
           against a client clock of unknown skew, and it fails precisely in
           the bulk-import case this path serves, where the pre-existing copy
           was added seconds earlier.

           A caller looping over this single-item method keeps the notebook at
           the top of *Recent* when no other activity intervenes, but every
           baseline read still writes ``lastViewedTime``. Any ``wait=True``
           caller also pays the polling reads. The existing MCP/REST URL-batch
           endpoints deliberately bypass this method and use one true-batch
           write plus, when needed, one reconciliation read (#2115). ``add_text`` is
           ``NON_IDEMPOTENT_NO_RETRY``, runs no probe, and is unaffected.

        .. warning::
           The baseline establishes *when* a matching source appeared, not
           *who* created it. If two callers add the same URL to one notebook
           concurrently and one create fails before committing, the failed
           caller's probe can attribute the other caller's source to itself
           (or see two new matches and raise). Because the recovered result is
           treated as fresh, a requested ``title`` would then be applied to the
           other caller's source — a visible mutation, not merely a
           misattributed id. A list-based probe cannot close that gap — the
           wire carries no client-supplied idempotency key — so serialize
           concurrent adds of the same URL into a notebook if you need that
           guarantee. The same limitation applies to ``add_drive`` and
           ``register_file_source``.
        """
        logger.debug("Adding URL source to notebook %s: %s", notebook_id, url[:80])
        video_id = extract_youtube_video_id(url)
        if not video_id and is_youtube_url(url):
            logger.warning(
                "URL appears to be YouTube but no video ID found: %s. "
                "Adding as web page - content may be incomplete. "
                "If this is a video URL, please report this as a bug.",
                url[:100],
            )

        async def _create() -> Source:
            # Preserve transport-level signals so callers can act on the
            # specific type (AuthError -> re-login, RateLimitError -> back-off
            # with retry_after, ServerError -> transient retry). RateLimitError,
            # ServerError, and NetworkError must propagate so idempotent_create
            # can catch them and run the probe. AuthError continues to
            # propagate to the caller because an auth failure cannot have
            # committed the write.
            try:
                if video_id:
                    result = await add_youtube_source(notebook_id, url)
                else:
                    result = await add_url_source(notebook_id, url)
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise SourceAddError(url, cause=e) from e

            if result is None:
                raise SourceAddError(url, message=f"API returned no data for URL: {url}")
            return Source.from_api_response(result, method_id=RPCMethod.ADD_SOURCE.value)

        # Capture baseline source ids before the first create attempt so the
        # probe can tell "this URL add landed" from "the same URL was already
        # in the notebook". A URL is NOT unique within a notebook — live
        # capture (#2204) added ``https://example.com/`` twice and got two
        # distinct source ids — so an unfiltered match could hand back a
        # pre-existing copy as if it were the one just created, masking a
        # failed create. ``None`` is the "baseline unavailable" sentinel; the
        # probe then refuses to guess. Mirrors ``add_drive`` below and
        # ``register_file_source`` in ``_source/upload.py``.
        #
        # NEW on every call (it used to list only inside _probe, i.e. only
        # after a transport failure). This list is a GET_NOTEBOOK, which the
        # backend answers by WRITING lastViewedTime (#2126) — so every add_url
        # now reshuffles the user's Recent ordering. Unavoidable (source ids
        # live only in that payload; LIST_NOTEBOOKS does not bump but does not
        # carry them) and accepted; see the ``.. note::`` on this method.
        baseline_ids: set[str] | None
        # Retained so the ambiguity raise below can name what went wrong: the
        # caller sees "baseline unavailable" long after this line ran, and
        # without the cause there is nothing in the process that can explain it.
        baseline_error: Exception | None = None
        try:
            baseline_ids = {source.id for source in await list_sources(notebook_id)}
        except Exception as exc:
            # WARNING, not DEBUG: the default logger level is WARNING
            # (``_logging.py``), so a DEBUG record here is discarded before any
            # handler sees it and this call would silently run with the #2204
            # protection disabled. Louder than the probe's own swallow is
            # justified — a failed probe costs one extra create attempt, while a
            # failed baseline disables the probe for the whole call AND turns a
            # recoverable transport error into a hard ambiguity error below.
            baseline_error = exc
            logger.warning(
                "add_url: baseline list() failed (%s); the idempotency probe can no "
                "longer tell a source this call created from one that was already "
                "there, so a transport failure will surface as an ambiguity error "
                "instead of recovering",
                type(exc).__name__,
                exc_info=True,
            )
            baseline_ids = None

        async def _probe() -> Source | None:
            try:
                sources = await list_sources(notebook_id)
            except (AuthError, RateLimitError, ServerError, NetworkError) as exc:
                # Transport- and auth-level probe failures must propagate.
                # Silently returning None here lets ``idempotent_create``
                # re-issue the create on top of a broken probe, which is
                # exactly the duplicate-source bug we are guarding against.
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
                # Propagate, do not retry (#2220). A decode failure here (e.g.
                # the strict ``RPCError`` GET_NOTEBOOK raises on a drifted
                # response) leaves the probe unable to answer, and its answer is
                # the only thing that makes the retry safe: this variant runs
                # with ``disable_internal_retries=True`` precisely because a
                # blind re-POST of ``ADD_SOURCE`` can duplicate. Returning
                # ``None`` here would claim "the create did not land" on no
                # evidence and re-issue it — silently downgrading a
                # ``PROBE_THEN_CREATE`` operation to at-least-once, which this
                # codebase's own taxonomy (``AT_LEAST_ONCE_ACCEPTED``) requires
                # the *caller* to opt into. Raising hands the caller the one
                # thing they can act on: this add is unresolved, go look.
                logger.warning(
                    "add_url: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    type(exc).__name__,
                    exc_info=True,
                )
                raise _unconfirmed(
                    SourceAddError(
                        url,
                        cause=exc,
                        message=(
                            # Front-loaded on purpose: the MCP and REST surfaces
                            # truncate messages at 300 characters, which cut the
                            # closing instruction mid-word on a realistic URL.
                            # The action comes first; the narrative can be lost.
                            "UNRESOLVED — do not blindly retry; check the notebook "
                            f"source list first. Cannot confirm URL source {url!r}: "
                            "the create failed at the transport level and may or may "
                            "not have committed, and the idempotency probe that would "
                            f"settle it failed too ({type(exc).__name__}). No FURTHER attempt "
                            "was made, because retrying on an unanswered probe is how "
                            "duplicates happen — but an earlier attempt in this call "
                            "may also have committed."
                        ),
                    )
                ) from exc
            matches = [source for source in sources if source.url == url]
            if baseline_ids is not None:
                matches = [source for source in matches if source.id not in baseline_ids]
            elif matches:
                # Without a baseline a match may predate this add — see the
                # ``baseline_ids`` comment for the failure mode this guards.
                # Both halves of the ambiguity are worth stating: the match may
                # predate the add, or it may BE the add, in which case the create
                # landed and the caller will otherwise never learn its id.
                raise _unconfirmed(
                    SourceAddError(
                        url,
                        cause=baseline_error,
                        message=(
                            # Action first: MCP and REST truncate at 300 chars,
                            # while the URL + matched-row description are unbounded.
                            "UNRESOLVED — check the notebook source list before retrying. "
                            f"Cannot disambiguate URL source {url!r}: the pre-create baseline "
                            f"snapshot failed ({type(baseline_error).__name__}), so "
                            f"{_describe_sources(matches)} may either predate this add or be "
                            "the source it just created."
                        ),
                    )
                )
            if len(matches) == 1:
                (match,) = matches  # exactly one (len==1 guard); unpack, not matches[0]
                return match
            if len(matches) > 1:
                raise _unconfirmed(
                    SourceAddError(
                        url,
                        message=(
                            # _describe_sources grows with every match; keep the
                            # manual-reconciliation instruction inside [:300].
                            "UNRESOLVED — check the notebook source list before retrying. "
                            f"Cannot disambiguate URL source {url!r}: probe found "
                            f"{len(matches)} new sources with this URL after a transport "
                            f"failure ({_describe_sources(matches)})."
                        ),
                    )
                )
            return None

        result = await idempotent_create(
            _create,
            _probe,
            label=f"sources.add_url[{url[:40]}]",
        )
        source = result.value

        if wait:
            source = await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)
            result = replace(result, value=source)

        return result if return_result else source

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        idempotent: bool = False,
        rpc: RpcCaller,
        wait_until_ready: WaitUntilReady,
        logger: logging.Logger,
    ) -> Source:
        """Add a text source to a notebook."""
        if idempotent:
            raise NonIdempotentRetryError(
                "add_text cannot be marked idempotent: text sources have no "
                "reliable server-side dedupe key (titles non-unique, content "
                "not exposed). For idempotent text imports, embed a UUID in "
                "the title and dedupe client-side. See "
                "docs/python-api.md#idempotency."
            )
        logger.debug("Adding text source to notebook %s: %s", notebook_id, title)
        # Nested template block per the Gemini-3.5 wire migration (#1546): the
        # text spec grew from 8 to 11 elements (slot 3 None -> 2, trailing 1) and
        # the flat [2],None,None tail collapsed into the shared template block.
        # The literal 2 at slot 3 is a source-type code taken verbatim from the
        # web-UI capture; its exact meaning is undocumented. Verified live
        # against an un-migrated account.
        params = [
            [[None, [title, content], None, 2, None, None, None, None, None, None, 1]],
            notebook_id,
            build_template_block(),
        ]
        try:
            result = await rpc.rpc_call(
                RPCMethod.ADD_SOURCE,
                params,
                source_path=f"/notebook/{notebook_id}",
                operation_variant="text",
            )
        except (AuthError, RateLimitError, ServerError, NetworkError):
            # Preserve transport-level signals so callers can act on the
            # specific type (AuthError -> re-login, RateLimitError -> back-off
            # with retry_after, ServerError -> transient retry) instead of
            # receiving everything collapsed into SourceAddError — the same
            # ADR-0019 catch ordering add_url and add_drive use.
            raise
        except RPCError as e:
            raise SourceAddError(
                title,
                cause=e,
                message=f"Failed to add text source '{title}'",
            ) from e

        if result is None:
            raise SourceAddError(title, message=f"API returned no data for text source: {title}")

        source = Source.from_api_response(result, method_id=RPCMethod.ADD_SOURCE.value)

        if wait:
            return await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)

        return source

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        *,
        mime_type: str = "application/vnd.google-apps.document",
        wait: bool = False,
        wait_timeout: float = 120.0,
        rpc: RpcCaller,
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
        source_data = [
            [file_id, mime_type, 1, title],
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            1,
        ]
        # TODO(#1546): Drive add is NOT yet migrated to the nested template
        # block — no live Drive capture/probe yet, so it stays on the old
        # [2], [1,...,[1]] tail. Migrate via build_template_block() once a Drive
        # add is captured from the web UI and verified against a live account.
        params = [
            [source_data],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]

        async def _create() -> Source:
            # Preserve transport-level signals so callers can act on the
            # specific type (AuthError -> re-login, RateLimitError -> back-off,
            # ServerError -> transient retry). The retryable transport
            # exceptions must propagate so idempotent_create can catch them
            # and run the probe.
            try:
                result = await rpc.rpc_call(
                    RPCMethod.ADD_SOURCE,
                    params,
                    source_path=f"/notebook/{notebook_id}",
                    allow_null=True,
                    disable_internal_retries=True,
                    operation_variant="drive",
                )
            except (AuthError, RateLimitError, ServerError, NetworkError):
                raise
            except RPCError as e:
                raise SourceAddError(title, cause=e) from e

            if result is None:
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
            return Source.from_api_response(result, method_id=RPCMethod.ADD_SOURCE.value)

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
            label=f"sources.add_drive[{file_id}]",
        )
        source = result.value

        if wait:
            source = await wait_until_ready(notebook_id, source.id, timeout=wait_timeout)
            result = replace(result, value=source)

        return result if return_result else source

    def extract_youtube_video_id(
        self,
        url: str,
        *,
        parse_url: ParseUrl,
        extract_video_id_from_parsed_url: ExtractVideoId,
        is_valid_video_id: ValidateVideoId,
        logger: logging.Logger,
    ) -> str | None:
        """Extract a YouTube video ID from supported URL formats."""
        try:
            parsed = parse_url(url.strip())
            hostname = (parsed.hostname or "").lower()

            youtube_domains = {
                "youtube.com",
                "www.youtube.com",
                "m.youtube.com",
                "music.youtube.com",
                "youtu.be",
            }

            if hostname not in youtube_domains:
                return None

            video_id = extract_video_id_from_parsed_url(parsed, hostname)

            if video_id and is_valid_video_id(video_id):
                return video_id

            return None

        except (AttributeError, TypeError, ValueError) as e:
            logger.debug("Failed to parse YouTube URL '%s': %s", url[:100], e)
            return None

    def extract_video_id_from_parsed_url(self, parsed: Any, hostname: str) -> str | None:
        """Extract the raw YouTube video ID from a parsed URL."""
        if hostname == "youtu.be":
            path = parsed.path.lstrip("/")
            if path:
                return path.split("/")[0].strip()
            return None

        path_prefixes = ("shorts", "embed", "live", "v")
        path_segments = parsed.path.lstrip("/").split("/")

        # Unpack instead of indexing ``path_segments[0]`` / ``[1]``: these are
        # URL path segments, not an RPC payload, but the positional-RPC ratchet
        # is type-blind, so the unpack keeps the benign string parse off the
        # flagged ``name[int]`` shape (semantics identical to the prior
        # ``len(...) >= 2`` + index reads).
        if len(path_segments) >= 2:
            prefix, segment, *_rest = path_segments
            if prefix.lower() in path_prefixes:
                return segment.strip()

        if parsed.query:
            query_params = parse_qs(parsed.query)
            v_param = query_params.get("v", [])
            # ``next(iter(...))`` instead of ``v_param[0]`` for the same
            # type-blind-ratchet reason; ``v_param`` is the parse_qs value list.
            first_v = next(iter(v_param), None)
            if first_v:
                return first_v.strip()

        return None

    def is_valid_video_id(self, video_id: str) -> bool:
        """Validate YouTube video ID format."""
        return bool(video_id and re.match(r"^[a-zA-Z0-9_-]+$", video_id))

    async def add_youtube_source(
        self,
        notebook_id: str,
        url: str,
        *,
        rpc: RpcCaller,
    ) -> Any:
        """Add a YouTube video as a source.

        The source entry is unchanged, but the flat ``[2], [1,...,[1]]`` tail
        (4 outer elements) collapsed into the single nested
        ``[2, None, None, [1, ..., [1]]]`` block (#1546). Verified live against
        an un-migrated account.
        """
        params = [
            [[None, None, None, None, None, None, None, [url], None, None, 1]],
            notebook_id,
            build_template_block(),
        ]
        return await rpc.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=False,
            disable_internal_retries=True,
            operation_variant="url",
        )

    async def add_url_source(
        self,
        notebook_id: str,
        url: str,
        *,
        rpc: RpcCaller,
    ) -> Any:
        """Add a regular URL as a source.

        The source spec gained a trailing ``1`` and the flat ``[2], None, None``
        tail collapsed into the nested ``[2, None, None, [1, ..., [1]]]`` block
        that NotebookLM's web UI now sends; migrated backends reject the old
        shape (``status=5``/``9``). Verified live against an un-migrated account.
        See https://github.com/teng-lin/notebooklm-py/issues/1546.
        """
        params = [
            [[None, None, [url], None, None, None, None, None, None, None, 1]],
            notebook_id,
            build_template_block(),
        ]
        return await rpc.rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            disable_internal_retries=True,
            operation_variant="url",
        )


__all__ = ["SourceAddService"]
