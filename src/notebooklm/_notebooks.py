"""Notebook operations API."""

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from ._core import ClientCore
from ._env import get_base_url
from ._settings import build_get_user_settings_params, extract_account_limits
from .exceptions import NotebookLimitError, RPCError
from .rpc import RPCMethod
from .types import AccountLimits, Notebook, NotebookDescription, SuggestedTopic

if TYPE_CHECKING:
    from ._sources import SourcesAPI

logger = logging.getLogger(__name__)

CREATE_NOTEBOOK_QUOTA_RPC_CODE = 3


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

    def __init__(self, core: ClientCore, sources_api: "SourcesAPI | None" = None):
        """Initialize the notebooks API.

        Args:
            core: The core client infrastructure.
            sources_api: Optional sources API for cross-API calls. If None,
                         creates a new instance (for backward compatibility).
        """
        self._core = core
        # Lazy import to avoid circular dependency
        from ._sources import SourcesAPI

        self._sources = sources_api or SourcesAPI(core)

    async def list(self) -> list[Notebook]:
        """List all notebooks.

        Returns:
            List of Notebook objects.
        """
        logger.debug("Listing notebooks")
        params = [None, 1, None, [2]]
        result = await self._core.rpc_call(RPCMethod.LIST_NOTEBOOKS, params)

        if result and isinstance(result, list) and len(result) > 0:
            raw_notebooks = result[0] if isinstance(result[0], list) else result
            return [Notebook.from_api_response(nb) for nb in raw_notebooks]
        return []

    async def create(self, title: str) -> Notebook:
        """Create a new notebook.

        Args:
            title: The title for the new notebook.

        Returns:
            The created Notebook object.
        """
        logger.debug("Creating notebook: %s", title)
        params = [title, None, None, [2], [1]]
        try:
            result = await self._core.rpc_call(RPCMethod.CREATE_NOTEBOOK, params)
        except RPCError as exc:
            await self._raise_quota_error_if_detected(exc)
            raise
        notebook = Notebook.from_api_response(result)
        logger.debug("Created notebook: %s", notebook.id)
        return notebook

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
        result = await self._core.rpc_call(
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
        """
        params = [notebook_id, None, [2], None, 0]
        result = await self._core.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        # get_notebook returns [nb_info, ...] where nb_info contains the notebook data
        nb_info = result[0] if result and isinstance(result, list) and len(result) > 0 else []
        return Notebook.from_api_response(nb_info)

    async def delete(self, notebook_id: str) -> bool:
        """Delete a notebook.

        Args:
            notebook_id: The notebook ID to delete.

        Returns:
            True if deletion succeeded.
        """
        logger.debug("Deleting notebook: %s", notebook_id)
        params = [[notebook_id], [2]]
        await self._core.rpc_call(RPCMethod.DELETE_NOTEBOOK, params)
        return True

    async def rename(self, notebook_id: str, new_title: str) -> Notebook:
        """Rename a notebook.

        Args:
            notebook_id: The notebook ID.
            new_title: The new title for the notebook.

        Returns:
            The renamed Notebook object (fetched after rename).
        """
        logger.debug("Renaming notebook %s to: %s", notebook_id, new_title)
        # Payload format discovered via browser traffic capture:
        # [notebook_id, [[null, null, null, [null, new_title]]]]
        params = [notebook_id, [[None, None, None, [None, new_title]]]]
        await self._core.rpc_call(
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
        result = await self._core.rpc_call(
            RPCMethod.SUMMARIZE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )
        # Response structure: [[[summary_string, ...], topics, ...]]
        # Summary is at result[0][0][0]
        try:
            if result and isinstance(result, list):
                summary = result[0][0][0]
                return str(summary) if summary else ""
        except (IndexError, TypeError) as e:
            # result[0][0][0] is unguarded — except IS reachable when the
            # summary payload shape drifts.
            logger.warning(
                "Summary extraction failed for notebook %s (schema drift?): %s",
                notebook_id,
                e,
                exc_info=True,
            )
        return ""

    async def get_description(self, notebook_id: str) -> NotebookDescription:
        """Get AI-generated summary and suggested topics for a notebook.

        This provides a high-level overview of what the notebook contains,
        similar to what's shown in the Chat panel when opening a notebook.

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
        result = await self._core.rpc_call(
            RPCMethod.SUMMARIZE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        summary = ""
        suggested_topics: list[SuggestedTopic] = []

        # Response structure: [[[summary_string], [[topics]], ...]]
        # Summary is at result[0][0][0], topics at result[0][1][0]
        if result and isinstance(result, list):
            try:
                outer = result[0]

                # Summary at outer[0][0]
                summary_val = outer[0][0]
                summary = str(summary_val) if summary_val else ""

                # Suggested topics at outer[1][0]
                topics_list = outer[1][0]
                if isinstance(topics_list, list):
                    for topic in topics_list:
                        if isinstance(topic, list) and len(topic) >= 2:
                            suggested_topics.append(
                                SuggestedTopic(
                                    question=str(topic[0]) if topic[0] else "",
                                    prompt=str(topic[1]) if topic[1] else "",
                                )
                            )
            except (IndexError, TypeError) as e:
                # A partial result (e.g. summary but no topics) is possible.
                logger.debug(
                    "Partial description for notebook %s (no topics?): %s",
                    notebook_id,
                    e,
                )

        return NotebookDescription(summary=summary, suggested_topics=suggested_topics)

    async def remove_from_recent(self, notebook_id: str) -> None:
        """Remove a notebook from the recently viewed list.

        Args:
            notebook_id: The notebook ID to remove from recent.
        """
        params = [notebook_id]
        await self._core.rpc_call(
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
        params = [notebook_id, None, [2], None, 0]
        return await self._core.rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def share(
        self, notebook_id: str, public: bool = True, artifact_id: str | None = None
    ) -> dict:
        """Toggle notebook sharing.

        Note: This method uses SHARE_ARTIFACT for artifact-level sharing.
        For notebook-level sharing with user management, use client.sharing instead:

            await client.sharing.set_public(notebook_id, True)
            await client.sharing.add_user(notebook_id, email, SharePermission.VIEWER)

        Sharing is a NOTEBOOK-LEVEL setting. When enabled, ALL artifacts in the
        notebook become accessible via their URLs.

        Args:
            notebook_id: The notebook ID.
            public: If True, enable sharing. If False, disable sharing.
            artifact_id: Optional artifact ID for generating a deep-link URL.

        Returns:
            Dict with 'public' status, 'url', and 'artifact_id'.
        """
        share_options = [1] if public else [0]
        if artifact_id:
            params = [share_options, notebook_id, artifact_id]
        else:
            params = [share_options, notebook_id]

        await self._core.rpc_call(
            RPCMethod.SHARE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        # Build share URL
        base_url = f"{get_base_url()}/notebook/{notebook_id}"
        if public and artifact_id:
            url = f"{base_url}?artifactId={artifact_id}"
        elif public:
            url = base_url
        else:
            url = None

        return {
            "public": public,
            "url": url,
            "artifact_id": artifact_id,
        }

    def get_share_url(self, notebook_id: str, artifact_id: str | None = None) -> str:
        """Get share URL for a notebook or artifact.

        This does NOT toggle sharing - it just returns the URL format.
        Use share() to enable/disable sharing.

        Args:
            notebook_id: The notebook ID.
            artifact_id: Optional artifact ID for a deep-link URL.

        Returns:
            The share URL string.
        """
        base_url = f"{get_base_url()}/notebook/{notebook_id}"
        if artifact_id:
            return f"{base_url}?artifactId={artifact_id}"
        return base_url

    async def get_metadata(self, notebook_id: str):
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
        # Get notebook details and sources list concurrently
        notebook, sources = await asyncio.gather(
            self.get(notebook_id),
            self._sources.list(notebook_id),
        )

        # Warn on potential data loss
        if notebook.sources_count > 0 and len(sources) == 0:
            logger.warning(
                "Notebook %s reports %d sources but listing returned empty",
                notebook_id,
                notebook.sources_count,
            )

        # Build simplified source info
        from .types import NotebookMetadata, SourceSummary

        simplified_sources = [
            SourceSummary(
                kind=source.kind,
                title=source.title,
                url=source.url,
            )
            for source in sources
        ]

        return NotebookMetadata(
            notebook=notebook,
            sources=simplified_sources,
        )
