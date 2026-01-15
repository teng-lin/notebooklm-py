"""Notebook operations API."""

import logging
from typing import Any

from ._core import ClientCore
from .rpc import RPCMethod
from .types import Notebook, NotebookDescription, SuggestedTopic

logger = logging.getLogger(__name__)


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

    def __init__(self, core: ClientCore):
        """Initialize the notebooks API.

        Args:
            core: The core client infrastructure.
        """
        self._core = core

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
        result = await self._core.rpc_call(RPCMethod.CREATE_NOTEBOOK, params)
        notebook = Notebook.from_api_response(result)
        logger.debug("Created notebook: %s", notebook.id)
        return notebook

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
        if result and isinstance(result, list) and len(result) > 0:
            return str(result[0]) if result[0] else ""
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

        if result and isinstance(result, list):
            # Summary at [0][0]
            if len(result) > 0 and isinstance(result[0], list) and len(result[0]) > 0:
                summary = result[0][0] if isinstance(result[0][0], str) else ""

            # Suggested topics at [1][0]
            if len(result) > 1 and isinstance(result[1], list) and len(result[1]) > 0:
                topics_list = result[1][0] if isinstance(result[1][0], list) else []
                for topic in topics_list:
                    if isinstance(topic, list) and len(topic) >= 2:
                        suggested_topics.append(
                            SuggestedTopic(
                                question=topic[0] if isinstance(topic[0], str) else "",
                                prompt=topic[1] if isinstance(topic[1], str) else "",
                            )
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
        base_url = f"https://notebooklm.google.com/notebook/{notebook_id}"
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
        base_url = f"https://notebooklm.google.com/notebook/{notebook_id}"
        if artifact_id:
            return f"{base_url}?artifactId={artifact_id}"
        return base_url
