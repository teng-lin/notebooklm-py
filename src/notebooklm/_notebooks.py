"""Notebook operations API."""

import logging
from typing import Any

from ._backend import BackendAdapter, BackendError
from ._deadline import RuntimeDeadlineFactory
from ._notebook_guide_service import NotebookGuideService
from ._notebook_metadata import (
    NotebookMetadataService,
    NotebookSourceLister,
)
from ._notebook_mutation_service import NotebookMutationService
from ._notebook_payloads import build_create_notebook_params as build_create_notebook_params
from ._read_services import NotebookReadService, SourceReadService
from ._semantic.compat import project_backend_call, project_backend_error
from ._semantic.projectors import (
    project_notebook,
    project_notebook_description,
    project_prompt_suggestions,
    project_source,
)
from ._sharing_manager import ShareManager
from ._suggestion_service import PROMPT_SUGGESTIONS_DEFAULT_MODE, SuggestionService
from .exceptions import (
    ClientError,
    NotebookNotFoundError,
)
from .rpc import GrpcStatusCode, RPCMethod, normalize_grpc_status
from .types import (
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    PromptSuggestion,
    Source,
)

logger = logging.getLogger(__name__)


class _SemanticSourceLister:
    """Adapt the neutral source read service to the public lister protocol.

    ``SourceReadService`` returns :class:`~notebooklm._semantic.records.SourceRecord`
    values; :class:`NotebookSourceLister` — shared with the injected
    :class:`SourcesAPI` — is a public-model contract. Projection is a facade
    responsibility, so the notebook facade owns it here for the
    direct-construction path that has no injected ``sources_api``.
    """

    __slots__ = ("_service",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._service = SourceReadService(backend)

    async def list(self, notebook_id: str, *, strict: bool = False) -> list[Source]:
        """List one notebook's sources as public models."""
        records = await self._service.list(notebook_id, strict=strict)
        return [project_source(record) for record in records]


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
        sources_api: NotebookSourceLister | None = None,
        *,
        metadata_service: NotebookMetadataService | None = None,
        share_manager: ShareManager | None = None,
        _backend: BackendAdapter | None = None,
        _deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        """Initialize the notebooks API.

        Args:
            sources_api: Optional source lister for cross-API metadata composition.
                When omitted alongside a semantic backend, direct construction
                uses a backend-owned ``SourceReadService``.
            metadata_service: Optional explicit metadata service for tests or advanced wiring.
            share_manager: Optional explicit legacy share manager for tests or advanced wiring.
            _backend: Private semantic backend supplied by the client composition root.
        """
        self._read_service = NotebookReadService(_backend) if _backend is not None else None
        self._mutation_service = (
            NotebookMutationService(_backend, deadline_factory=_deadline_factory)
            if _backend is not None
            else None
        )
        self._guide_service = NotebookGuideService(_backend) if _backend is not None else None
        self._suggestion_service = (
            SuggestionService(_backend, deadline_factory=_deadline_factory)
            if _backend is not None
            else None
        )
        self._sources = sources_api
        self._metadata_service: NotebookMetadataService | None
        if metadata_service is not None:
            self._metadata_service = metadata_service
        elif sources_api is not None:
            self._metadata_service = NotebookMetadataService(
                # Keep notebook lookup late-bound so tests and advanced callers that
                # replace ``api.get`` after construction still affect get_metadata().
                get_notebook=lambda notebook_id: self.get(notebook_id),
                source_lister=sources_api,
            )
        elif _backend is not None:
            self._metadata_service = NotebookMetadataService(
                # Preserve the same late-bound public seam as production
                # composition while keeping standalone metadata source reads on
                # the semantic backend rather than the legacy raw collaborator.
                get_notebook=lambda notebook_id: self.get(notebook_id),
                source_lister=_SemanticSourceLister(_backend),
            )
        else:
            self._metadata_service = None
        # Production composition injects the explicitly authorized legacy
        # manager.  The fallback preserves direct/standalone construction for
        # internal callers and tests without making the semantic facade a
        # ``RpcCaller`` consumer.
        self._share_manager = share_manager or ShareManager(_backend)
        # CREATE_NOTEBOOK volunteers its newly-created ChatSession, while
        # GET_NOTEBOOK omits it. Keep that one-shot hint until ChatAPI consumes
        # it so the first ask need not immediately re-fetch the same id through
        # hPTbtc (#2133). The cache is scoped to this client instance and each
        # entry is popped on first use; closing the client releases any hints
        # from notebooks that were created without a subsequent ask.
        self._created_chat_session_ids: dict[str, str] = {}

    def _require_read_service(self) -> NotebookReadService:
        """Return the composition-root service for the migrated read slice."""
        if self._read_service is None:
            raise RuntimeError("NotebooksAPI semantic read backend was not configured")
        return self._read_service

    def _require_mutation_service(self) -> NotebookMutationService:
        """Return the composition-root service for migrated notebook mutations."""
        if self._mutation_service is None:
            raise RuntimeError("NotebooksAPI semantic mutation backend was not configured")
        return self._mutation_service

    def _require_guide_service(self) -> NotebookGuideService:
        """Return the composition-root service for generated notebook guides."""
        if self._guide_service is None:
            raise RuntimeError("NotebooksAPI semantic guide backend was not configured")
        return self._guide_service

    def _require_suggestion_service(self) -> SuggestionService:
        """Return the composition-root service for prompt suggestions."""
        if self._suggestion_service is None:
            raise RuntimeError("NotebooksAPI semantic suggestion backend was not configured")
        return self._suggestion_service

    def _take_created_chat_session_id(self, notebook_id: str) -> str | None:
        """Consume CREATE_NOTEBOOK's volunteered current chat-session id."""
        return self._created_chat_session_ids.pop(notebook_id, None)

    async def get_source_ids(self, notebook_id: str) -> list[str]:
        """Return source IDs from one semantic notebook snapshot."""
        public_error: Exception | None = None
        try:
            return await self._require_read_service().get_source_ids(notebook_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        assert public_error is not None
        raise public_error

    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: list[str] | None = None,
        mode: int = PROMPT_SUGGESTIONS_DEFAULT_MODE,
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
                ``1..10`` (``0`` / omitted makes the server return ``INTERNAL``).
                It selects which studio surface/format the prompts are written for
                (#1726, live-verified): ``1`` audio deep-dive, ``2`` audio brief,
                ``3`` video explainer, ``4`` (default) chat "ask about the content"
                questions, ``5`` audio critique, ``6`` audio debate, ``8`` quiz,
                ``9`` flashcards, ``10`` video short (``7`` unidentified). Stays a
                plain int, not a named enum, since the bundle exposes the values
                but not Google's member names. See
                ``PROMPT_SUGGESTIONS_DEFAULT_MODE`` for the full map + method.
            query: Optional free-text steer for the kind of prompts to suggest.
                An empty / whitespace-only string is treated as no steer.

        Returns:
            A list of :class:`~notebooklm.types.PromptSuggestion`. An empty /
            degenerate server response yields ``[]`` (suggestions are
            best-effort UI sugar — an absent payload does not raise).

        Raises:
            ValidationError: if ``mode`` is outside the inclusive ``1..10`` range
                (caught before any network call, so a bad mode never costs an
                RPC).

        .. versionadded:: 0.8.0
        """
        logger.debug("Suggesting prompts for notebook %s (mode=%d)", notebook_id, mode)
        records = await project_backend_call(
            self._require_suggestion_service().suggest_prompts(
                notebook_id,
                source_ids=source_ids,
                mode=mode,
                query=query,
            )
        )
        return project_prompt_suggestions(tuple(records))

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
        public_error: Exception | None = None
        try:
            records = await self._require_read_service().list()
        except BackendError as error:
            # WebRpcBackend deliberately exposes only the neutral BackendError
            # vocabulary. At this public compatibility facade, reconstruct the
            # exact pre-migration RPC/Network exception class and its reviewed
            # structured diagnostics without reaching through ``__cause__``.
            public_error = project_backend_error(error)
        else:
            return [project_notebook(record) for record in records]
        raise public_error

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

            "Appeared since the call started" is measured against a
            pre-create snapshot of the notebook ids. If that snapshot
            could not be taken, the probe cannot attribute *any* match
            and raises :class:`RPCError` on the first one rather than
            adopting a notebook it may not have created (#2232). The
            raised error carries the ``unconfirmed`` marker; see
            docs/python-api.md#idempotency.
        """
        logger.debug("Creating notebook: %s", title)
        public_error: Exception | None = None
        try:
            record = await self._require_mutation_service().create(title)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            if record.id and record.chat_sessions:
                self._created_chat_session_ids[record.id] = record.chat_sessions[0].id
            logger.debug("Created notebook: %s", record.id)
            return project_notebook(record)
        # Raise outside the private BackendError catch frame so a reviewed
        # reconstructed quota cause/context graph remains the public graph.
        assert public_error is not None
        raise public_error

    async def get(self, notebook_id: str) -> Notebook:
        """Get notebook details.

        Args:
            notebook_id: The notebook ID.

        Returns:
            Notebook object with details.

        Raises:
            NotebookNotFoundError: If the notebook does not exist. Both backend
                signals are handled, so the ADR-0019 contract holds either way:
                a proper RPC error (gRPC status ``5``, surfaced by the decoder
                as ``ClientError`` and translated below), or the historical
                empty / degenerate payload with no RPC error at all, which the
                post-validation further down still catches.
        """
        public_error: Exception | None = None
        notebook: Notebook | None = None
        try:
            record = await self._require_read_service().get(notebook_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            notebook = None if record is None else project_notebook(record)

        if isinstance(public_error, ClientError):
            # Translate the status-5 rejection into this method's documented
            # miss signal: ``ClientError`` and ``NotebookNotFoundError`` are
            # siblings under ``RPCError``, not ancestor/descendant, so
            # ``get_or_none``'s ``except`` never sees it (#2132, ADR-0019).
            # Narrow on purpose -- ``PERMISSION_DENIED`` comes through this
            # same branch and must keep propagating.
            #
            # ``detail`` carries the decoder's guidance onto the typed error
            # rather than leaving it on ``__cause__``: status 5 also means
            # "belongs to a different signed-in account" (#114 / #294),
            # ``server/_errors.py`` promises the 404 body keeps that verbatim,
            # and every adapter renders ``str(exc)``.
            if normalize_grpc_status(public_error.rpc_code) is GrpcStatusCode.NOT_FOUND:
                raise NotebookNotFoundError(
                    notebook_id,
                    method_id=RPCMethod.GET_NOTEBOOK.value,
                    raw_response=public_error.raw_response,
                    rpc_code=public_error.rpc_code,
                    found_ids=public_error.found_ids,
                    detail=str(public_error),
                ) from public_error
        if public_error is not None:
            raise public_error
        if notebook is None:
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

        Status-5 policy: **both** its meanings collapse to ``None`` here. The
        backend sends that one status whether the notebook is absent or lives
        under a *different* signed-in account (#114 / #294), so the
        account-routing guidance is unobservable on this API by construction.
        Use :meth:`get` when that matters — it raises with the guidance in the
        message, the ``rpc_code``, and the reconstructed rejection as ``__cause__``.
        ``PERMISSION_DENIED`` is folded in neither place.

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
        public_error: Exception | None = None
        try:
            await self._require_mutation_service().delete(notebook_id)
            return
        except BackendError as error:
            public_error = project_backend_error(error)
        assert public_error is not None
        raise public_error

    async def rename(self, notebook_id: str, new_title: str) -> Notebook:
        """Rename a notebook.

        Args:
            notebook_id: The notebook ID.
            new_title: The new title for the notebook.

        Returns:
            The renamed Notebook object (fetched after rename).
        """
        return await self.update(notebook_id, title=new_title)

    async def set_emoji(self, notebook_id: str, emoji: str) -> Notebook:
        """Set a notebook's display emoji and return the refreshed notebook."""
        return await self.update(notebook_id, emoji=emoji)

    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        """Update a notebook's title and/or emoji in one mutation.

        ``None`` means preserve the existing property; an empty string is sent
        verbatim and can therefore clear the emoji. At least one property must
        be supplied.
        """
        logger.debug("Updating notebook %s (title=%r, emoji=%r)", notebook_id, title, emoji)
        public_error: Exception | None = None
        try:
            record = await self._require_mutation_service().update(
                notebook_id,
                title=title,
                emoji=emoji,
            )
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return project_notebook(record)
        assert public_error is not None
        raise public_error

    async def get_summary(self, notebook_id: str) -> str:
        """Get raw summary text for a notebook."""
        public_error: Exception | None = None
        try:
            return await self._require_guide_service().get_summary(notebook_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        assert public_error is not None
        raise public_error

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        """Get AI-generated summary and suggested topics for a notebook."""
        public_error: Exception | None = None
        try:
            record = await self._require_guide_service().get_description(notebook_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return project_notebook_description(record)
        assert public_error is not None
        raise public_error

    async def remove_from_recent(self, notebook_id: str) -> None:
        """Remove a notebook from the recently viewed list."""
        public_error: Exception | None = None
        try:
            await self._require_mutation_service().remove_from_recent(notebook_id)
            return
        except BackendError as error:
            public_error = project_backend_error(error)
        assert public_error is not None
        raise public_error

    async def get_raw(self, notebook_id: str) -> Any:
        """Get raw notebook data from API.

        This returns the raw API response, useful for accessing data
        not parsed into the Notebook dataclass (like sources list).

        Args:
            notebook_id: The notebook ID.

        Returns:
            Raw API response data.
        """
        public_error: Exception | None = None
        try:
            result = await self._require_read_service().get_raw(notebook_id)
        except BackendError as error:
            public_error = project_backend_error(error)
        else:
            return result.raw
        assert public_error is not None
        raise public_error

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
        if self._metadata_service is None:
            raise RuntimeError(
                "NotebooksAPI.get_metadata requires a semantic backend or an injected source lister"
            )
        return await self._metadata_service.get_metadata(notebook_id)
