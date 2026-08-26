"""Transport-neutral semantic service for the migrated P6.7 Source slice."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

from ._backend import (
    BackendAdapter,
    BackendContractError,
    BackendDeadlineExceededError,
    BackendError,
    BackendErrorReason,
    annotate_backend_error,
    mark_backend_outcome_unknown,
    rebind_operation,
    require_leaves,
)
from ._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from ._idempotency_create import (
    _CreateResultKind,
    idempotent_create,
    semantic_may_have_committed,
)
from ._operations import Operation
from ._records import (
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_ADD_TEXT_DEF,
    SOURCE_ADD_URL_BATCH_DEF,
    SOURCE_ADD_URL_DEF,
    SOURCE_CHECK_FRESHNESS_DEF,
    SOURCE_DELETE_DEF,
    SOURCE_GET_DEF,
    SOURCE_GET_FULLTEXT_DEF,
    SOURCE_GET_GUIDE_DEF,
    SOURCE_LIST_DEF,
    SOURCE_PATCH_TITLE_DEF,
    SOURCE_REFRESH_DEF,
    SOURCE_REGISTER_DEF,
    SOURCE_UPDATE_DEF,
    SOURCE_WAIT_DEF,
    SourceAddCommitState,
    SourceAddDriveInput,
    SourceAddDriveResult,
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceAddTextResult,
    SourceAddTitleState,
    SourceAddUrlBatchInput,
    SourceAddUrlBatchResult,
    SourceAddUrlReceipt,
    SourceAddUrlResult,
    SourceDeleteInput,
    SourceFileInputKind,
    SourceFreshnessInput,
    SourceFulltextInput,
    SourceFulltextResult,
    SourceGetInput,
    SourceGuideInput,
    SourceGuideResult,
    SourceListInput,
    SourceListResult,
    SourcePatchTitleInput,
    SourceProgressCallback,
    SourceRecord,
    SourceRefreshInput,
    SourceRegisterInput,
    SourceRegisterKind,
    SourceUpdateResult,
    SourceWaitSnapshotInput,
    SourceWaitSnapshotResult,
)
from ._url_utils import extract_youtube_video_id, is_youtube_url

# The pre-P10 ``source.add_text`` row reached this message through
# ``SourceAddService``; the workflow that replaces the row owns it verbatim so
# the public ``NonIdempotentRetryError`` text is unchanged.
_TEXT_NON_IDEMPOTENT_MESSAGE = (
    "add_text cannot be marked idempotent: text sources have no "
    "reliable server-side dedupe key (titles non-unique, content "
    "not exposed). For idempotent text imports, embed a UUID in "
    "the title and dedupe client-side. See "
    "docs/python-api.md#idempotency."
)

#: Failure kinds the pre-P10 registration handlers' ``except RPCError`` wrapped
#: into a ``SourceAddError``. It is deliberately *not* "every RPC-shaped
#: reason": ``AuthError``/``RateLimitError``/``ServerError``/``NetworkError``
#: (and ``RPCTimeoutError``, a ``NetworkError`` subclass) were caught first and
#: re-raised unwrapped under ADR-0019, so callers can still act on the specific
#: type. Anything outside this set keeps the leaf's own public identity.
#:
#: ``add_text`` and ``add_url`` shared this catch ordering verbatim below the
#: port, so the hoisted workflows share one definition of it rather than each
#: re-deriving which leaves survive as themselves.
_WRAPPED_REGISTRATION_FAILURE_KINDS = frozenset(
    {
        SourceAddFailureKind.RPC,
        SourceAddFailureKind.CLIENT,
        SourceAddFailureKind.DECODING,
        SourceAddFailureKind.RESPONSE_TOO_LARGE,
        SourceAddFailureKind.UNKNOWN_RPC_METHOD,
    }
)

#: ``SourceAddError``'s own default message, owned verbatim so the hoisted URL
#: workflow reports the text the retired row's ``SourceAddError(url)`` did
#: without naming a public exception type above the port.
_URL_ADD_FAILURE_MESSAGE = (
    "Failed to add source: {url}\n"
    "Possible causes:\n"
    "  - URL is invalid or inaccessible\n"
    "  - Content is behind a paywall or requires authentication\n"
    "  - Page content is empty or could not be parsed\n"
    "  - Rate limiting or quota exceeded"
)

#: Neutral reasons the pre-P10 probe re-raised *unwrapped* after marking the
#: outcome unknown: exactly the ``(AuthError, RateLimitError, ServerError,
#: NetworkError)`` tuple its ``except`` named, plus ``TIMEOUT`` because
#: ``RPCTimeoutError`` is a ``NetworkError`` subclass. Anything else means the
#: probe could not answer for a non-transport reason and becomes the UNRESOLVED
#: ``SourceAddError``.
_DIRECT_PROBE_REASONS = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.NETWORK,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.SERVER,
        BackendErrorReason.TIMEOUT,
    }
)

#: Neutral reasons whose replayed public exception is an ``RPCError`` or a
#: ``NetworkError`` — exactly the two families ``honor_requested_title``'s
#: ``except (RPCError, NetworkError)`` swallowed below the port. The post-create
#: rename is non-fatal by contract: the add already succeeded, so a rename
#: failure keeps the added source and logs a warning (#1960). Every other
#: reason still aborts, so a genuinely new failure mode cannot be silently
#: absorbed by the title phase.
_RENAME_SWALLOWED_REASONS = frozenset(
    {
        BackendErrorReason.AUTH,
        BackendErrorReason.CLIENT,
        BackendErrorReason.DECODING,
        BackendErrorReason.NETWORK,
        BackendErrorReason.NOTEBOOK_NOT_FOUND,
        BackendErrorReason.RATE_LIMIT,
        BackendErrorReason.RESPONSE_TOO_LARGE,
        BackendErrorReason.RPC,
        BackendErrorReason.SERVER,
        BackendErrorReason.SOURCE_NOT_FOUND,
        BackendErrorReason.TIMEOUT,
        BackendErrorReason.UNKNOWN_RPC_METHOD,
    }
)

#: Diagnostics key ``_backend_compat`` reads to restore the implicit context a
#: probe inherits from the create it was run to reconcile. ``_capture_public_
#: failure`` deliberately refuses to descend into a private ``BackendError``
#: context, so a sequencing workflow carries that earlier public failure itself.
_CREATE_CONTEXT_FAILURE = "create_context_failure"

# The same logger name and level the retired row logged under.
_source_logger = logging.getLogger("notebooklm").getChild("_sources")


def _source_add_failure(
    operation: Operation,
    record: SourceAddFailureRecord,
    *,
    outcome_unknown: bool = False,
    dispatched: bool = False,
) -> BackendError:
    """Report one source-add failure as bounded neutral evidence.

    ``_backend_compat`` replays an *equal* public exception at the facade from
    ``record`` alone, so a transport-neutral workflow never has to name — or
    construct — a public exception type.
    """
    return BackendError(
        message=record.message,
        operation=operation,
        outcome_unknown=outcome_unknown,
        diagnostics=MappingProxyType({"source_add_failure": record}),
        reason=BackendErrorReason.SOURCE_ADD,
        dispatched=dispatched,
    )


def _leaf_failure_record(error: BackendError) -> SourceAddFailureRecord | None:
    """Return the leaf's captured public graph, if the backend captured one.

    Capturing it is a *web* convention, not a port requirement: another adapter
    may report a closed reason and nothing else, and the compatibility projector
    reconstructs a public exception from the reason alone in that case. ``None``
    therefore means "project by reason", not "malformed". A value of the wrong
    type is malformed, and fails closed.
    """
    record = (error.diagnostics or {}).get("public_error_failure")
    if record is None:
        return None
    if not isinstance(record, SourceAddFailureRecord):
        raise BackendContractError(
            "source registration failure has invalid public-error evidence",
            operation=error.operation,
        ) from error
    return record


def _degraded_failure_record(error: BaseException) -> SourceAddFailureRecord | None:
    """The captured graph of a failure the workflow deliberately continued past.

    Unlike :func:`_leaf_failure_record` this never fails closed. The pre-create
    baseline read runs before anything is written, so proceeding without it is
    safe and it degrades rather than aborting; escalating malformed evidence
    there would convert the one read that is allowed to fail into a hard one.
    """
    if not isinstance(error, BackendError):
        return None
    record = (error.diagnostics or {}).get("public_error_failure")
    return record if isinstance(record, SourceAddFailureRecord) else None


def _failure_type_name(error: BaseException) -> str:
    """The public exception class name a neutral failure was translated from.

    The ambiguity and UNRESOLVED messages name the failure the caller would
    otherwise have seen; a web adapter raises its ``BackendError`` *from* that
    public leaf, so the cause carries the name the pre-P10 messages printed.
    """
    cause = error.__cause__
    return type(cause).__name__ if cause is not None else type(error).__name__


def _describe_sources(sources: Sequence[SourceRecord]) -> str:
    """Render matched sources as ``id (title)`` for an ambiguity message.

    The ambiguity raises tell the caller to go check the notebook's source
    list; naming the exact rows saves them diffing a list by eye against a URL
    that, by definition, appears in it more than once.
    """
    return ", ".join(f"{source.id} ({source.title!r})" for source in sources)


class SourceService:
    """Invoke typed Source operations without web/RPC/public-model vocabulary."""

    __slots__ = ("_backend", "_deadline_factory")

    def __init__(
        self,
        backend: BackendAdapter,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._backend = backend
        # P9.2 contract 3: a service-owned workflow mints one deadline
        # before its first leaf and threads that identity through every phase.
        self._deadline_factory = deadline_factory

    async def add_url(
        self,
        notebook_id: str,
        url: str,
        *,
        wait: bool = False,
        wait_timeout: float = 120.0,
        requested_title: str | None = None,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddUrlResult:
        """Register one URL — generic or YouTube — behind a probe-then-create.

        The workflow the P9.4b ``source.add_url`` row owned, sequenced over
        typed leaves instead: one ``source.list`` baseline, one
        ``source.register`` URL allocation, a reconciling ``source.list`` probe
        when that allocation may have committed, and a best-effort
        ``source.patch_title`` finalise.

        A URL is **not** unique within a notebook — the backend happily holds
        the same URL twice (live-verified on #2204) — so a bare URL match
        cannot tell *"the create I just issued landed"* from *"a source with
        this URL was already here"*. Probe matches are therefore filtered
        against a baseline of source ids captured **before** the first create
        attempt. An unavailable baseline or an ambiguous multi-match is
        reported as an unresolved failure rather than guessed at.

        .. note::
           **A probe that cannot answer aborts the add (#2220).** The probe
           returns ``None`` only when it has affirmatively established that no
           matching source exists; ``None`` is read by ``idempotent_create`` as
           evidence the create did not land, and acted on by repeating it. A
           broken probe is not that evidence, and retrying anyway would
           silently turn a ``PROBE_THEN_CREATE`` operation into an
           at-least-once one at the exact moment its guarantee matters.

        .. note::
           The baseline snapshot is taken on *every* call and is a
           ``GET_NOTEBOOK`` on the web, which the backend answers by **writing**
           ``lastViewedTime`` (#2126) — so every ``add_url`` promotes the
           notebook in the user's *Recent* list. Accepted, and unchanged by the
           hoist: no cheaper id-based probe exists, and ``source.get`` cannot
           substitute — both the pre-create baseline set and the post-failure
           reconcile need the *whole* id set, which a single-id result cannot
           express.

        .. warning::
           The baseline establishes *when* a matching source appeared, not
           *who* created it. If two callers add the same URL to one notebook
           concurrently and one create fails before committing, the failed
           caller's probe can attribute the other caller's source to itself
           (or see two new matches and report ambiguity). Because a recovered
           result is treated as fresh, a requested ``title`` would then be
           applied to the other caller's source. Serialize concurrent adds of
           the same URL into a notebook if you need that guarantee.

        ``wait``/``wait_timeout`` are accepted for signature stability and are
        the public facade's: readiness polling never crossed the port, and the
        retired row failed closed on any attempt to take it below one.
        """
        del wait_timeout  # readiness polling is the facade's; see the docstring.
        workflow = SOURCE_ADD_URL_DEF.key
        # The title leaves are checked up front even when no title was
        # requested, exactly as ``update`` checks its conditional hydration
        # leaf: a backend must never register the source and only then discover
        # it cannot run the finalise the contract promises.
        require_leaves(
            self._backend,
            SOURCE_LIST_DEF.key,
            SOURCE_REGISTER_DEF.key,
            SOURCE_PATCH_TITLE_DEF.key,
            SOURCE_GET_DEF.key,
        )
        # One absolute budget for every phase. The retired row ran under the
        # deadline ``WebRpcBackend`` seeded for the whole ``CLIENT_TIMEOUT``
        # operation; the workflow is that operation's owner now, so it mints
        # the identity and threads it through baseline, create, probe and
        # rename unchanged.
        deadline = self._start_deadline(deadline)

        _source_logger.debug("Adding URL source to notebook %s: %s", notebook_id, url[:80])
        video_id = extract_youtube_video_id(url, logger=_source_logger)
        if not video_id and is_youtube_url(url):
            _source_logger.warning(
                "URL appears to be YouTube but no video ID found: %s. "
                "Adding as web page - content may be incomplete. "
                "If this is a video URL, please report this as a bug.",
                url[:100],
            )

        baseline_ids, baseline_failure, baseline_error_name = await self._url_baseline(
            notebook_id,
            deadline=deadline,
        )
        last_create_error: BackendError | None = None

        async def register() -> SourceRecord:
            nonlocal last_create_error
            try:
                registered = await self._backend.invoke(
                    SOURCE_REGISTER_DEF,
                    SourceRegisterInput(
                        notebook_id,
                        SourceRegisterKind.URL,
                        urls=(url,),
                        youtube_flags=(bool(video_id),),
                    ),
                    deadline=deadline,
                )
            except BackendError as leaf_error:
                # Transport reasons keep their own public identity so
                # ``semantic_may_have_committed`` can still see the commit
                # uncertainty and run the probe; only the residual RPC family
                # is wrapped, exactly as the retired ``except RPCError`` did.
                error = self._url_registration_failure(workflow, url, leaf_error)
                last_create_error = error
                raise error from leaf_error.__cause__
            source = next(iter(registered.sources), None)
            if source is None:
                message = f"API returned no data for URL: {url}"
                raise _source_add_failure(
                    workflow,
                    SourceAddFailureRecord(
                        kind=SourceAddFailureKind.SOURCE_ADD,
                        message=message,
                        args=(message,),
                        url=url,
                    ),
                )
            return source

        async def probe() -> SourceRecord | None:
            create_error = last_create_error
            if create_error is None:
                raise BackendError(
                    "source.add_url reconciliation started without a registration failure",
                    operation=workflow,
                )
            try:
                current = await self._url_probe_snapshot(notebook_id, deadline=deadline)
            except BackendError as leaf_error:
                raise self._url_probe_failure(
                    workflow,
                    url,
                    leaf_error,
                    create_error,
                ) from leaf_error.__cause__
            except Exception as error:
                _source_logger.warning(
                    "add_url: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    type(error).__name__,
                    exc_info=True,
                )
                raise self._unresolved_url_error(
                    workflow,
                    url,
                    create_error,
                    probe_failure=None,
                    failure_name=type(error).__name__,
                ) from error

            matches = [source for source in current.sources if source.url == url]
            if baseline_ids is not None:
                matches = [source for source in matches if source.id not in baseline_ids]
            elif matches:
                # Without a baseline a match may predate this add. Both halves
                # of the ambiguity are worth stating: the match may predate the
                # add, or it may BE the add, in which case the create landed and
                # the caller will otherwise never learn its id.
                raise self._unconfirmed_url_error(
                    workflow,
                    url,
                    # Action first: MCP and REST truncate at 300 chars, while
                    # the URL + matched-row description are unbounded.
                    "UNRESOLVED — check the notebook source list before retrying. "
                    f"Cannot disambiguate URL source {url!r}: the pre-create baseline "
                    f"snapshot failed ({baseline_error_name}), so "
                    f"{_describe_sources(matches)} may either predate this add or be "
                    "the source it just created.",
                    create_error,
                    cause=baseline_failure,
                )
            if len(matches) == 1:
                (match,) = matches  # exactly one (len==1 guard); unpack, not matches[0]
                return match
            if len(matches) > 1:
                raise self._unconfirmed_url_error(
                    workflow,
                    url,
                    # ``_describe_sources`` grows with every match; keep the
                    # manual-reconciliation instruction inside [:300].
                    "UNRESOLVED — check the notebook source list before retrying. "
                    f"Cannot disambiguate URL source {url!r}: probe found "
                    f"{len(matches)} new sources with this URL after a transport "
                    f"failure ({_describe_sources(matches)}).",
                    create_error,
                )
            return None

        try:
            created = await idempotent_create(
                register,
                probe,
                may_have_committed=semantic_may_have_committed,
                label=f"sources.add_url[{url[:40]}]",
            )
        except BackendError as error:
            raise self._url_add_failure(workflow, error) from error.__cause__

        source_before_title = created.value
        normalized_title = requested_title.strip() if requested_title is not None else ""
        # A probed result is attributable to this call — the baseline diff is
        # what proves it — so the requested title is honored either way. Under
        # ``wait`` the facade renames after readiness instead (``finalize_title``).
        source = (
            source_before_title
            if wait
            else await self._honor_requested_title(
                notebook_id,
                source_before_title,
                requested_title,
                deadline=deadline,
            )
        )
        if not normalized_title:
            title_state = SourceAddTitleState.NOT_REQUESTED
        elif source_before_title.title == normalized_title:
            title_state = SourceAddTitleState.UNCHANGED
        elif wait:
            title_state = SourceAddTitleState.NOT_ATTEMPTED
        elif source.title == normalized_title:
            title_state = SourceAddTitleState.RENAMED
        else:
            title_state = SourceAddTitleState.RENAME_FAILED

        return SourceAddUrlResult(
            source=source,
            receipt=SourceAddUrlReceipt(
                commit_state=(
                    SourceAddCommitState.CREATED
                    if created.kind is _CreateResultKind.CREATED
                    else SourceAddCommitState.RECONCILED
                ),
                title_state=title_state,
            ),
        )

    async def finalize_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddUrlResult:
        """Apply a waited URL title under the original add operation."""
        require_leaves(self._backend, SOURCE_PATCH_TITLE_DEF.key, SOURCE_GET_DEF.key)
        deadline = self._start_deadline(None)
        renamed = await self._honor_requested_title(
            notebook_id,
            source,
            requested_title,
            deadline=deadline,
        )
        normalized_title = requested_title.strip()
        return SourceAddUrlResult(
            renamed,
            SourceAddUrlReceipt(
                SourceAddCommitState.CREATED,
                (
                    SourceAddTitleState.RENAMED
                    if normalized_title and renamed.title == normalized_title
                    else SourceAddTitleState.RENAME_FAILED
                ),
            ),
        )

    async def _url_probe_snapshot(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceListResult:
        """The reconciling snapshot, named apart from the pre-create baseline.

        Same leaf, same request — but the two reads answer different questions
        and fail differently (the baseline degrades, the probe aborts), so they
        are separable phases rather than one repeated call.
        """
        return await self._backend.invoke(
            SOURCE_LIST_DEF,
            SourceListInput(notebook_id),
            deadline=deadline,
        )

    async def _url_baseline(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None,
    ) -> tuple[set[str] | None, SourceAddFailureRecord | None, str | None]:
        """Snapshot source ids before the first create, or degrade to ``None``.

        ``None`` is the "baseline unavailable" sentinel; the probe then refuses
        to guess. The read runs before anything is written, so proceeding
        without it is safe — but the failure is retained so the ambiguity
        report can say what went wrong long after this line ran.
        """
        try:
            baseline = await self._backend.invoke(
                SOURCE_LIST_DEF,
                SourceListInput(notebook_id),
                deadline=deadline,
            )
        except Exception as error:
            # WARNING, not DEBUG: the default logger level is WARNING, so a
            # DEBUG record here is discarded before any handler sees it and this
            # call would silently run with the #2204 protection disabled. Louder
            # than the probe's own swallow is justified — a failed probe costs
            # one extra create attempt, while a failed baseline disables the
            # probe for the whole call AND turns a recoverable transport error
            # into a hard ambiguity error.
            _source_logger.warning(
                "add_url: baseline list() failed (%s); the idempotency probe can no "
                "longer tell a source this call created from one that was already "
                "there, so a transport failure will surface as an ambiguity error "
                "instead of recovering",
                _failure_type_name(error),
                exc_info=True,
            )
            return None, _degraded_failure_record(error), _failure_type_name(error)
        return {source.id for source in baseline.sources}, None, None

    @classmethod
    def _url_registration_failure(
        cls,
        workflow: Operation,
        url: str,
        error: BackendError,
    ) -> BackendError:
        """Rebind one URL registration failure, wrapping only the RPC leaf.

        The reason is deliberately preserved on everything this does not wrap:
        it is what ``semantic_may_have_committed`` reads to decide whether the
        write may have landed, and therefore whether the probe runs at all.
        """
        if isinstance(error, BackendContractError) or error.reason is None:
            return error
        if isinstance(error, BackendDeadlineExceededError):
            # An expiry keeps its subclass; a *dispatched* one is still
            # commit-uncertain, so re-attributing it leaves the probe reachable.
            return rebind_operation(error, workflow)
        leaf = _leaf_failure_record(error)
        if leaf is None or leaf.kind not in _WRAPPED_REGISTRATION_FAILURE_KINDS:
            # The transport four-tuple and every other public leaf keep their
            # own type, message and fields (ADR-0019 catch ordering); a backend
            # that captured no graph is re-attributed and projected by reason.
            return rebind_operation(error, workflow)
        message = _URL_ADD_FAILURE_MESSAGE.format(url=url)
        return _source_add_failure(
            workflow,
            SourceAddFailureRecord(
                kind=SourceAddFailureKind.SOURCE_ADD,
                message=message,
                args=(message,),
                url=url,
                cause=leaf,
                # ``raise SourceAddError(url, cause=e) from e`` inside ``except
                # RPCError as e``: the RPC error is the explicit cause, the
                # implicit context, and the reason the context is suppressed.
                context_is_cause=True,
                explicit_cause=True,
                suppress_context=True,
            ),
            outcome_unknown=error.outcome_unknown,
            dispatched=error.dispatched,
        )

    @classmethod
    def _url_probe_failure(
        cls,
        workflow: Operation,
        url: str,
        leaf_error: BackendError,
        create_error: BackendError,
    ) -> BackendError:
        """Report a probe that could not settle whether the create committed."""
        error = rebind_operation(leaf_error, workflow)
        if isinstance(leaf_error, BackendDeadlineExceededError):
            # An aggregate budget running out is the caller's own answer, not a
            # source-add failure: keep the subclass and only record that the
            # create's outcome is now unconfirmed.
            return mark_backend_outcome_unknown(cls._attach_create_context(error, create_error))
        if error.reason in _DIRECT_PROBE_REASONS:
            # Transport- and auth-level probe failures propagate with their own
            # public type, so "re-authenticate" / "connectivity" stay readable.
            # They are re-reported from the leaf's captured graph rather than
            # from the reason alone, because that graph carries fields the
            # closed reason cannot (``original_error``, and the ``source_id`` /
            # ``stage`` a partial-upload probe tags its failure with).
            #
            # They are marked unconfirmed first: the create may already have
            # committed and this probe could not say. Without the marker a
            # ServerError/RateLimitError here classifies as the *retriable*
            # SERVER/RATE_LIMITED with the hint "retry after a short delay" —
            # and the caller retries the ADD, not the probe.
            leaf = _leaf_failure_record(error)
            if leaf is None:
                return mark_backend_outcome_unknown(cls._attach_create_context(error, create_error))
            return cls._attach_create_context(
                _source_add_failure(
                    workflow,
                    leaf,
                    outcome_unknown=True,
                    dispatched=error.dispatched,
                ),
                create_error,
            )
        # Propagate, do not retry (#2220). A decode failure leaves the probe
        # unable to answer, and its answer is the only thing that makes the
        # retry safe. Returning "no match" here would claim "the create did not
        # land" on no evidence and re-issue it.
        _source_logger.warning(
            "add_url: probe list() failed with a non-transport error (%s); the "
            "create cannot be confirmed, so it will not be retried",
            _failure_type_name(leaf_error),
            exc_info=True,
        )
        return cls._unresolved_url_error(
            workflow,
            url,
            create_error,
            probe_failure=_leaf_failure_record(leaf_error),
            failure_name=_failure_type_name(leaf_error),
        )

    @classmethod
    def _unresolved_url_error(
        cls,
        workflow: Operation,
        url: str,
        create_error: BackendError,
        *,
        probe_failure: SourceAddFailureRecord | None,
        failure_name: str,
    ) -> BackendError:
        """The #2220 "probe could not answer" report, chain and all."""
        return cls._unconfirmed_url_error(
            workflow,
            url,
            # Front-loaded on purpose: the MCP and REST surfaces truncate
            # messages at 300 characters, which cut the closing instruction
            # mid-word on a realistic URL. The action comes first; the
            # narrative can be lost.
            "UNRESOLVED — do not blindly retry; check the notebook "
            f"source list first. Cannot confirm URL source {url!r}: "
            "the create failed at the transport level and may or may "
            "not have committed, and the idempotency probe that would "
            f"settle it failed too ({failure_name}). No FURTHER attempt "
            "was made, because retrying on an unanswered probe is how "
            "duplicates happen — but an earlier attempt in this call "
            "may also have committed.",
            create_error,
            cause=probe_failure,
            explicit=True,
        )

    @classmethod
    def _unconfirmed_url_error(
        cls,
        workflow: Operation,
        url: str,
        message: str,
        create_error: BackendError,
        *,
        cause: SourceAddFailureRecord | None = None,
        explicit: bool = False,
    ) -> BackendError:
        """One ``SOURCE_ADD`` report whose outcome is genuinely unknown.

        ``explicit`` distinguishes the two below-port raise forms the public
        graph still has to tell apart, and with them *where* the create belongs
        in the chain:

        * ``raise SourceAddError(...) from exc`` — the probe wrap. The probe
          failure is the report's explicit cause *and* its (suppressed)
          context, so the create it could not settle sits one level further
          down, as the probe's own implicit context.
        * a bare ``raise SourceAddError(...)`` — the two ambiguity reports. The
          cause is an *attribute* only, ``__cause__`` stays unset, and the
          create is the report's own implicit context.
        """
        create_failure = _degraded_failure_record(create_error)
        if explicit and cause is not None and create_failure is not None:
            cause = replace(cause, context=create_failure)
        error = _source_add_failure(
            workflow,
            SourceAddFailureRecord(
                kind=SourceAddFailureKind.SOURCE_ADD,
                message=message,
                args=(message,),
                url=url,
                cause=cause,
                context_is_cause=explicit,
                explicit_cause=explicit,
                suppress_context=explicit,
            ),
            outcome_unknown=True,
        )
        if explicit and cause is not None:
            return error
        return cls._attach_create_context(error, create_error)

    @staticmethod
    def _attach_create_context(
        error: BackendError,
        create_error: BackendError,
    ) -> BackendError:
        """Carry the create's public failure as the probe report's context.

        ``_capture_public_failure`` refuses to descend into a private
        ``BackendError`` context, so the implicit chain Python gives a probe
        run inside the create's ``except`` is not captured at the leaf. The
        sequencing workflow supplies it, and ``_backend_compat`` restores it.
        """
        failure = _degraded_failure_record(create_error)
        if failure is None:
            return error
        return annotate_backend_error(error, **{_CREATE_CONTEXT_FAILURE: failure})

    @classmethod
    def _url_add_failure(cls, workflow: Operation, error: BackendError) -> BackendError:
        """Report one failed add: the retired row's receipt, and its full graph.

        A create failure the workflow re-attributed rather than wrapped still
        carries only its closed *reason* at this point, because the reason is
        what ``semantic_may_have_committed`` reads to decide whether to probe.
        The probe has finished by the time this runs, so the leaf's captured
        public graph can be reported instead — which is how fields no reason
        can express (``source_id`` / ``stage`` on a tagged partial failure)
        reach the facade, exactly as they did when the retired row captured the
        escaping exception itself.
        """
        leaf = (
            None if isinstance(error, BackendDeadlineExceededError) else _leaf_failure_record(error)
        )
        if leaf is not None:
            reported = _source_add_failure(
                workflow,
                leaf,
                outcome_unknown=error.outcome_unknown,
                dispatched=error.dispatched,
            )
            leaf_operation = (error.diagnostics or {}).get("leaf_operation")
            if leaf_operation is not None:
                error = annotate_backend_error(reported, leaf_operation=leaf_operation)
            else:
                error = reported
        return annotate_backend_error(
            error,
            receipt=SourceAddUrlReceipt(
                commit_state=(
                    SourceAddCommitState.UNKNOWN
                    if error.outcome_unknown
                    else SourceAddCommitState.FAILED
                ),
                title_state=SourceAddTitleState.NOT_ATTEMPTED,
                outcome_unknown=error.outcome_unknown,
            ),
        )

    async def _honor_requested_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str | None,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceRecord:
        """Best-effort post-add rename so an explicit title survives re-derivation.

        YouTube, native Google Drive, and web-page imports re-derive the display
        title server-side, silently discarding the title sent with the add
        (#1960). The backend derives it *synchronously*, so a follow-up rename
        lands after that derivation and sticks.

        Non-fatal by contract: the add already succeeded, so a rename failure
        keeps the added source (with its upstream title) and logs a warning
        rather than raising — callers detect the miss by comparing the returned
        title against the one they requested.
        """
        if not requested_title:
            return source
        requested = requested_title.strip()
        if not requested or source.title == requested:
            return source
        try:
            renamed = await self._patch_title(
                notebook_id,
                source.id,
                requested,
                deadline=deadline,
            )
        except BackendError as error:
            if error.reason not in _RENAME_SWALLOWED_REASONS:
                raise
            _source_logger.warning(
                "Source %s added but rename to %r failed; keeping upstream title %r",
                source.id,
                requested,
                source.title,
                exc_info=True,
            )
            return source
        # ``source.patch_title``'s echo can be sparse (id + title only), so
        # returning it wholesale would drop url / kind / status. Keep the
        # fully-hydrated added source and swap in just the new title.
        return replace(source, title=(renamed.title if renamed else None) or requested)

    async def _patch_title(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        deadline: RuntimeDeadline | None,
    ) -> SourceRecord | None:
        """One title set-op, hydrating a null echo through ``source.get``."""
        patched = await self._backend.invoke(
            SOURCE_PATCH_TITLE_DEF,
            SourcePatchTitleInput(notebook_id, source_id, new_title),
            deadline=deadline,
        )
        if patched.source is not None:
            return patched.source
        hydrated = await self._backend.invoke(
            SOURCE_GET_DEF,
            SourceGetInput(notebook_id, source_id),
            deadline=deadline,
        )
        if hydrated.source is None:
            raise BackendError(
                message=f"Source not found: {source_id}",
                operation=SOURCE_ADD_URL_DEF.key,
                diagnostics=MappingProxyType({"source_id": source_id, "raw_response": None}),
                reason=BackendErrorReason.SOURCE_NOT_FOUND,
            )
        return hydrated.source

    def _start_deadline(self, deadline: RuntimeDeadline | None) -> RuntimeDeadline | None:
        """Mint the one workflow deadline unless the caller supplied its own."""
        if deadline is not None or self._deadline_factory is None:
            return deadline
        return self._deadline_factory.start()

    async def add_urls_batch(
        self,
        notebook_id: str,
        urls: tuple[str, ...],
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddUrlBatchResult:
        return await self._backend.invoke(
            SOURCE_ADD_URL_BATCH_DEF,
            SourceAddUrlBatchInput(notebook_id, urls),
            deadline=deadline,
        )

    async def add_text(
        self,
        notebook_id: str,
        title: str,
        content: str,
        *,
        wait: bool,
        wait_timeout: float,
        idempotent: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddTextResult:
        """Register one pasted-text source over the ``source.register`` leaf.

        Text is the source-add family's one registration with no probe: titles
        are not unique and the body is never echoed, so there is nothing to
        reconcile against and no baseline worth taking. The workflow is
        therefore the refusal, one write, and the two ways that write can fail
        to name a source — which is exactly what the retired row did.

        ``wait``/``wait_timeout`` stay on the signature because the input record
        carries them, but readiness polling is the public facade's (the row
        failed closed on any attempt to take it below the port).

        No deadline is minted from ``_deadline_factory``. ``source.add_text`` is
        deliberately absent from the closed deadline ledger
        (``_web/deadlines.py``): it is one write with no aggregate budget, and
        the retired row ran it with whatever the caller supplied — which the
        facade leaves ``None``. Starting one here would invent a budget, and
        with it an expiry path that turns a timeout on a
        ``NON_IDEMPOTENT_NO_RETRY`` create into a different public failure.
        """
        workflow = SOURCE_ADD_TEXT_DEF.key
        if idempotent:
            # Checked before the leaf gate: refusing a replay writes nothing,
            # and the pre-P10 handler refused before it looked at anything else.
            raise _source_add_failure(
                workflow,
                SourceAddFailureRecord(
                    kind=SourceAddFailureKind.NON_IDEMPOTENT_RETRY,
                    message=_TEXT_NON_IDEMPOTENT_MESSAGE,
                    args=(_TEXT_NON_IDEMPOTENT_MESSAGE,),
                ),
            )
        require_leaves(self._backend, SOURCE_REGISTER_DEF.key)

        _source_logger.debug("Adding text source to notebook %s: %s", notebook_id, title)
        try:
            registered = await self._backend.invoke(
                SOURCE_REGISTER_DEF,
                SourceRegisterInput(
                    notebook_id,
                    SourceRegisterKind.TEXT,
                    title=title,
                    content=content,
                ),
                deadline=deadline,
            )
        except BackendError as error:
            raise self._text_registration_failure(workflow, title, error) from error.__cause__

        source = next(iter(registered.sources), None)
        if source is None:
            raise _source_add_failure(
                workflow,
                SourceAddFailureRecord(
                    kind=SourceAddFailureKind.SOURCE_ADD,
                    message=f"API returned no data for text source: {title}",
                    args=(f"API returned no data for text source: {title}",),
                    url=title,
                ),
            )
        return SourceAddTextResult(source)

    @staticmethod
    def _text_registration_failure(
        workflow: Operation,
        title: str,
        error: BackendError,
    ) -> BackendError:
        """Rebind one registration failure to the workflow, wrapping only the RPC leaf.

        The leaf already captured its own public graph as neutral evidence, so
        the wrap is a record nesting a record: no public exception is built,
        yet ``_backend_compat`` reconstructs the identical
        ``SourceAddError(title, cause=<the RPC error>) from <the RPC error>``
        the retired row produced.
        """
        if isinstance(error, BackendContractError) or error.reason is None:
            return error
        if isinstance(error, BackendDeadlineExceededError):
            # An explicitly supplied budget running out is the caller's answer,
            # not a source-add failure: keep the subclass and only re-attribute
            # it, exactly as ``update`` does.
            return rebind_operation(error, workflow)
        leaf = _leaf_failure_record(error)
        if leaf is None:
            # Nothing to wrap and nothing to replay: re-attribute the reason and
            # let the compatibility projector build the public exception from it.
            return rebind_operation(error, workflow)
        if leaf.kind not in _WRAPPED_REGISTRATION_FAILURE_KINDS:
            # ADR-0019 catch ordering: the transport four-tuple and every other
            # public leaf keep their own type, message and fields.
            return _source_add_failure(
                workflow,
                leaf,
                outcome_unknown=error.outcome_unknown,
                dispatched=error.dispatched,
            )
        message = f"Failed to add text source '{title}'"
        return _source_add_failure(
            workflow,
            SourceAddFailureRecord(
                kind=SourceAddFailureKind.SOURCE_ADD,
                message=message,
                args=(message,),
                url=title,
                cause=leaf,
                # ``raise SourceAddError(...) from e`` inside ``except RPCError
                # as e``: the RPC error is the explicit cause, the implicit
                # context, and the reason the context is suppressed.
                context_is_cause=True,
                explicit_cause=True,
                suppress_context=True,
            ),
        )

    async def add_drive(
        self,
        notebook_id: str,
        file_id: str,
        title: str,
        *,
        mime_type: str,
        wait: bool,
        wait_timeout: float,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceAddDriveResult:
        return await self._backend.invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(
                notebook_id,
                file_id,
                title,
                mime_type,
                wait=wait,
                wait_timeout=wait_timeout,
            ),
            deadline=deadline,
        )

    async def finalize_drive_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddDriveResult:
        """Apply a waited Drive title under the original add operation."""

        return await self._backend.invoke(
            SOURCE_ADD_DRIVE_DEF,
            SourceAddDriveInput(
                notebook_id,
                "",
                requested_title,
                "application/vnd.google-apps.document",
                finalize_source=source,
            ),
            deadline=None,
        )

    async def add_file(
        self,
        notebook_id: str,
        file_path: str | Path,
        *,
        mime_type: str | None,
        wait: bool,
        wait_timeout: float,
        title: str | None,
        on_progress: SourceProgressCallback | None,
    ) -> SourceAddFileResult:
        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.LOCAL,
                file_path=file_path,
                mime_type=mime_type,
                title=title,
                wait=wait,
                wait_timeout=wait_timeout,
                on_progress=on_progress,
            ),
            deadline=None,
        )

    async def add_drive_file(
        self,
        notebook_id: str,
        document_id: str,
        *,
        title: str | None,
        wait: bool,
        wait_timeout: float,
    ) -> SourceAddFileResult:
        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.DRIVE_DOWNLOAD,
                document_id=document_id,
                title=title,
                wait=wait,
                wait_timeout=wait_timeout,
            ),
            deadline=None,
        )

    async def finalize_file_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddFileResult:
        """Apply a waited upload title under the original add operation."""

        return await self._backend.invoke(
            SOURCE_ADD_FILE_DEF,
            SourceAddFileInput(
                notebook_id,
                SourceFileInputKind.LOCAL,
                title=requested_title,
                finalize_source=source,
            ),
            deadline=None,
        )

    async def delete(self, notebook_id: str, source_id: str) -> None:
        await self._backend.invoke(
            SOURCE_DELETE_DEF,
            SourceDeleteInput(notebook_id, source_id),
            deadline=None,
        )

    async def update(
        self,
        notebook_id: str,
        source_id: str,
        new_title: str,
        *,
        return_object: bool,
        deadline: RuntimeDeadline | None = None,
    ) -> SourceUpdateResult:
        """Rename through one patch leaf and hydrate only a null echo."""
        workflow = SOURCE_UPDATE_DEF.key
        # The conditional leaf set is still checked up front: a later backend
        # must never perform the title write and only then discover it cannot
        # execute the null-echo hydration path.
        require_leaves(self._backend, SOURCE_PATCH_TITLE_DEF.key, SOURCE_GET_DEF.key)
        if deadline is None and self._deadline_factory is not None:
            deadline = self._deadline_factory.start()

        write_dispatched = False
        try:
            patched = await self._backend.invoke(
                SOURCE_PATCH_TITLE_DEF,
                SourcePatchTitleInput(notebook_id, source_id, new_title),
                deadline=deadline,
            )
            write_dispatched = True
            source = patched.source
            if source is None:
                hydrated = await self._backend.invoke(
                    SOURCE_GET_DEF,
                    SourceGetInput(notebook_id, source_id),
                    deadline=deadline,
                )
                source = hydrated.source
                if source is None:
                    raise BackendError(
                        message=f"Source not found: {source_id}",
                        operation=workflow,
                        diagnostics=MappingProxyType(
                            {
                                "source_id": source_id,
                                "raw_response": None,
                            }
                        ),
                        reason=BackendErrorReason.SOURCE_NOT_FOUND,
                    )
            return SourceUpdateResult(source if return_object else None)
        except BackendError as error:
            if error.operation is workflow:
                raise
            if write_dispatched and isinstance(error, BackendDeadlineExceededError):
                # The patch landed but the hydration read exhausted the shared
                # budget: the workflow outcome is now unsafe to retry blindly.
                error = mark_backend_outcome_unknown(error)
            raise rebind_operation(error, workflow) from error.__cause__

    async def refresh(self, notebook_id: str, source_id: str) -> None:
        await self._backend.invoke(
            SOURCE_REFRESH_DEF,
            SourceRefreshInput(notebook_id, source_id),
            deadline=None,
        )

    async def check_freshness(self, notebook_id: str, source_id: str) -> bool:
        result = await self._backend.invoke(
            SOURCE_CHECK_FRESHNESS_DEF,
            SourceFreshnessInput(notebook_id, source_id),
            deadline=None,
        )
        return result.fresh

    async def wait_snapshot(self, notebook_id: str) -> SourceWaitSnapshotResult:
        """Fetch one neutral snapshot for a facade-owned source poll tick."""
        return await self._backend.invoke(
            SOURCE_WAIT_DEF,
            SourceWaitSnapshotInput(notebook_id),
            # Source wait historically owns a relative polling budget and does
            # not clamp an in-flight GET_NOTEBOOK read to the remaining time.
            deadline=None,
        )

    async def get_guide(self, notebook_id: str, source_id: str) -> SourceGuideResult:
        return await self._backend.invoke(
            SOURCE_GET_GUIDE_DEF,
            SourceGuideInput(notebook_id, source_id),
            deadline=None,
        )

    async def get_fulltext(
        self,
        notebook_id: str,
        source_id: str,
        *,
        output_format: str,
    ) -> SourceFulltextResult:
        return await self._backend.invoke(
            SOURCE_GET_FULLTEXT_DEF,
            SourceFulltextInput(notebook_id, source_id, output_format),
            deadline=None,
        )


__all__ = ["SourceService"]
