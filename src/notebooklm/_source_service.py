"""Transport-neutral semantic service for the migrated P6.7 Source slice."""

from __future__ import annotations

import logging
from dataclasses import replace
from functools import partial
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
    _IdempotentCreateResult,
    idempotent_create,
    semantic_may_have_committed,
)
from ._operations import Operation
from ._semantic.records import (
    SOURCE_ADD_DRIVE_DEF,
    SOURCE_ADD_FILE_DEF,
    SOURCE_ADD_TEXT_DEF,
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
    SourceAddDriveResult,
    SourceAddFailureKind,
    SourceAddFailureRecord,
    SourceAddFileInput,
    SourceAddFileResult,
    SourceAddTextResult,
    SourceAddTitleState,
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
from ._source_add_reports import (
    CREATE_CONTEXT_FAILURE,
    DEFAULT_ADD_FAILURE_MESSAGE,
    DIRECT_PROBE_REASONS,
    DRIVE_BLANK_FILE_ID_MESSAGE,
    DRIVE_NULL_RESULT_MESSAGE,
    RENAME_SWALLOWED_REASONS,
    TEXT_NON_IDEMPOTENT_MESSAGE,
    WRAPPED_REGISTRATION_FAILURE_KINDS,
    GuardedRegistration,
    degraded_failure_record,
    drive_baseline_ambiguity,
    drive_match_ambiguity,
    drive_subject,
    failure_type_name,
    leaf_failure_record,
    source_add_failure,
    url_baseline_ambiguity,
    url_match_ambiguity,
)
from ._source_batch_service import run_url_batch_registration
from ._url_utils import extract_youtube_video_id, is_youtube_url

# The same logger name and level the retired rows logged under.
_source_logger = logging.getLogger("notebooklm").getChild("_sources")


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

        try:
            created = await self._guarded_registration(
                notebook_id,
                GuardedRegistration(
                    workflow=workflow,
                    label="add_url",
                    identity=url,
                    subject=f"URL source {url!r}",
                    payload=SourceRegisterInput(
                        notebook_id,
                        SourceRegisterKind.URL,
                        urls=(url,),
                        youtube_flags=(bool(video_id),),
                    ),
                    matches=lambda source: source.url == url,
                    null_result_message=f"API returned no data for URL: {url}",
                    baseline_ambiguity=partial(url_baseline_ambiguity, url),
                    match_ambiguity=partial(url_match_ambiguity, url),
                    idempotency_label=f"sources.add_url[{url[:40]}]",
                ),
                deadline=deadline,
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
                workflow=workflow,
                hydrate_on_null=True,
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
            workflow=SOURCE_ADD_URL_DEF.key,
            hydrate_on_null=True,
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

    async def _guarded_registration(
        self,
        notebook_id: str,
        variant: GuardedRegistration,
        *,
        deadline: RuntimeDeadline | None,
    ) -> _IdempotentCreateResult[SourceRecord]:
        """Baseline, register, and reconcile one guarded source create.

        The algorithm both probed registrations run, with ``variant`` supplying
        everything that differs between them:

        1. one unconditional ``source.list`` baseline of source ids, captured
           before the first create so a probe match can be attributed to *this*
           call — neither a URL nor a Drive ``documentId`` is unique within a
           notebook (#2204, #2113), so an unfiltered match could hand back a
           pre-existing copy as if it were the one just created;
        2. one ``source.register`` write;
        3. on commit uncertainty only, a reconciling ``source.list`` probe
           filtered against that baseline, with four failure branches.

        .. note::
           **A probe that cannot answer aborts the add (#2220).** The probe
           returns ``None`` only when it has affirmatively established that no
           matching source exists; ``None`` is read by ``idempotent_create`` as
           evidence the create did not land, and acted on by repeating it. A
           broken probe is not that evidence, and retrying anyway would silently
           turn a ``PROBE_THEN_CREATE`` operation into an at-least-once one at
           the exact moment its guarantee matters.
        """
        workflow = variant.workflow
        baseline_ids, baseline_failure, baseline_error_name = await self._add_baseline(
            notebook_id,
            deadline=deadline,
            label=variant.label,
        )
        last_create_error: BackendError | None = None

        async def register() -> SourceRecord:
            nonlocal last_create_error
            try:
                registered = await self._backend.invoke(
                    SOURCE_REGISTER_DEF,
                    variant.payload,
                    deadline=deadline,
                )
            except BackendError as leaf_error:
                # Transport reasons keep their own public identity so
                # ``semantic_may_have_committed`` can still see the commit
                # uncertainty and run the probe; only the residual RPC family
                # is wrapped, exactly as the retired ``except RPCError`` did.
                error = self._registration_failure(workflow, variant.identity, leaf_error)
                last_create_error = error
                raise error from leaf_error.__cause__
            source = next(iter(registered.sources), None)
            if source is None:
                raise source_add_failure(
                    workflow,
                    SourceAddFailureRecord(
                        kind=SourceAddFailureKind.SOURCE_ADD,
                        message=variant.null_result_message,
                        args=(variant.null_result_message,),
                        url=variant.identity,
                    ),
                )
            return source

        async def probe() -> SourceRecord | None:
            create_error = last_create_error
            if create_error is None:
                raise BackendError(
                    f"{workflow.value} reconciliation started without a registration failure",
                    operation=workflow,
                )
            try:
                current = await self._probe_snapshot(notebook_id, deadline=deadline)
            except BackendError as leaf_error:
                raise self._probe_failure(
                    workflow,
                    variant.identity,
                    variant.subject,
                    leaf_error,
                    create_error,
                    label=variant.label,
                ) from leaf_error.__cause__
            except Exception as error:
                _source_logger.warning(
                    "%s: probe list() failed with a non-transport error (%s); the "
                    "create cannot be confirmed, so it will not be retried",
                    variant.label,
                    type(error).__name__,
                    exc_info=True,
                )
                raise self._unresolved_add_error(
                    workflow,
                    variant.identity,
                    variant.subject,
                    create_error,
                    probe_failure=None,
                    failure_name=type(error).__name__,
                ) from error

            matches = [source for source in current.sources if variant.matches(source)]
            if baseline_ids is not None:
                matches = [source for source in matches if source.id not in baseline_ids]
            elif matches:
                # Without a baseline a match may predate this add; adopting it
                # would report a create that never landed.
                raise self._unconfirmed_add_error(
                    workflow,
                    variant.identity,
                    variant.baseline_ambiguity(matches, baseline_error_name),
                    create_error,
                    cause=baseline_failure,
                )
            if len(matches) == 1:
                (match,) = matches  # exactly one (len==1 guard); unpack, not matches[0]
                return match
            if len(matches) > 1:
                raise self._unconfirmed_add_error(
                    workflow,
                    variant.identity,
                    variant.match_ambiguity(matches),
                    create_error,
                )
            return None

        return await idempotent_create(
            register,
            probe,
            may_have_committed=semantic_may_have_committed,
            label=variant.idempotency_label,
        )

    async def _probe_snapshot(
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

    async def _add_baseline(
        self,
        notebook_id: str,
        *,
        deadline: RuntimeDeadline | None,
        label: str,
    ) -> tuple[set[str] | None, SourceAddFailureRecord | None, str | None]:
        """Snapshot source ids before the first create, or degrade to ``None``.

        ``None`` is the "baseline unavailable" sentinel; the probe then refuses
        to guess. The read runs before anything is written, so proceeding
        without it is safe — but the failure is retained so the ambiguity
        report can say what went wrong long after this line ran.

        ``label`` is the workflow's own name in the diagnostic. The URL and
        Drive baselines are the same read of the same leaf answering the same
        question, so they share one implementation; the log line still names
        which add lost its probe, exactly as the two below-port copies did.
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
                "%s: baseline list() failed (%s); the idempotency probe can no "
                "longer tell a source this call created from one that was already "
                "there, so a transport failure will surface as an ambiguity error "
                "instead of recovering",
                label,
                failure_type_name(error),
                exc_info=True,
            )
            return None, degraded_failure_record(error), failure_type_name(error)
        return {source.id for source in baseline.sources}, None, None

    @classmethod
    def _registration_failure(
        cls,
        workflow: Operation,
        identity: str,
        error: BackendError,
    ) -> BackendError:
        """Rebind one probed registration failure, wrapping only the RPC leaf.

        The reason is deliberately preserved on everything this does not wrap:
        it is what ``semantic_may_have_committed`` reads to decide whether the
        write may have landed, and therefore whether the probe runs at all.

        ``identity`` is what the retired handler passed as ``SourceAddError``'s
        first argument — the URL for ``add_url``, the requested title for
        ``add_drive`` — and is both the record's ``url`` field and the value
        the default message interpolates. The two handlers ran the identical
        ``except RPCError as e: raise SourceAddError(<identity>, cause=e) from
        e``, so one implementation covers both.
        """
        if isinstance(error, BackendContractError) or error.reason is None:
            return error
        if isinstance(error, BackendDeadlineExceededError):
            # An expiry keeps its subclass; a *dispatched* one is still
            # commit-uncertain, so re-attributing it leaves the probe reachable.
            return rebind_operation(error, workflow)
        leaf = leaf_failure_record(error)
        if leaf is None or leaf.kind not in WRAPPED_REGISTRATION_FAILURE_KINDS:
            # The transport four-tuple and every other public leaf keep their
            # own type, message and fields (ADR-0019 catch ordering); a backend
            # that captured no graph is re-attributed and projected by reason.
            return rebind_operation(error, workflow)
        message = DEFAULT_ADD_FAILURE_MESSAGE.format(url=identity)
        return source_add_failure(
            workflow,
            SourceAddFailureRecord(
                kind=SourceAddFailureKind.SOURCE_ADD,
                message=message,
                args=(message,),
                url=identity,
                cause=leaf,
                # ``raise SourceAddError(identity, cause=e) from e`` inside ``except
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
    def _probe_failure(
        cls,
        workflow: Operation,
        identity: str,
        subject: str,
        leaf_error: BackendError,
        create_error: BackendError,
        *,
        label: str,
    ) -> BackendError:
        """Report a probe that could not settle whether the create committed.

        ``identity`` is the ``SourceAddError`` first argument (the URL, or the
        Drive requested title); ``subject`` is how the UNRESOLVED message names
        the thing that could not be confirmed, which for Drive is the *file id*
        rather than that identity. ``label`` names the workflow in the log line.
        """
        error = rebind_operation(leaf_error, workflow)
        if isinstance(leaf_error, BackendDeadlineExceededError):
            # An aggregate budget running out is the caller's own answer, not a
            # source-add failure: keep the subclass and only record that the
            # create's outcome is now unconfirmed.
            return mark_backend_outcome_unknown(cls._attach_create_context(error, create_error))
        if error.reason in DIRECT_PROBE_REASONS:
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
            leaf = leaf_failure_record(error)
            if leaf is None:
                return mark_backend_outcome_unknown(cls._attach_create_context(error, create_error))
            return cls._attach_create_context(
                source_add_failure(
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
            "%s: probe list() failed with a non-transport error (%s); the "
            "create cannot be confirmed, so it will not be retried",
            label,
            failure_type_name(leaf_error),
            exc_info=True,
        )
        return cls._unresolved_add_error(
            workflow,
            identity,
            subject,
            create_error,
            probe_failure=leaf_failure_record(leaf_error),
            failure_name=failure_type_name(leaf_error),
        )

    @classmethod
    def _unresolved_add_error(
        cls,
        workflow: Operation,
        identity: str,
        subject: str,
        create_error: BackendError,
        *,
        probe_failure: SourceAddFailureRecord | None,
        failure_name: str,
    ) -> BackendError:
        """The #2220 "probe could not answer" report, chain and all.

        The URL and Drive handlers wrote this message word for word alike apart
        from ``subject`` (``URL source <url>`` / ``Drive source <file id>``), so
        it is one string with one hole rather than two near-copies that can
        drift apart.
        """
        return cls._unconfirmed_add_error(
            workflow,
            identity,
            # Front-loaded on purpose: the MCP and REST surfaces truncate
            # messages at 300 characters, which cut the closing instruction
            # mid-word on a realistic URL. The action comes first; the
            # narrative can be lost.
            "UNRESOLVED — do not blindly retry; check the notebook "
            f"source list first. Cannot confirm {subject}: "
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
    def _unconfirmed_add_error(
        cls,
        workflow: Operation,
        identity: str,
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
        create_failure = degraded_failure_record(create_error)
        if explicit and cause is not None and create_failure is not None:
            cause = replace(cause, context=create_failure)
        error = source_add_failure(
            workflow,
            SourceAddFailureRecord(
                kind=SourceAddFailureKind.SOURCE_ADD,
                message=message,
                args=(message,),
                url=identity,
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
        failure = degraded_failure_record(create_error)
        if failure is None:
            return error
        return annotate_backend_error(error, **{CREATE_CONTEXT_FAILURE: failure})

    @classmethod
    def _add_failure(cls, workflow: Operation, error: BackendError) -> BackendError:
        """Report one failed add with its full public graph, not just a reason.

        A create failure the workflow re-attributed rather than wrapped still
        carries only its closed *reason* at this point, because the reason is
        what ``semantic_may_have_committed`` reads to decide whether to probe.
        The probe has finished by the time this runs, so the leaf's captured
        public graph can be reported instead — which is how fields no reason
        can express (``source_id`` / ``stage`` on a tagged partial failure)
        reach the facade, exactly as they did when the retired rows captured the
        escaping exception themselves.
        """
        leaf = (
            None if isinstance(error, BackendDeadlineExceededError) else leaf_failure_record(error)
        )
        if leaf is None:
            return error
        reported = source_add_failure(
            workflow,
            leaf,
            outcome_unknown=error.outcome_unknown,
            dispatched=error.dispatched,
        )
        leaf_operation = (error.diagnostics or {}).get("leaf_operation")
        if leaf_operation is None:
            return reported
        return annotate_backend_error(reported, leaf_operation=leaf_operation)

    @classmethod
    def _url_add_failure(cls, workflow: Operation, error: BackendError) -> BackendError:
        """One failed URL add: its full graph, plus the retired row's receipt.

        Only the URL result type carries a receipt, so only its failures carry
        one; a Drive failure reports the same graph with nothing attached, as
        the Drive row did.
        """
        reported = cls._add_failure(workflow, error)
        return annotate_backend_error(
            reported,
            receipt=SourceAddUrlReceipt(
                commit_state=(
                    SourceAddCommitState.UNKNOWN
                    if reported.outcome_unknown
                    else SourceAddCommitState.FAILED
                ),
                title_state=SourceAddTitleState.NOT_ATTEMPTED,
                outcome_unknown=reported.outcome_unknown,
            ),
        )

    async def _honor_requested_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str | None,
        *,
        deadline: RuntimeDeadline | None,
        workflow: Operation,
        hydrate_on_null: bool,
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
                workflow=workflow,
                hydrate_on_null=hydrate_on_null,
            )
        except BackendError as error:
            if error.reason not in RENAME_SWALLOWED_REASONS:
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
        workflow: Operation,
        hydrate_on_null: bool,
    ) -> SourceRecord | None:
        """One title set-op, hydrating a null echo through ``source.get``.

        ``hydrate_on_null`` is per-workflow because the retired rows differed:
        the URL row read the renamed source back, while the Drive row took the
        null echo as the answer and let the caller keep the requested title.
        Hydrating on the Drive path would add a ``GET_NOTEBOOK`` — and with it a
        recency write and a new ``SourceNotFoundError`` failure mode — to a
        phase that never had either.
        """
        patched = await self._backend.invoke(
            SOURCE_PATCH_TITLE_DEF,
            SourcePatchTitleInput(notebook_id, source_id, new_title),
            deadline=deadline,
        )
        if patched.source is not None:
            return patched.source
        if not hydrate_on_null:
            return None
        hydrated = await self._backend.invoke(
            SOURCE_GET_DEF,
            SourceGetInput(notebook_id, source_id),
            deadline=deadline,
        )
        if hydrated.source is None:
            raise BackendError(
                message=f"Source not found: {source_id}",
                operation=workflow,
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
        """Register many URLs in one write and attribute the response positionally.

        The gate, the empty short-circuit and the one aggregate budget; the
        workflow itself is ``_source_batch_service.run_url_batch_registration``,
        which lives next door because this module is at the ADR-0008 size budget
        with three hoisted workflows in it. This stays the single entry point —
        the facade and the MCP/REST adapters reach the batch only through it.
        """
        # Both leaves are checked before the write, exactly as ``add_url``
        # checks its finalise leaves: a backend must never register the sources
        # and only then discover it cannot run the reconciliation.
        require_leaves(self._backend, SOURCE_REGISTER_DEF.key, SOURCE_LIST_DEF.key)
        if not urls:
            return SourceAddUrlBatchResult(())
        # One absolute budget for the write and its reconciliation, minted here
        # for the same reason ``add_url`` mints one: the retired row ran under
        # the deadline ``WebRpcBackend`` seeded for the whole ``CLIENT_TIMEOUT``
        # operation, and the workflow is that operation's owner now.
        return await run_url_batch_registration(
            self._backend,
            notebook_id,
            urls,
            deadline=self._start_deadline(deadline),
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
            raise source_add_failure(
                workflow,
                SourceAddFailureRecord(
                    kind=SourceAddFailureKind.NON_IDEMPOTENT_RETRY,
                    message=TEXT_NON_IDEMPOTENT_MESSAGE,
                    args=(TEXT_NON_IDEMPOTENT_MESSAGE,),
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
            raise source_add_failure(
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
        leaf = leaf_failure_record(error)
        if leaf is None:
            # Nothing to wrap and nothing to replay: re-attribute the reason and
            # let the compatibility projector build the public exception from it.
            return rebind_operation(error, workflow)
        if leaf.kind not in WRAPPED_REGISTRATION_FAILURE_KINDS:
            # ADR-0019 catch ordering: the transport four-tuple and every other
            # public leaf keep their own type, message and fields.
            return source_add_failure(
                workflow,
                leaf,
                outcome_unknown=error.outcome_unknown,
                dispatched=error.dispatched,
            )
        message = f"Failed to add text source '{title}'"
        return source_add_failure(
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
        """Import one native Google Drive document behind a probe-then-create.

        The workflow the P9.4b ``source.add_drive`` row owned, sequenced over
        typed leaves instead: one ``source.list`` baseline, one
        ``source.register`` drive allocation, a reconciling ``source.list``
        probe when that allocation may have committed, and a best-effort
        ``source.patch_title`` finalise.

        The probe matches on :attr:`SourceRecord.drive_document_id`, the Drive
        ``documentId`` the backend echoes back in the source metadata.
        Drive-backed sources carry **no** URL, so the URL-shaped probe this
        replaced could never match one and silently duplicated the source on
        every retry until #2113.

        A ``documentId`` is **not** unique within a notebook — the repo's own
        ``sources_check_freshness_drive`` capture holds two source ids sharing
        one — so probe matches are filtered against a baseline of source ids
        taken **before** the first create attempt, and an unavailable baseline
        or an ambiguous multi-match is reported as an unresolved failure rather
        than guessed at.

        .. note::
           The baseline snapshot is taken on *every* call and is a
           ``GET_NOTEBOOK`` on the web, which the backend answers by **writing**
           ``lastViewedTime`` (#2126) — so every ``add_drive`` promotes the
           notebook in the user's *Recent* list. Accepted, and unchanged by the
           hoist: source ids are published only inside that payload, and
           ``source.get`` cannot substitute for ``source.list`` — the baseline
           set and the reconcile both need the *whole* id set, which a single-id
           result cannot express.

        .. warning::
           The baseline establishes *when* a matching source appeared, not
           *who* created it. If two callers add the same Drive file to one
           notebook concurrently and one create fails before committing, the
           failed caller's probe can attribute the other caller's source to
           itself. The wire carries no client-supplied idempotency key, so
           serialize concurrent adds of the same file into a notebook if you
           need that guarantee.

        .. note::
           The ``title`` is sent on the wire but **ignored** for native Drive
           imports: NotebookLM re-derives the display title from live Drive
           metadata. The finalise phase is what makes an explicit ``title``
           stick (#1960).

        ``wait``/``wait_timeout`` are accepted for signature stability and are
        the public facade's: readiness polling never crossed the port, and the
        retired row failed closed on any attempt to take it below one.
        """
        del wait_timeout  # readiness polling is the facade's; see the docstring.
        workflow = SOURCE_ADD_DRIVE_DEF.key
        if not file_id or not file_id.strip():
            # Checked before the leaf gate and before the baseline read, exactly
            # where the pre-P10 handler checked it: a blank Drive id is also
            # unmatchable by the probe below (a row's ``drive_document_id`` is
            # never ``""``), so without this guard a transport failure would
            # retry the blank add and could leave two garbage sources behind.
            raise source_add_failure(
                workflow,
                SourceAddFailureRecord(
                    kind=SourceAddFailureKind.VALIDATION,
                    message=DRIVE_BLANK_FILE_ID_MESSAGE,
                    args=(DRIVE_BLANK_FILE_ID_MESSAGE,),
                ),
            )
        # The title leaf is checked up front even when no title was requested,
        # for the reason ``add_url`` states: a backend must never register the
        # source and only then discover it cannot run the promised finalise.
        # ``source.get`` is absent on purpose — the Drive finalise does not
        # hydrate a null echo.
        require_leaves(
            self._backend,
            SOURCE_LIST_DEF.key,
            SOURCE_REGISTER_DEF.key,
            SOURCE_PATCH_TITLE_DEF.key,
        )
        # One absolute budget for every phase, minted here now that the workflow
        # owns the ``CLIENT_TIMEOUT`` operation the retired row ran under.
        deadline = self._start_deadline(deadline)

        _source_logger.debug("Adding Drive source to notebook %s: %s", notebook_id, title)

        try:
            created = await self._guarded_registration(
                notebook_id,
                GuardedRegistration(
                    workflow=workflow,
                    label="add_drive",
                    identity=title,
                    subject=drive_subject(file_id),
                    payload=SourceRegisterInput(
                        notebook_id,
                        SourceRegisterKind.DRIVE,
                        title=title,
                        file_id=file_id,
                        mime_type=mime_type,
                    ),
                    # Exact equality — not a substring test — so neither an
                    # interior substring nor a prefix collision (``abc`` vs
                    # ``abcdef``) can produce a false positive, and non-Drive
                    # rows (``drive_document_id is None``) never match.
                    matches=lambda source: source.drive_document_id == file_id,
                    null_result_message=DRIVE_NULL_RESULT_MESSAGE.format(
                        title=title, mime_type=mime_type
                    ),
                    baseline_ambiguity=partial(drive_baseline_ambiguity, file_id),
                    match_ambiguity=partial(drive_match_ambiguity, file_id),
                    idempotency_label=f"sources.add_drive[{file_id}]",
                ),
                deadline=deadline,
            )
        except BackendError as error:
            raise self._add_failure(workflow, error) from error.__cause__

        # A probed result is attributable to this call — the baseline diff is
        # what proves it — so the requested title is honored either way. Under
        # ``wait`` the facade renames after readiness (``finalize_drive_title``).
        source = (
            created.value
            if wait
            else await self._honor_requested_title(
                notebook_id,
                created.value,
                title,
                deadline=deadline,
                workflow=workflow,
                hydrate_on_null=False,
            )
        )
        return SourceAddDriveResult(source)

    async def finalize_drive_title(
        self,
        notebook_id: str,
        source: SourceRecord,
        requested_title: str,
    ) -> SourceAddDriveResult:
        """Apply a waited Drive title under the original add operation."""
        require_leaves(self._backend, SOURCE_PATCH_TITLE_DEF.key)
        deadline = self._start_deadline(None)
        renamed = await self._honor_requested_title(
            notebook_id,
            source,
            requested_title,
            deadline=deadline,
            workflow=SOURCE_ADD_DRIVE_DEF.key,
            hydrate_on_null=False,
        )
        return SourceAddDriveResult(renamed)

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
