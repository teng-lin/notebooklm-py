"""Backend-neutral notebook operations API."""

import builtins
import contextlib
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Any, Literal
from urllib.parse import quote

from ._env import get_base_url
from ._idempotency import (
    OperationJournal,
    bind_operation_journal_entries,
    call_unconfirmed_on_transport_loss,
    unresolved_commit_error,
)
from ._notebook_metadata import NotebookMetadataService, NotebookSourceLister, SpawnChild
from ._runtime.call_supervisor import OperationLease
from .exceptions import (
    NetworkError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    ServerError,
    ValidationError,
)
from .types import (
    NextStepSuggestion,
    Notebook,
    NotebookDescription,
    NotebookMetadata,
    PromptSuggestion,
)

logger = logging.getLogger(__name__)


ShareUrlBuilder = Callable[[str, str | None], str]
_CopyFailureChain = Literal["explicit", "suppress"]


def build_share_url(base_url: str, notebook_id: str, artifact_id: str | None = None) -> str:
    """Build the legacy NotebookLM notebook or artifact share URL.

    Both IDs are percent-encoded with ``safe=""`` so reserved characters
    (``/``, ``?``, ``&``, ``#``) and whitespace cannot escape the path /
    query position and rewrite the URL into another endpoint.
    """
    notebook_url = f"{base_url}/notebook/{quote(notebook_id, safe='')}"
    if artifact_id:
        return f"{notebook_url}?artifactId={quote(artifact_id, safe='')}"
    return notebook_url


def _build_default_share_url(notebook_id: str, artifact_id: str | None = None) -> str:
    """Build a share URL from the base URL resolved at call time."""
    return build_share_url(get_base_url(), notebook_id, artifact_id)


class NotebooksAPI(ABC):
    """Backend-neutral operations on NotebookLM notebooks.

    The public namespace class owns transport-independent orchestration. Concrete
    backends implement each one-call operation and the create/copy send hooks.
    """

    _create_method_id: str
    _copy_method_id: str
    _copy_failure_chain: _CopyFailureChain

    @abstractmethod
    def _operation_scope(
        self, label: str
    ) -> contextlib.AbstractAsyncContextManager[OperationLease | None]:
        """Return the backend's scope for one multi-call workflow."""
        raise NotImplementedError

    def __init__(
        self,
        sources_api: NotebookSourceLister,
        *,
        spawn_child: SpawnChild,
        metadata_service: NotebookMetadataService | None = None,
        share_url_builder: ShareUrlBuilder = _build_default_share_url,
    ) -> None:
        """Initialize transport-neutral notebook state.

        Args:
            sources_api: Source lister for cross-API metadata composition.
            metadata_service: Optional explicit metadata service for tests or advanced wiring.
            share_url_builder: Synchronous notebook/artifact share URL builder.
        """
        self._sources = sources_api
        self._metadata_service = metadata_service or NotebookMetadataService(
            # Keep notebook lookup late-bound so tests and advanced callers that
            # replace ``api.get`` after construction still affect get_metadata().
            get_notebook=lambda notebook_id: self.get(notebook_id),
            source_lister=self._sources,
            spawn_child=spawn_child,
        )
        self._share_url_builder = share_url_builder
        # CREATE_NOTEBOOK/COPY_PROJECT may volunteer a chat-session id that
        # ChatAPI consumes once before falling back to a backend read. Producers
        # and any eviction/cleanup policy stay backend-owned.
        self._created_chat_session_ids: dict[str, str] = {}

    def _take_created_chat_session_id(self, notebook_id: str) -> str | None:
        """Consume a create/copy response's volunteered chat-session id once."""
        return self._created_chat_session_ids.pop(notebook_id, None)

    @abstractmethod
    async def get_source_ids(self, notebook_id: str) -> builtins.list[str]:
        """Return all source IDs in a notebook."""

    @abstractmethod
    async def suggest_prompts(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
        mode: int = 4,
        query: str | None = None,
    ) -> builtins.list[PromptSuggestion]:
        """Return AI-suggested prompts for a notebook."""

    @abstractmethod
    async def suggest_next_steps(
        self,
        notebook_id: str,
        *,
        source_ids: builtins.list[str] | None = None,
    ) -> builtins.list[NextStepSuggestion]:
        """Return grounded follow-up questions for a notebook (``NextStepSuggestions``).

        The standalone form of the ``next_steps`` block a chat answer carries.
        ``source_ids=None`` scopes to every source in the notebook.
        """

    @abstractmethod
    async def list(self) -> builtins.list[Notebook]:
        """List notebooks in backend-defined recent-first order."""

    async def create(self, title: str) -> Notebook:
        """Create a new notebook.

        Args:
            title: The title for the new notebook.

        Returns:
            The created Notebook object.

        Safety:
            Sends the create exactly once. If the response is lost, the original
            exception is preserved and marked unconfirmed; title matches from a
            later list are not proof that this call created a notebook.
        """
        async with self._operation_scope("notebooks.create"):
            logger.debug("Creating notebook: %s", title)
            entry = OperationJournal("notebooks.create").new_entry(method=self._create_method_id)

            async def _create() -> Notebook:
                with bind_operation_journal_entries(entry):
                    return await self._send_create(title)

            return await call_unconfirmed_on_transport_loss(
                _create,
                method=self._create_method_id,
                what="the notebook create",
                journal_entry=entry,
            )

    @abstractmethod
    async def _send_create(self, title: str) -> Notebook:
        """Send one backend create operation and decode the notebook."""

    async def copy(self, notebook_id: str, title: str) -> Notebook:
        """Copy a notebook, including its sources and Studio artifacts.

        ``CopyProject`` has no caller-provided idempotency token. Internal
        transport retries are disabled so a lost response cannot create a
        second copy. If the call fails after the server commits, callers must
        disambiguate the intended copy from their notebook list.
        """
        if not notebook_id:
            raise ValidationError("notebook_id must not be empty")
        if not title or not title.strip():
            raise ValidationError("title must not be empty")

        async with self._operation_scope("notebooks.copy"):
            try:
                return await self._send_copy(notebook_id, title)
            except (NetworkError, RateLimitError, ServerError) as exc:
                rpc_code = exc.rpc_code if isinstance(exc, RPCError) else None
                failure = unresolved_commit_error(
                    self._copy_method_id,
                    "CopyProject",
                    RPCError(
                        "UNRESOLVED — CopyProject may have committed before its response was "
                        "lost. Do not blindly retry; list notebooks and resolve copies "
                        "manually first.",
                        method_id=self._copy_method_id,
                        rpc_code=rpc_code,
                    ),
                    preserve_exception=True,
                )
                if self._copy_failure_chain == "explicit":
                    raise failure from exc
                raise failure from None

    @abstractmethod
    async def _send_copy(self, notebook_id: str, title: str) -> Notebook:
        """Send one backend copy operation and decode the new notebook."""

    @abstractmethod
    async def get(self, notebook_id: str) -> Notebook:
        """Get notebook details or raise ``NotebookNotFoundError``."""

    async def get_or_none(self, notebook_id: str) -> Notebook | None:
        """Get notebook details, returning ``None`` when it does not exist."""
        try:
            return await self.get(notebook_id)
        except NotebookNotFoundError:
            return None

    @abstractmethod
    async def delete(self, notebook_id: str) -> None:
        """Delete a notebook."""

    async def rename(self, notebook_id: str, new_title: str) -> Notebook:
        """Rename a notebook and return the refreshed notebook."""
        return await self.update(notebook_id, title=new_title)

    async def set_emoji(self, notebook_id: str, emoji: str) -> Notebook:
        """Set a notebook's display emoji and return the refreshed notebook."""
        return await self.update(notebook_id, emoji=emoji)

    @abstractmethod
    async def update(
        self,
        notebook_id: str,
        *,
        title: str | None = None,
        emoji: str | None = None,
    ) -> Notebook:
        """Update a notebook's title and/or emoji in one mutation."""

    @abstractmethod
    async def get_summary(self, notebook_id: str) -> str:
        """Get raw summary text for a notebook."""

    @abstractmethod
    async def get_description(self, notebook_id: str) -> NotebookDescription:
        """Get an AI-generated summary and suggested topics for a notebook."""

    @abstractmethod
    async def remove_from_recent(self, notebook_id: str) -> None:
        """Remove a notebook from the recently viewed list."""

    @abstractmethod
    async def get_raw(self, notebook_id: str) -> Any:
        """Get undecoded notebook data from the backend."""

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        """Get a share URL without toggling server-side sharing."""
        return self._share_url_builder(notebook_id, artifact_id)

    async def get_metadata(self, notebook_id: str) -> NotebookMetadata:
        """Get notebook details composed with simplified source metadata."""
        async with self._operation_scope("notebooks.get_metadata"):
            return await self._metadata_service.get_metadata(notebook_id)


__all__ = ["NotebooksAPI"]
