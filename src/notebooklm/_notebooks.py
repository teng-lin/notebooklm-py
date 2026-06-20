"""Notebook operations API."""

import logging
import reprlib
from typing import Any

from ._idempotency import idempotent_create
from ._notebook_metadata import (
    NotebookMetadataService,
    NotebookSourceLister,
    create_default_source_lister,
)
from ._notebook_payloads import (
    _PROMPT_SUGGESTIONS_DEFAULT_MODE,
    build_prompt_suggestions_params,
)
from ._row_adapters.notebooks import PromptSuggestionRow, unwrap_prompt_suggestions
from ._row_adapters.sources import SourceRow
from ._runtime.contracts import RpcCaller
from ._settings import build_get_user_settings_params, extract_account_limits
from ._sharing_manager import ShareManager
from ._source.upload_payloads import build_template_block
from .exceptions import (
    AuthError,
    DecodingError,
    NetworkError,
    NotebookLimitError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from .rpc import RPCMethod, safe_index
from .types import (
    AccountLimits,
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    PromptSuggestion,
    SuggestedTopic,
)

logger = logging.getLogger(__name__)


CREATE_NOTEBOOK_QUOTA_RPC_CODE = 3


def build_create_notebook_params(title: str) -> list[Any]:
    """Return the canonical CREATE_NOTEBOOK RPC payload.

    The trailing :func:`build_template_block` replaced the old flat ``[2], [1]``
    tail that migrated backends now reject with ``status=3`` (#1546).
    """
    return [title, None, None, build_template_block()]


def build_get_notebook_params(notebook_id: str) -> list[Any]:
    """Return the canonical GET_NOTEBOOK (``rLM1Ne``) RPC payload.

    The Gemini-3.5 rollout migrated the read path's trailing template block from
    the flat ``[2]`` to the same nested :func:`build_template_block` wrapper the
    write path adopted in #1548 (issue #1549). Live-verified forward-compatible:
    the nested shape returns a byte-identical decoded notebook (notebook id /
    title and every ``SourceRow``) as the flat ``[2]`` on an un-migrated account,
    so it is safe across cohorts. The trailing ``None, 0`` is unchanged — only
    the template block at position 2 is migrated (the narrow scope #1549 tracks).
    """
    return [notebook_id, None, build_template_block(), None, 0]


def _extract_summary(outer: Any) -> str:
    """Extract the summary string from a SUMMARIZE ``result[0]`` payload.

    The expected shape is ``[[summary_string, ...], ...]`` — i.e. the summary
    lives at ``outer[0][0]``. Only a genuinely *absent* summary is treated as
    routinely-optional: a brand-new, source-less notebook has no summary yet,
    so the server returns ``None`` at ``outer``, an empty ``outer``, or an
    explicitly-null summary slot (``outer[0] is None``). Those three shapes
    short-circuit to ``""`` so a healthy "no summary yet" response doesn't
    surface as schema drift.

    Everything else descends through ``safe_index``: a *present-but-malformed*
    payload — a scalar ``outer`` (e.g. ``123``), or a non-``None`` ``outer[0]``
    that isn't the expected ``[summary_string, ...]`` list — is genuine drift
    and raises ``UnknownRPCMethodError`` with method_id + source rather than
    silently becoming an empty summary (which would mask the wire-schema move).

    Returns:
        The summary string, or ``""`` when the payload omits the summary
        slot (the caller is responsible for treating an empty summary as
        "no description available").
    """
    # Genuinely-absent summary (no payload, empty payload, or null slot) is the
    # routine "no summary yet" case — return "" without logging drift.
    if outer is None:
        return ""
    if isinstance(outer, list) and (
        not outer
        or safe_index(
            outer, 0, method_id=RPCMethod.SUMMARIZE.value, source="_notebooks._extract_summary"
        )
        is None
    ):
        return ""
    # Descend outer[0][0] via safe_index. A scalar ``outer`` or a malformed
    # ``outer[0]`` (present, non-None, but not the expected list) raises drift
    # at the failing step rather than silently returning "".
    summary_val = safe_index(
        outer,
        0,
        0,
        method_id=RPCMethod.SUMMARIZE.value,
        source="_notebooks._extract_summary",
    )
    if summary_val is None:
        return ""
    return str(summary_val)


def _extract_suggested_topics(outer: Any) -> list[SuggestedTopic]:
    """Extract suggested topics from a SUMMARIZE ``result[0]`` payload.

    The expected shape is ``[..., [[[question, prompt, ...], ...], ...], ...]``
    — the topics list lives at ``outer[1][0]``, and each topic is itself a
    list whose first two entries are ``question`` and ``prompt``.

    The outer ``[1]`` slot is treated as routinely-optional (a notebook with
    no topics legitimately omits it, so missing-slot is not "drift"); the
    inner ``[0]`` descent goes through ``safe_index`` so genuine schema
    drift surfaces with method_id + source. Per-topic shape checks log a
    debug diagnostic and skip malformed entries rather than abort, because
    a partial response (some valid topics + some drift) is more useful to
    callers than an empty list.

    Returns:
        List of :class:`SuggestedTopic`. Empty when the payload omits the
        slot or when every topic entry fails shape validation.
    """
    # outer[1] is routinely absent/empty when a notebook has no topics;
    # use a plain guard rather than safe_index so that case doesn't log
    # a drift warning on every healthy "no topics" response. Still log
    # a DEBUG record so partial descriptions remain observable to anyone
    # tailing logs while diagnosing a notebook with missing topics.
    if not isinstance(outer, list) or len(outer) < 2:
        logger.debug("_extract_suggested_topics: Partial description — no outer[1] slot")
        return []

    topics_container = safe_index(
        outer, 1, method_id=RPCMethod.SUMMARIZE.value, source="_notebooks._extract_suggested_topics"
    )
    if not isinstance(topics_container, list) or len(topics_container) == 0:
        logger.debug(
            "_extract_suggested_topics: Partial description — outer[1] is empty or non-list"
        )
        return []

    topics_list = safe_index(
        topics_container,
        0,
        method_id=RPCMethod.SUMMARIZE.value,
        source="_notebooks._extract_suggested_topics",
    )
    if not isinstance(topics_list, list):
        if topics_list is not None:
            logger.debug(
                "_extract_suggested_topics: expected list at outer[1][0], got %s",
                type(topics_list).__name__,
            )
        return []

    topics: list[SuggestedTopic] = []
    for index, topic in enumerate(topics_list):
        if not isinstance(topic, list) or len(topic) < 2:
            logger.debug(
                "_extract_suggested_topics: skipping malformed topic at index %d (type=%s)",
                index,
                type(topic).__name__,
            )
            continue
        # ``topic`` is guarded to a list of len >= 2 above, so these slot reads
        # cannot fail; ``safe_index`` keeps the position knowledge on the
        # schema-drift seam without changing behaviour.
        question = safe_index(
            topic,
            0,
            method_id=RPCMethod.SUMMARIZE.value,
            source="_notebooks._extract_suggested_topics",
        )
        prompt = safe_index(
            topic,
            1,
            method_id=RPCMethod.SUMMARIZE.value,
            source="_notebooks._extract_suggested_topics",
        )
        topics.append(
            SuggestedTopic(
                question=str(question) if question else "",
                prompt=str(prompt) if prompt else "",
            )
        )
    return topics


class NotebooksAPI:
    """Operations on NotebookLM notebooks.

    Provides methods for listing, creating, getting, deleting, and renaming
    notebooks, as well as getting AI-generated descriptions.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            notebooks = await client.notebooks.list()
            new_nb = await client.notebooks.create("My Research")
            await client.notebooks.rename(new_nb.id, "Better Title")
    """

    def __init__(
        self,
        rpc: RpcCaller,
        sources_api: NotebookSourceLister | None = None,
        *,
        metadata_service: NotebookMetadataService | None = None,
        share_manager: ShareManager | None = None,
    ) -> None:
        """Initialize the notebooks API.

        Args:
            rpc: RPC dispatch surface (typically the shared client session).
            sources_api: Optional source lister for cross-API metadata composition.
            metadata_service: Optional explicit metadata service for tests or advanced wiring.
            share_manager: Optional explicit legacy share manager for tests or advanced wiring.
        """
        self._rpc = rpc
        self._sources = sources_api or create_default_source_lister(self._rpc)
        self._metadata_service = metadata_service or NotebookMetadataService(
            # Keep notebook lookup late-bound so tests and advanced callers that
            # replace ``api.get`` after construction still affect get_metadata().
            get_notebook=lambda notebook_id: self.get(notebook_id),
            source_lister=self._sources,
        )
        self._share_manager = share_manager or ShareManager(self._rpc)

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
        _is_retry: bool = False,
        *,
        disable_internal_retries: bool = False,
        operation_variant: str | None = None,
    ) -> Any:
        """Delegate through the current RPC caller for late-bound overrides."""
        return await self._rpc.rpc_call(
            method,
            params,
            source_path=source_path,
            allow_null=allow_null,
            _is_retry=_is_retry,
            disable_internal_retries=disable_internal_retries,
            operation_variant=operation_variant,
        )

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        """Extract all source IDs from a notebook.

        Fetches notebook data and extracts source IDs for use with chat and
        artifact generation when targeting specific sources.

        Args:
            notebook_id: The notebook ID.

        Returns:
            List of source IDs. Empty list when the notebook has no sources or
            when get_source_ids encounters a schema/validation mismatch while
            extracting IDs.

        Note:
            RPC, auth, and network errors raised by ``get_raw()`` propagate to
            the caller; only local source-shape validation failures are caught
            below and converted to an empty list. Per-row id-envelope
            decoding (including the drive-backed ``[None, True, [id]]``
            shape) is delegated to
            :class:`notebooklm._row_adapters.sources.SourceRow`; this method only
            performs the envelope walk down to ``notebook[0][1]``.
        """
        notebook_data = await self.get_raw(notebook_id)

        source_ids: list[str] = []
        if not notebook_data or not isinstance(notebook_data, list):
            return source_ids

        # Schema-drift detection points: log WARNING at each isinstance/len
        # guard that fails on a non-empty response (real drift surfaces here,
        # not at the safety-net except below).
        # ``notebook_data`` is a non-empty list here (guarded above), so the
        # ``[0]`` read cannot fail; the ``[1]`` read below is gated by
        # ``len(notebook_info) > 1``. Both descents route through ``safe_index``
        # — the sanctioned schema-drift seam — so position knowledge stays out
        # of open-coded subscripts. The reads are all length-guarded, so
        # ``safe_index`` never actually raises here; the ``except`` below remains
        # defense-in-depth (now genuinely unreachable, as noted).
        method_id = RPCMethod.GET_NOTEBOOK.value
        try:
            notebook_info = safe_index(
                notebook_data, 0, method_id=method_id, source="NotebooksAPI.get_source_ids"
            )
            if not isinstance(notebook_info, list):
                # notebook_data is already known to be a non-empty list here
                # (guarded by `if not notebook_data` above).
                logger.warning(
                    "get_source_ids: notebook_data[0] shape unexpected for %s "
                    "(schema drift?). top-type=%s",
                    notebook_id,
                    type(notebook_info).__name__,
                )
                return source_ids

            sources = (
                safe_index(
                    notebook_info, 1, method_id=method_id, source="NotebooksAPI.get_source_ids"
                )
                if len(notebook_info) > 1
                else None
            )
            if not isinstance(sources, list):
                logger.warning(
                    "get_source_ids: notebook_info[1] not list for %s (schema drift?). len=%d",
                    notebook_id,
                    len(notebook_info),
                )
                return source_ids
            for source in sources:
                if not (isinstance(source, list) and source):
                    continue
                # Per-row id-envelope decoding is delegated to SourceRow:
                # ``SourceRow.id`` returns ``""`` for malformed envelopes
                # (matching legacy ``isinstance(first, list) and first``)
                # and stringifies non-string ids. The legacy code here
                # additionally required ``isinstance(sid, str)``; that
                # check was inconsistent with the sibling
                # ``_source.listing._extract_source_id`` path (which
                # accepts any non-None id via ``str(src_id)`` at the
                # ``Source(id=...)`` boundary). Unifying both call sites
                # through ``SourceRow.id`` aligns behavior — integer-ids
                # (none observed in Google's wire today) would now be
                # stringified rather than silently dropped.
                row = SourceRow.from_entry(source, method_id=RPCMethod.GET_NOTEBOOK.value)
                sid = row.id
                if sid:
                    source_ids.append(sid)
        except (IndexError, TypeError) as e:
            # Defense-in-depth: guards above should make this unreachable.
            logger.warning(
                "get_source_ids: unexpected exception despite guards for %s: %s",
                notebook_id,
                e,
                exc_info=True,
            )

        return source_ids

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: list[str] | None = None,
        mode: int = _PROMPT_SUGGESTIONS_DEFAULT_MODE,
        query: str | None = None,
    ) -> list[PromptSuggestion]:
        """Get AI-suggested prompts for a notebook.

        Backed by ``GeneratePromptSuggestions`` (``otmP3b``): a *general*
        notebook-prompt endpoint whose ``mode`` selects the product surface to
        suggest for. With the default ``mode=4`` the server suggests chat
        questions to ask :meth:`ChatAPI.ask`; other modes target other surfaces
        (critique, audio/debate, quiz, flashcards). The server returns a short
        list of ``{title, prompt}`` suggestions, each ``prompt`` a ready-to-send
        multi-line instruction.

        Args:
            notebook_id: The notebook to suggest prompts for.
            source_ids: Source ids to scope the suggestions to. ``None``
                (default) uses **all** of the notebook's sources.
            mode: The required ``C0`` int "mode/surface" enum, inclusive range
                ``1..9`` (``0`` / omitted makes the server return ``INTERNAL``).
                The suggestions are LLM-generated (non-deterministic) but their
                *framing* is a stable function of ``mode``: it selects the product
                surface the prompts are written for. Default ``4`` = general "ask
                about the content" questions (the web chat surface's own default);
                ``5`` = critique/evaluate; ``6`` = audio/debate; ``8`` = quiz;
                ``9`` = flashcards; ``1-3`` and ``7`` track ``4``. Stays a plain
                int, not a named enum, since the bundle exposes the values but not
                Google's member names. See ``_PROMPT_SUGGESTIONS_DEFAULT_MODE`` for
                the full bundle + live-probe table.
            query: Optional free-text steer for the kind of prompts to suggest.
                An empty / whitespace-only string is treated as no steer.

        Returns:
            A list of :class:`~notebooklm.types.PromptSuggestion`. An empty /
            degenerate server response yields ``[]`` (suggestions are
            best-effort UI sugar — an absent payload does not raise).

        Raises:
            ValidationError: if ``mode`` is outside the inclusive ``1..9`` range
                (caught before any network call, so a bad mode never costs an
                RPC).

        .. versionadded:: 0.8.0
        """
        logger.debug("Suggesting prompts for notebook %s (mode=%d)", notebook_id, mode)
        # Validate the mode up front (before the source-id fetch) so a bad value
        # fails fast without a wasted round-trip; the builder's ValueError is
        # re-raised as the public ValidationError for a uniform error contract.
        try:
            build_prompt_suggestions_params(notebook_id, [], mode=mode)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if source_ids is None:
            source_ids = await self.get_source_ids(notebook_id)

        params = build_prompt_suggestions_params(notebook_id, source_ids, mode=mode, query=query)
        result = await self._rpc.rpc_call(
            RPCMethod.SUGGEST_PROMPTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        rows = unwrap_prompt_suggestions(result, source="suggest_prompts")
        # ``is_well_formed`` only gates on row LENGTH (>= 2 slots), not on the
        # field values, mirroring ``ReportSuggestionRow``: a length-ok row whose
        # title/prompt degrade to "" (a non-string leaf) still maps to a
        # ``PromptSuggestion("", "")``. Real traffic always carries string
        # leaves, so this is a best-effort tolerance for a degenerate server
        # payload, not an expected output — callers should not treat an empty
        # title/prompt as meaningful.
        return [
            PromptSuggestion(title=row.title, prompt=row.prompt)
            for row in map(PromptSuggestionRow, rows)
            if row.is_well_formed
        ]

    async def list(self) -> list[Notebook]:
        """List notebooks (most-recently-viewed first).

        .. note::
            The backing RPC is ``ListRecentlyViewedProjects`` — results are
            ordered most-recently-viewed first (live-observed). It is not
            independently confirmed whether this can ever omit an *owned*
            notebook; in practice it matches the set shown on the NotebookLM
            home page.

        Returns:
            List of Notebook objects.
        """
        logger.debug("Listing notebooks")
        params = [None, 1, None, [2]]
        result = await self._rpc.rpc_call(RPCMethod.LIST_NOTEBOOKS, params)

        # LIST_NOTEBOOKS responses arrive as a single-element envelope whose
        # first element is the notebook-row list (``[[row1, row2, ...]]``).
        # The wrap probe mirrors the fail-loud dispatch in
        # ``_artifact/listing.py::list_raw``: an empty/``None`` payload and a
        # ``None`` row-list slot are legitimate "no notebooks" shapes (soft
        # ``[]``), while a truthy payload that doesn't match the envelope — a
        # non-list payload, or a truthy non-list where the row list belongs —
        # is schema drift and raises ``DecodingError`` instead of flowing
        # garbage rows into ``Notebook.from_api_response`` (which would
        # silently fabricate empty-id notebooks).
        if not result:
            return []
        if isinstance(result, list):
            # ``result`` is a non-empty list here (guarded above), so this ``[0]``
            # read cannot fail; ``safe_index`` keeps the envelope-unwrap position
            # knowledge on the sanctioned schema-drift seam.
            raw_notebooks = safe_index(
                result, 0, method_id=RPCMethod.LIST_NOTEBOOKS.value, source="NotebooksAPI.list"
            )
            if isinstance(raw_notebooks, list):
                return [Notebook.from_api_response(nb) for nb in raw_notebooks]
            if raw_notebooks is None:
                return []
        raise DecodingError(
            "Unrecognized LIST_NOTEBOOKS payload shape",
            # reprlib bounds the preview without materialising the full repr
            # of a large/deep payload (mirrors safe_index's own truncation).
            raw_response=reprlib.repr(result),
            method_id=RPCMethod.LIST_NOTEBOOKS.value,
        )

    async def create(self, title: str) -> Notebook:
        """Create a new notebook.

        Args:
            title: The title for the new notebook.

        Returns:
            The created Notebook object.

        Idempotency:
            Wraps the underlying CREATE_NOTEBOOK RPC in a
            probe-then-retry loop. On a transient transport failure
            (5xx / 429 / network), the wrapper lists notebooks and
            checks whether a new notebook with the requested title
            appeared since the call started. If exactly one match is
            found, that notebook is returned without re-issuing the
            create. If zero matches, the create is retried. If more
            than one matches, the wrapper raises an :class:`RPCError`
            because the situation is ambiguous (concurrent creates by
            other clients) and the caller must intervene.
        """
        logger.debug("Creating notebook: %s", title)
        params = build_create_notebook_params(title)

        # Capture the baseline notebook IDs *before* the create so the
        # probe can distinguish a notebook that landed during this
        # call from a pre-existing notebook with the same title. The
        # baseline is best-effort — if listing fails (e.g. transient
        # 5xx), we fall back to an empty baseline so a brand-new
        # account behaves correctly.
        #
        # Edge case: when the baseline fetch fails AND a pre-existing
        # notebook with the same title already exists, the probe cannot
        # tell that notebook apart from one that just landed. The
        # ambiguous-probe guard only fires when >1 matches appear, so
        # a single pre-existing same-titled notebook would be returned
        # as if it were freshly created. This is a doubly-exceptional
        # scenario (baseline list failure + title collision) and is
        # accepted as a known limitation; callers needing strict
        # uniqueness should embed a UUID in the title.
        try:
            baseline_ids = {nb.id for nb in await self.list()}
        except Exception:
            logger.debug(
                "create: baseline list() failed; falling back to empty baseline",
                exc_info=True,
            )
            baseline_ids = set()

        async def _create() -> Notebook:
            try:
                result = await self._rpc.rpc_call(
                    RPCMethod.CREATE_NOTEBOOK,
                    params,
                    disable_internal_retries=True,
                )
            except RPCError as exc:
                await self._raise_quota_error_if_detected(exc)
                raise
            notebook = Notebook.from_api_response(result)
            logger.debug("Created notebook: %s", notebook.id)
            return notebook

        async def _probe() -> Notebook | None:
            # Transport- and auth-level errors during the probe MUST
            # propagate: the original create may have committed
            # server-side and we have no way to confirm. Silently
            # returning None would let ``idempotent_create`` re-issue the
            # create on the next attempt and duplicate the notebook.
            # Surfacing the transport error keeps the caller in control —
            # they can decide whether to re-probe later (e.g. once
            # connectivity recovers) before retrying the create.
            #
            # Other exception types (decoding errors, unexpected RPC
            # failures, programming bugs) are still treated as "probe
            # could not confirm a match" — those signal that the probe
            # path itself is broken in a way that wouldn't be fixed by a
            # retry, so falling through to None preserves the existing
            # contract of "best-effort probe".
            try:
                current = await self.list()
            except (AuthError, RateLimitError, ServerError, NetworkError):
                # Transport- and auth-level probe failures must propagate.
                # Silently returning None here lets ``idempotent_create``
                # re-issue the create on top of a broken probe, which is
                # exactly the duplicate-resource bug we are guarding against.
                logger.warning(
                    "create: probe list() failed with transport/auth error; "
                    "propagating so the caller can avoid a duplicate-resource retry"
                )
                raise
            except Exception:
                logger.debug(
                    "create: probe list() failed with non-transport error; treating as no match",
                    exc_info=True,
                )
                return None
            matches = [nb for nb in current if nb.id not in baseline_ids and nb.title == title]
            if len(matches) == 1:
                # ``matches`` is a list of typed ``Notebook`` objects (NOT a raw
                # RPC payload) — tuple unpacking reads the single match
                # without the ``name[int]`` shape that the positional-decode gate
                # (rightly) flags only for genuine payload descents.
                (match,) = matches  # exactly one (len==1 guard); unpack avoids name[int]
                return match
            if len(matches) > 1:
                # Ambiguous: more than one new notebook with this title
                # appeared during the call. We cannot safely pick one;
                # surface the situation so the caller can resolve it.
                raise RPCError(
                    f"Cannot disambiguate notebook with title {title!r}: "
                    f"probe found {len(matches)} new notebooks with this title "
                    "after a transport failure. Resolve manually before retrying.",
                    method_id=RPCMethod.CREATE_NOTEBOOK.value,
                )
            return None

        return await idempotent_create(
            _create,
            _probe,
            label=f"notebooks.create[{title!r}]",
        )

    async def _raise_quota_error_if_detected(self, error: RPCError) -> None:
        """Convert CREATE_NOTEBOOK invalid-argument failures into quota errors."""
        if (
            error.method_id != RPCMethod.CREATE_NOTEBOOK.value
            or error.rpc_code != CREATE_NOTEBOOK_QUOTA_RPC_CODE
        ):
            return

        # The backend reports quota exhaustion as code 3 rather than a typed
        # limit error, so verify against the account's advertised limit before
        # changing the exception type.
        try:
            account_limits = await self._get_account_limits()
        except Exception:
            logger.debug(
                "Could not fetch account limits after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return

        notebook_limit = account_limits.notebook_limit
        if notebook_limit is None:
            return

        try:
            notebooks = await self.list()
        except Exception:
            logger.debug(
                "Could not list notebooks after CREATE_NOTEBOOK failure; "
                "leaving original RPC error unchanged",
                exc_info=True,
            )
            return

        owned_count = sum(1 for notebook in notebooks if notebook.is_owner)
        # Allow one notebook of slack because list results can lag a failed
        # create or omit service-internal notebooks that still count.
        if owned_count < max(notebook_limit - 1, 0):
            return

        raise NotebookLimitError(
            owned_count,
            limit=notebook_limit,
            original_error=error,
        ) from error

    async def _get_account_limits(self) -> AccountLimits:
        """Fetch NotebookLM account limits from user settings."""
        result = await self._rpc.rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            build_get_user_settings_params(),
            source_path="/",
        )
        return extract_account_limits(result)

    async def get(self, notebook_id: str) -> Notebook:
        """Get notebook details.

        Args:
            notebook_id: The notebook ID.

        Returns:
            Notebook object with details.

        Raises:
            NotebookNotFoundError: If the notebook does not exist. The backend
                returns an empty / degenerate payload (missing ``id`` and
                ``title``) for unknown IDs rather than a proper RPC error, so
                this method post-validates the parsed response.
        """
        params = build_get_notebook_params(notebook_id)
        result = await self._rpc.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        # get_notebook returns [nb_info, ...] where nb_info contains the notebook
        # data. The ``[0]`` read is fully guarded (truthy + list + non-empty), so
        # ``safe_index`` cannot raise here; it keeps the envelope-unwrap position
        # on the sanctioned schema-drift seam.
        nb_info = (
            safe_index(result, 0, method_id=RPCMethod.GET_NOTEBOOK.value, source="NotebooksAPI.get")
            if result and isinstance(result, list) and len(result) > 0
            else []
        )
        # Guard the empty-payload case BEFORE parsing. ``Notebook.from_api_response``
        # currently tolerates ``[]`` but a future tightening could turn that into
        # an ``IndexError`` that would surface as a confusing crash instead of
        # the intended ``NotebookNotFoundError``. Raising here keeps the contract
        # stable regardless of how the parser evolves.
        if not nb_info:
            raise NotebookNotFoundError(
                notebook_id,
                method_id=RPCMethod.GET_NOTEBOOK.value,
            )
        notebook = Notebook.from_api_response(nb_info)
        # Defense-in-depth: even when the outer list isn't empty, the server can
        # return a payload whose id and title both parse to ``""``. A valid
        # notebook always has at least one of the two populated.
        if not notebook.id and not notebook.title:
            raise NotebookNotFoundError(
                notebook_id,
                method_id=RPCMethod.GET_NOTEBOOK.value,
            )
        return notebook

    async def get_or_none(self, notebook_id: str) -> Notebook | None:
        """Get notebook details, returning ``None`` when it does not exist.

        The sanctioned ``None``-on-miss lookup (ADR-0019): a companion to
        :meth:`get`, which raises :class:`~notebooklm.exceptions.NotebookNotFoundError`
        on a miss. This catches *only* that genuine-absence signal and returns
        ``None``; transport, auth, and decode faults — including the broader
        :class:`~notebooklm.exceptions.RPCError` subtree
        :class:`NotebookNotFoundError` also inherits — propagate unchanged.

        Args:
            notebook_id: The notebook ID.

        Returns:
            The :class:`~notebooklm.types.Notebook`, or ``None`` if not found.
        """
        try:
            return await self.get(notebook_id)
        except NotebookNotFoundError:
            return None

    async def delete(self, notebook_id: str) -> None:
        """Delete a notebook.

        Idempotent: deleting an already-absent notebook succeeds (returns
        ``None``) and never raises ``NotebookNotFoundError``. Real failures
        (``403``/``5xx``/auth/transport) still propagate.

        Args:
            notebook_id: The notebook ID to delete.

        .. versionchanged:: 0.7.0
            **Breaking change:** previously returned a hardcoded ``True``;
            now returns ``None`` (issue #1211). ``if await notebooks.delete(...):``
            no longer enters its block.
        """
        logger.debug("Deleting notebook: %s", notebook_id)
        # DELETE_NOTEBOOK is the live ``DeleteProjects`` (batch-capable: the
        # leading slot is a list of ids); we delete a single notebook per call.
        params = [[notebook_id], [2]]
        await self._rpc.rpc_call(RPCMethod.DELETE_NOTEBOOK, params)

    async def rename(self, notebook_id: str, new_title: str) -> Notebook:
        """Rename a notebook.

        Args:
            notebook_id: The notebook ID.
            new_title: The new title for the notebook.

        Returns:
            The renamed Notebook object (fetched after rename).
        """
        logger.debug("Renaming notebook %s to: %s", notebook_id, new_title)
        # RENAME_NOTEBOOK is the live ``MutateProject``, a generic notebook
        # mutator: the same RPC sets the title here, chat config in
        # ``ChatAPI.configure``, and the share view-level in
        # ``SharingAPI.set_view_level`` — each with a different params shape.
        # Payload format discovered via browser traffic capture:
        # [notebook_id, [[null, null, null, [null, new_title]]]]
        params = [notebook_id, [[None, None, None, [None, new_title]]]]
        await self._rpc.rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            params,
            source_path="/",  # Home page context, not notebook page
            allow_null=True,
        )
        # Fetch and return the updated notebook
        return await self.get(notebook_id)

    async def get_summary(self, notebook_id: str) -> str:
        """Get raw summary text for a notebook.

        For parsed summary with topics, use get_description() instead.

        Args:
            notebook_id: The notebook ID.

        Returns:
            Raw summary text string.
        """
        params = [notebook_id, [2]]
        result = await self._rpc.rpc_call(
            RPCMethod.SUMMARIZE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        # Response structure: [[[summary_string, ...], topics, ...]]. ``result[0]``
        # is the ``outer`` payload that ``_extract_summary`` descends, so delegate
        # to it: empty/None/null-slot → "" and present-but-malformed → drift,
        # identically to ``get_description`` (single source of truth — #1485).
        if not isinstance(result, list) or not result:
            return ""
        # ``result`` is a non-empty list here; ``safe_index`` keeps the
        # envelope-unwrap position on the schema-drift seam (cannot raise here).
        return _extract_summary(
            safe_index(
                result, 0, method_id=RPCMethod.SUMMARIZE.value, source="NotebooksAPI.get_summary"
            )
        )

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        """Get AI-generated summary and suggested topics for a notebook.

        This provides a high-level overview of what the notebook contains,
        similar to what's shown in the Chat panel when opening a notebook.

        .. note::
            The backing RPC is ``GenerateNotebookGuide`` — it produces the
            notebook *guide*: a short summary plus suggested starter questions
            (each ``SuggestedTopic`` carries the question and its chat prompt),
            rather than a freeform summary alone.

        Args:
            notebook_id: The notebook ID.

        Returns:
            NotebookDescription with summary and suggested topics.

        Example:
            desc = await client.notebooks.get_description(notebook_id)
            print(desc.summary)
            for topic in desc.suggested_topics:
                print(f"Q: {topic.question}")
        """
        # Get raw summary data
        params = [notebook_id, [2]]
        result = await self._rpc.rpc_call(
            RPCMethod.SUMMARIZE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        summary = ""
        suggested_topics: list[SuggestedTopic] = []

        # Response structure: [[[summary_string], [[topics]], ...]]
        # Summary is at result[0][0][0], topics at result[0][1][0].
        # The outer descent and per-slot extraction live in named helpers
        # (`_extract_summary` / `_extract_suggested_topics`) so the deep
        # index access stays auditable when Google's shape drifts.
        if result and isinstance(result, list) and len(result) > 0:
            # ``result`` is a non-empty list here (guarded); ``safe_index`` keeps
            # the envelope-unwrap position on the schema-drift seam.
            outer = safe_index(
                result,
                0,
                method_id=RPCMethod.SUMMARIZE.value,
                source="NotebooksAPI.get_description",
            )
            summary = _extract_summary(outer)
            suggested_topics = _extract_suggested_topics(outer)

        return NotebookDescription(summary=summary, suggested_topics=suggested_topics)

    async def remove_from_recent(self, notebook_id: str) -> None:
        """Remove a notebook from the recently viewed list.

        Args:
            notebook_id: The notebook ID to remove from recent.
        """
        params = [notebook_id]
        await self._rpc.rpc_call(
            RPCMethod.REMOVE_RECENTLY_VIEWED,
            params,
            allow_null=True,
        )

    async def get_raw(self, notebook_id: str) -> Any:
        """Get raw notebook data from API.

        This returns the raw API response, useful for accessing data
        not parsed into the Notebook dataclass (like sources list).

        Args:
            notebook_id: The notebook ID.

        Returns:
            Raw API response data.
        """
        params = build_get_notebook_params(notebook_id)
        return await self._rpc.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        """Get share URL for a notebook or artifact.

        This does NOT toggle sharing - it just returns the URL format.
        Use :meth:`SharingAPI.set_public` (``client.sharing.set_public``) to
        enable/disable sharing.

        Args:
            notebook_id: The notebook ID.
            artifact_id: Optional artifact ID for a deep-link URL.

        Returns:
            The share URL string.
        """
        return self._share_manager.get_share_url(notebook_id, artifact_id)

    async def get_metadata(self, notebook_id: str) -> NotebookMetadata:
        """Get notebook metadata with sources list.

        This combines notebook details with a simplified sources list,
        useful for export/overview of notebook contents.

        Uses asyncio.gather to fetch notebook and sources concurrently
        for better performance.

        Args:
            notebook_id: The notebook ID.

        Returns:
            NotebookMetadata with notebook details and simplified sources list.

        Example:
            metadata = await client.notebooks.get_metadata(notebook_id)
            print(f"Notebook: {metadata.title}")
            print(f"Sources: {len(metadata.sources)}")
            # Export to JSON
            import json
            print(json.dumps(metadata.to_dict(), indent=2))
        """
        return await self._metadata_service.get_metadata(notebook_id)
