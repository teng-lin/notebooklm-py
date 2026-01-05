"""High-level async API client for NotebookLM."""

import re
import httpx
from typing import Any, Dict, Optional, Union
from urllib.parse import urlencode

from .auth import AuthTokens
from .rpc import (
    RPCMethod,
    StudioContentType,
    AudioFormat,
    AudioLength,
    VideoFormat,
    VideoStyle,
    QuizQuantity,
    QuizDifficulty,
    InfographicOrientation,
    InfographicDetail,
    SlideDeckFormat,
    SlideDeckLength,
    BATCHEXECUTE_URL,
    QUERY_URL,
    encode_rpc_request,
    build_request_body,
    decode_response,
)


class NotebookLMClient:
    """Async client for NotebookLM RPC API.

    Uses the reverse-engineered batchexecute protocol to interact with
    NotebookLM. Requires valid authentication tokens obtained via browser login.

    Example:
        async with NotebookLMClient(auth_tokens) as client:
            notebooks = await client.list_notebooks()
            await client.create_notebook("My Research")
    """

    def __init__(self, auth: AuthTokens):
        self.auth = auth
        self._http_client: Optional[httpx.AsyncClient] = None
        self._reqid_counter = 100000
        self._conversation_cache = {}

    async def __aenter__(self) -> "NotebookLMClient":
        self._http_client = httpx.AsyncClient(
            headers={
                "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
                "Cookie": self.auth.cookie_header,
            },
            timeout=30.0,
        )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._http_client:
            await self._http_client.aclose()
            self._http_client = None

    def _build_url(self, rpc_method: RPCMethod, source_path: str = "/") -> str:
        params = {
            "rpcids": rpc_method.value,
            "source-path": source_path,
            "f.sid": self.auth.session_id,
            "rt": "c",
        }
        return f"{BATCHEXECUTE_URL}?{urlencode(params)}"

    async def _rpc_call(
        self,
        method: RPCMethod,
        params: list[Any],
        source_path: str = "/",
        allow_null: bool = False,
    ) -> Any:
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        url = self._build_url(method, source_path)
        rpc_request = encode_rpc_request(method, params)
        body = build_request_body(rpc_request, self.auth.csrf_token)

        response = await self._http_client.post(url, content=body)
        response.raise_for_status()

        return decode_response(response.text, method.value, allow_null=allow_null)

    async def list_artifacts(self, notebook_id: str) -> list[Any]:
        """List all artifacts (studio content) in a notebook.

        Returns a list of artifacts including Audio Overviews, Briefing Docs,
        Quizzes, Flashcards, etc.
        """
        # Signature from browser capture: [[2], notebook_id, filter_string]
        # Filter excludes suggested/draft items
        params = [[2], notebook_id, 'NOT artifact.status = "ARTIFACT_STATUS_SUGGESTED"']
        result = await self._rpc_call(
            RPCMethod.LIST_ARTIFACTS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        # Result is [[artifact1, ...], ...]
        if result and isinstance(result, list) and len(result) > 0:
            return result[0] if isinstance(result[0], list) else result
        return []

    async def get_artifact(self, notebook_id: str, artifact_id: str) -> Any:
        """Get a specific artifact by ID.

        Args:
            notebook_id: The notebook ID.
            artifact_id: The artifact ID to retrieve.

        Returns:
            Artifact data.
        """
        params = [artifact_id, notebook_id, [2]]
        return await self._rpc_call(
            RPCMethod.GET_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def list_video_overviews(self, notebook_id: str) -> list[Any]:
        """List all video overviews in the notebook."""
        artifacts = await self.list_artifacts(notebook_id)
        return [
            a for a in artifacts if isinstance(a, list) and len(a) > 2 and a[2] == 3
        ]

    async def list_slide_decks(self, notebook_id: str) -> list[Any]:
        """List all slide decks in the notebook."""
        artifacts = await self.list_artifacts(notebook_id)
        return [
            a for a in artifacts if isinstance(a, list) and len(a) > 2 and a[2] == 8
        ]

    async def list_reports(self, notebook_id: str) -> list[Any]:
        """List all reports in the notebook (Briefing Doc, Study Guide, Blog Post, etc.)."""
        artifacts = await self.list_artifacts(notebook_id)
        return [
            a for a in artifacts if isinstance(a, list) and len(a) > 2 and a[2] == 2
        ]

    async def list_quizzes(self, notebook_id: str) -> list[Any]:
        """List all quizzes in the notebook.

        Quizzes and flashcards both use type 4 internally. This method
        distinguishes them by checking if the title contains 'Quiz'.
        """
        artifacts = await self.list_artifacts(notebook_id)
        return [
            a
            for a in artifacts
            if isinstance(a, list)
            and len(a) > 2
            and a[2] == 4
            and isinstance(a[1], str)
            and "quiz" in a[1].lower()
        ]

    async def list_flashcards(self, notebook_id: str) -> list[Any]:
        """List all flashcards in the notebook.

        Flashcards and quizzes both use type 4 internally. This method
        distinguishes them by checking if the title contains 'Flashcard'.
        """
        artifacts = await self.list_artifacts(notebook_id)
        return [
            a
            for a in artifacts
            if isinstance(a, list)
            and len(a) > 2
            and a[2] == 4
            and isinstance(a[1], str)
            and "flashcard" in a[1].lower()
        ]

    async def list_infographics(self, notebook_id: str) -> list[Any]:
        """List all infographics in the notebook."""
        artifacts = await self.list_artifacts(notebook_id)
        return [
            a for a in artifacts if isinstance(a, list) and len(a) > 2 and a[2] == 7
        ]

    async def list_data_tables(self, notebook_id: str) -> list[Any]:
        """List all data tables in the notebook."""
        artifacts = await self.list_artifacts(notebook_id)
        return [
            a for a in artifacts if isinstance(a, list) and len(a) > 2 and a[2] == 9
        ]

    async def get_note(self, notebook_id: str, note_id: str) -> Optional[Any]:
        """Get a specific note by ID."""
        all_notes = await self._fetch_all_note_items(notebook_id)
        for note in all_notes:
            if isinstance(note, list) and len(note) > 0 and note[0] == note_id:
                return note
        return None

    async def download_audio(
        self, notebook_id: str, output_path: str, artifact_id: Optional[str] = None
    ) -> str:
        """Download the Audio Overview (MP3/MP4) to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the file.
            artifact_id: Optional specific audio artifact ID.

        Returns:
            The output path if successful.
        """
        import httpx

        artifacts = await self.list_artifacts(notebook_id)

        # Filter for audio artifacts
        audio_candidates = [
            a
            for a in artifacts
            if isinstance(a, list) and len(a) > 4 and a[2] == 1 and a[4] == 3
        ]

        if artifact_id:
            audio_art = next((a for a in audio_candidates if a[0] == artifact_id), None)
            if not audio_art:
                raise ValueError(
                    f"Audio artifact {artifact_id} not found or not ready."
                )
        else:
            audio_art = audio_candidates[0] if audio_candidates else None

        if not audio_art:
            raise ValueError("No completed audio overview found.")

        # Extract URL from artifact structure
        # Structure: art[6] -> metadata list -> index 5 is media list
        try:
            metadata = audio_art[6]
            if not isinstance(metadata, list) or len(metadata) <= 5:
                raise ValueError("Invalid audio metadata structure.")

            media_list = metadata[5]
            if not isinstance(media_list, list) or len(media_list) == 0:
                raise ValueError("No media URLs found.")

            # Find best quality URL (audio/mp4)
            # Items are [url, type_code, mime_type]
            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "audio/mp4":
                    url = item[0]
                    break

            if not url and len(media_list) > 0 and isinstance(media_list[0], list):
                url = media_list[0][0]  # Fallback to first URL

            if not url:
                raise ValueError("Could not extract download URL.")

            # Download
            async with httpx.AsyncClient() as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(response.content)

            return output_path

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse audio artifact structure: {e}")

    async def download_video(
        self, notebook_id: str, output_path: str, artifact_id: Optional[str] = None
    ) -> str:
        """Download a Video Overview (MP4) to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the file.
            artifact_id: Optional specific video artifact ID.

        Returns:
            The output path if successful.
        """
        import httpx

        videos = await self.list_video_overviews(notebook_id)
        # Filter for completed videos (Status 3)
        completed_videos = [v for v in videos if len(v) > 4 and v[4] == 3]

        if artifact_id:
            video_art = next((v for v in completed_videos if v[0] == artifact_id), None)
            if not video_art:
                raise ValueError(
                    f"Video artifact {artifact_id} not found or not ready."
                )
        else:
            # Sort by creation time? Or just pick the first completed one
            video_art = completed_videos[0] if completed_videos else None

        if not video_art:
            raise ValueError("No completed video overview found.")

        # Extract URL from artifact structure
        # Video metadata is at index 8
        try:
            if len(video_art) <= 8:
                raise ValueError("Invalid video artifact structure.")

            metadata = video_art[8]
            if not isinstance(metadata, list):
                raise ValueError("Invalid video metadata structure.")

            # Find the media list (list of lists with URLs)
            media_list = None
            for item in metadata:
                if (
                    isinstance(item, list)
                    and len(item) > 0
                    and isinstance(item[0], list)
                    and len(item[0]) > 0
                    and isinstance(item[0][0], str)
                    and item[0][0].startswith("http")
                ):
                    media_list = item
                    break

            if not media_list:
                raise ValueError("No media URLs found.")

            # Find video/mp4
            url = None
            for item in media_list:
                if isinstance(item, list) and len(item) > 2 and item[2] == "video/mp4":
                    url = item[0]
                    # Prefer the one with code 4 (usually higher quality?) or just first found
                    if item[1] == 4:
                        break

            if not url and len(media_list) > 0:
                url = media_list[0][0]

            if not url:
                raise ValueError("Could not extract download URL.")

            # Download
            async with httpx.AsyncClient() as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(response.content)

            return output_path

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse video artifact structure: {e}")

    async def download_infographic(
        self, notebook_id: str, output_path: str, artifact_id: Optional[str] = None
    ) -> str:
        """Download an Infographic (PNG/JPG) to a file.

        Args:
            notebook_id: The notebook ID.
            output_path: Path to save the file.
            artifact_id: Optional specific infographic artifact ID.

        Returns:
            The output path if successful.
        """
        import httpx

        infos = await self.list_infographics(notebook_id)
        # Filter for completed infographics (Status 3)
        completed_infos = [i for i in infos if len(i) > 4 and i[4] == 3]

        if artifact_id:
            info_art = next((i for i in completed_infos if i[0] == artifact_id), None)
            if not info_art:
                raise ValueError(
                    f"Infographic artifact {artifact_id} not found or not ready."
                )
        else:
            info_art = completed_infos[0] if completed_infos else None

        if not info_art:
            raise ValueError("No completed infographic found.")

        # Extract URL
        # Metadata is usually near the end. Search for the structure [null, "en", ...]
        try:
            metadata = None
            for item in reversed(info_art):
                if (
                    isinstance(item, list)
                    and len(item) > 0
                    and isinstance(item[0], list)
                ):
                    # Could be the metadata wrapper
                    # Metadata structure: [[null, "en", ...], "Title", [["Title", ["URL", W, H], ...]]]
                    if len(item) > 2 and isinstance(item[2], list) and len(item[2]) > 0:
                        content_list = item[2]
                        if (
                            isinstance(content_list[0], list)
                            and len(content_list[0]) > 1
                        ):
                            img_data = content_list[0][1]
                            if (
                                isinstance(img_data, list)
                                and len(img_data) > 0
                                and isinstance(img_data[0], str)
                                and img_data[0].startswith("http")
                            ):
                                metadata = item
                                break

            if not metadata:
                raise ValueError("Could not find infographic metadata.")

            url = metadata[2][0][1][0]  # ContentList[0] -> ImageInfo[0] -> URL

            # Download
            async with httpx.AsyncClient() as client:
                response = await client.get(url, follow_redirects=True)
                response.raise_for_status()
                with open(output_path, "wb") as f:
                    f.write(response.content)

            return output_path

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse infographic structure: {e}")

    async def download_slide_deck(
        self, notebook_id: str, output_dir: str, artifact_id: Optional[str] = None
    ) -> list[str]:
        """Download a slide deck as images to a directory.

        Args:
            notebook_id: The notebook ID.
            output_dir: Directory to save the slide images (PNGs).
            artifact_id: Optional specific slide deck artifact ID.

        Returns:
            List of paths to downloaded images.
        """
        import os
        import httpx

        slides = await self.list_slide_decks(notebook_id)
        # Filter for completed slide decks (Status 3)
        completed_slides = [s for s in slides if len(s) > 4 and s[4] == 3]

        if artifact_id:
            slide_art = next((s for s in completed_slides if s[0] == artifact_id), None)
            if not slide_art:
                raise ValueError(
                    f"Slide deck artifact {artifact_id} not found or not ready."
                )
        else:
            slide_art = completed_slides[0] if completed_slides else None

        if not slide_art:
            raise ValueError("No completed slide deck found.")

        if not os.path.exists(output_dir):
            os.makedirs(output_dir)

        downloaded_paths = []

        try:
            metadata = None
            # Search for metadata similar to infographic
            for item in reversed(slide_art):
                if (
                    isinstance(item, list)
                    and len(item) > 2
                    and isinstance(item[0], list)
                    and len(item[0]) > 1
                    and item[0][1] == "en"
                ):
                    # Likely metadata: [[null, "en"], "Title", [[slide1], [slide2]...]]
                    metadata = item
                    break

            if not metadata:
                raise ValueError("Could not find slides metadata.")

            slides_list = metadata[2]
            if not isinstance(slides_list, list):
                raise ValueError("Invalid slides list structure.")

            async with httpx.AsyncClient() as client:
                for i, slide in enumerate(slides_list):
                    # slide structure: [["URL", W, H], "Desc", "Content"]
                    if (
                        isinstance(slide, list)
                        and len(slide) > 0
                        and isinstance(slide[0], list)
                        and len(slide[0]) > 0
                    ):
                        url = slide[0][0]
                        if isinstance(url, str) and url.startswith("http"):
                            filename = f"slide_{i + 1:03d}.png"
                            path = os.path.join(output_dir, filename)

                            response = await client.get(url, follow_redirects=True)
                            response.raise_for_status()
                            with open(path, "wb") as f:
                                f.write(response.content)

                            downloaded_paths.append(path)

            return downloaded_paths

        except (IndexError, TypeError) as e:
            raise ValueError(f"Failed to parse slides structure: {e}")

    async def list_notebooks(self) -> list[Any]:
        params = [None, 1, None, [2]]
        result = await self._rpc_call(RPCMethod.LIST_NOTEBOOKS, params)
        if result and isinstance(result, list) and len(result) > 0:
            return result[0] if isinstance(result[0], list) else result
        return []

    async def list_featured_projects(
        self, page_size: int = 10, page_token: Optional[str] = None
    ) -> Any:
        """List featured/public notebooks."""
        params = [page_size, page_token]
        return await self._rpc_call(
            RPCMethod.LIST_FEATURED_PROJECTS,
            params,
            allow_null=True,
        )

    async def remove_recently_viewed(self, notebook_id: str) -> Any:
        """Remove a notebook from the recently viewed list."""
        params = [notebook_id]
        return await self._rpc_call(
            RPCMethod.REMOVE_RECENTLY_VIEWED,
            params,
            allow_null=True,
        )

    async def create_notebook(self, title: str) -> Any:
        params = [title, None, None, [2], [1]]
        return await self._rpc_call(RPCMethod.CREATE_NOTEBOOK, params)

    async def get_notebook(self, notebook_id: str) -> Any:
        params = [notebook_id, None, [2], None, 0]
        return await self._rpc_call(
            RPCMethod.GET_NOTEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def delete_notebook(self, notebook_id: str) -> Any:
        params = [[notebook_id], [2]]
        return await self._rpc_call(RPCMethod.DELETE_NOTEBOOK, params)

    async def rename_notebook(self, notebook_id: str, new_title: str) -> Any:
        """Rename a notebook.

        Uses the MutateProject RPC (s0tc2d) with the correct nested payload format
        discovered via browser traffic capture.

        Args:
            notebook_id: The notebook ID.
            new_title: The new title for the notebook.

        Returns:
            None (API returns null on success).
        """
        # Payload format discovered via browser traffic capture:
        # [notebook_id, [[null, null, null, [null, new_title]]]]
        params = [notebook_id, [[None, None, None, [None, new_title]]]]
        return await self._rpc_call(
            RPCMethod.RENAME_NOTEBOOK,
            params,
            source_path="/",  # Home page context, not notebook page
            allow_null=True,
        )

    async def delete_source(self, notebook_id: str, source_id: str) -> Any:
        """Delete a source from a notebook."""
        params = [[[source_id]]]
        return await self._rpc_call(
            RPCMethod.DELETE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def get_source(self, notebook_id: str, source_id: str) -> Any:
        """Get details of a specific source."""
        params = [source_id, notebook_id, [2]]
        return await self._rpc_call(
            RPCMethod.GET_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def refresh_source(self, notebook_id: str, source_id: str) -> Any:
        """Refresh a source to get updated content (for URL/Drive sources)."""
        params = [source_id]
        return await self._rpc_call(
            RPCMethod.REFRESH_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def check_source_freshness(self, notebook_id: str, source_id: str) -> Any:
        """Check if a source needs to be refreshed."""
        params = [source_id]
        return await self._rpc_call(
            RPCMethod.CHECK_SOURCE_FRESHNESS,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def mutate_source(
        self, notebook_id: str, source_id: str, updates: dict
    ) -> Any:
        """Update source metadata (e.g., enabled state)."""
        params = [source_id, updates]
        return await self._rpc_call(
            RPCMethod.MUTATE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def rename_source(
        self, notebook_id: str, source_id: str, new_title: str
    ) -> Any:
        """Rename a source."""
        params = [None, [source_id], [[[new_title]]]]
        return await self._rpc_call(
            RPCMethod.MUTATE_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def discover_sources(self, notebook_id: str, query: str) -> Any:
        """Discover sources based on a query (for research)."""
        params = [notebook_id, query]
        return await self._rpc_call(
            RPCMethod.DISCOVER_SOURCES,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    def _extract_youtube_video_id(self, url: str) -> Optional[str]:
        match = re.match(r"https?://youtu\.be/([a-zA-Z0-9_-]+)", url)
        if match:
            return match.group(1)
        match = re.match(
            r"https?://(?:www\.)?youtube\.com/watch\?v=([a-zA-Z0-9_-]+)", url
        )
        if match:
            return match.group(1)
        return None

    async def add_youtube_source(self, notebook_id: str, url: str) -> Any:
        """Add a YouTube video as a source.

        Uses the native NotebookLM RPC to add a YouTube video. The API
        automatically handles transcript extraction.

        Args:
            notebook_id: The notebook ID.
            url: YouTube video URL.

        Returns:
            The created source data.
        """
        video_id = self._extract_youtube_video_id(url)
        if not video_id:
            raise ValueError(f"Invalid YouTube URL: {url}")

        params = [
            [[None, None, None, None, None, None, None, [url], None, None, 1]],
            notebook_id,
            [2],
            [1, None, None, None, None, None, None, None, None, None, [1]],
        ]
        return await self._rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def _add_url_source(self, notebook_id: str, url: str) -> Any:
        params = [
            [[None, None, [url], None, None, None, None, None]],
            notebook_id,
            [2],
            None,
            None,
        ]
        return await self._rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def add_source_url(self, notebook_id: str, url: str) -> Any:
        video_id = self._extract_youtube_video_id(url)
        if video_id:
            return await self.add_youtube_source(notebook_id, url)
        return await self._add_url_source(notebook_id, url)

    async def add_source_text(
        self,
        notebook_id: str,
        title: str,
        text: str,
    ) -> Any:
        params = [
            [[None, [title, text], None, None, None, None, None, None]],
            notebook_id,
            [2],
            None,
            None,
        ]
        return await self._rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def add_source_file(
        self,
        notebook_id: str,
        file_path: Union[str, "Path"],
        mime_type: Optional[str] = None,
    ) -> Any:
        """Add a file source to a notebook using native upload.

        Args:
            notebook_id: The notebook ID.
            file_path: Path to the file to upload.
            mime_type: MIME type of the file. If None, auto-detect from extension.

        Returns:
            API response with source ID.

        Supported file types:
            - PDF: application/pdf
            - Text: text/plain
            - Markdown: text/markdown
            - Word: application/vnd.openxmlformats-officedocument.wordprocessingml.document
        """
        from pathlib import Path
        import base64
        import mimetypes

        file_path = Path(file_path)

        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Auto-detect MIME type if not provided
        if mime_type is None:
            mime_type, _ = mimetypes.guess_type(str(file_path))
            if mime_type is None:
                # Default to text/plain for unknown types
                mime_type = "text/plain"

        # Read and encode file content
        with open(file_path, "rb") as f:
            content = f.read()

        base64_content = base64.b64encode(content).decode("utf-8")
        filename = file_path.name

        # Build source data array for file upload
        # Format: [base64_content, filename, mime_type, "base64"]
        source_data = [base64_content, filename, mime_type, "base64"]

        params = [[source_data], notebook_id, [2]]

        return await self._rpc_call(
            RPCMethod.ADD_SOURCE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def get_summary(self, notebook_id: str) -> Any:
        params = [notebook_id, [2]]
        return await self._rpc_call(
            RPCMethod.SUMMARIZE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def generate_audio(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        audio_format: Optional[AudioFormat] = None,
        audio_length: Optional[AudioLength] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate audio overview (podcast) using the unified artifact endpoint.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for the podcast hosts.
            audio_format: Format style (DEEP_DIVE, BRIEF, CRITIQUE, DEBATE).
            audio_length: Length preference (SHORT, DEFAULT, LONG).

        Returns:
            Dictionary containing artifact metadata with keys:
                - artifact_id: Unique artifact identifier
                - status: "in_progress" or "completed"
                - title: Artifact title (if available)
                - create_time: Creation timestamp (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        format_code = audio_format.value if audio_format else 1
        length_code = audio_length.value if audio_length else 2

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                1,
                source_ids_triple,
                None,
                None,
                [
                    None,
                    [
                        instructions,  # Host instructions at position [6][1][0]
                        length_code,  # Length at position [6][1][1]
                        None,
                        source_ids_double,
                        language,
                        None,
                        format_code,  # Format at position [6][1][6]
                    ],
                ],
            ],
        ]
        result = await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,  # Google may return null on rate limit or quota errors
        )

        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            return {
                "artifact_id": artifact_data[0],
                "status": "in_progress" if artifact_data[4] == 1 else "completed",
                "title": artifact_data[1] if len(artifact_data) > 1 else None,
                "create_time": artifact_data[2] if len(artifact_data) > 2 else None,
            }

        return None

    async def list_artifacts_alt(self, notebook_id: str) -> Any:
        """Alternative method to list artifacts using LfTXoe RPC."""
        params = [notebook_id]
        return await self._rpc_call(
            RPCMethod.LIST_ARTIFACTS_ALT,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def get_audio_overview(self, notebook_id: str) -> Any:
        """Get audio overview details and status."""
        params = [notebook_id]
        return await self._rpc_call(
            RPCMethod.GET_AUDIO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def delete_audio_overview(self, notebook_id: str) -> Any:
        """Delete the audio overview for a notebook."""
        params = [notebook_id]
        return await self._rpc_call(
            RPCMethod.DELETE_AUDIO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def share_audio(self, notebook_id: str, public: bool = False) -> Any:
        """Share audio overview. Set public=True for public link."""
        share_options = [1] if public else [0]
        params = [share_options, notebook_id]
        return await self._rpc_call(
            RPCMethod.SHARE_AUDIO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def share_project(
        self, notebook_id: str, settings: Optional[dict] = None
    ) -> Any:
        """Share notebook with specified settings."""
        params = [notebook_id, settings or {}]
        return await self._rpc_call(
            RPCMethod.SHARE_PROJECT,
            params,
            allow_null=True,
        )

    async def refresh_auth(self) -> AuthTokens:
        """Refresh authentication tokens by fetching the NotebookLM homepage.

        This helps prevent 'Session Expired' errors by obtaining a fresh CSRF
        token (SNlM0e) and session ID (FdrFJe).
        """
        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        response = await self._http_client.get("https://notebooklm.google.com/")
        response.raise_for_status()

        # Extract SNlM0e (CSRF token)
        csrf_match = re.search(r'"SNlM0e":"([^"]+)"', response.text)
        if csrf_match:
            self.auth.csrf_token = csrf_match.group(1)

        # Extract FdrFJe (Session ID)
        sid_match = re.search(r'"FdrFJe":"([^"]+)"', response.text)
        if sid_match:
            self.auth.session_id = sid_match.group(1)

        return self.auth

    async def get_conversation_history(self, notebook_id: str, limit: int = 20) -> Any:
        """Get conversation history for a notebook."""
        params = [[], None, notebook_id, limit]
        return await self._rpc_call(
            RPCMethod.GET_CONVERSATION_HISTORY,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def get_project_analytics(self, notebook_id: str) -> Any:
        """Get analytics and metadata for a notebook."""
        params = [notebook_id]
        return await self._rpc_call(
            RPCMethod.PROJECT_ANALYTICS,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def list_guidebooks(self, notebook_id: str) -> Any:
        """List guidebooks for a notebook."""
        params = [notebook_id]
        return await self._rpc_call(
            RPCMethod.GET_GUIDEBOOKS,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def update_guidebook(
        self, notebook_id: str, guidebook_id: str, updates: dict
    ) -> Any:
        """Update a guidebook."""
        params = [guidebook_id, updates]
        return await self._rpc_call(
            RPCMethod.UPDATE_GUIDEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def delete_guidebook(self, notebook_id: str, guidebook_id: str) -> Any:
        """Delete a guidebook."""
        params = [guidebook_id]
        return await self._rpc_call(
            RPCMethod.DELETE_GUIDEBOOK,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    def _extract_source_ids(self, notebook_data: Any) -> list[str]:
        """Extract source IDs from notebook data."""
        source_ids = []
        if not notebook_data or not isinstance(notebook_data, list):
            return source_ids

        try:
            # Notebook structure: [[title, sources_array, id, emoji, ...], ...]
            # Sources at nb[0][1], each source: [[source_id], title, metadata, ...]
            if len(notebook_data) > 0 and isinstance(notebook_data[0], list):
                notebook_info = notebook_data[0]
                if len(notebook_info) > 1 and isinstance(notebook_info[1], list):
                    sources = notebook_info[1]
                    for source in sources:
                        if isinstance(source, list) and len(source) > 0:
                            first = source[0]
                            if isinstance(first, list) and len(first) > 0:
                                sid = first[0]
                                if isinstance(sid, str):
                                    source_ids.append(sid)
        except (IndexError, TypeError):
            pass

        return source_ids

    async def generate_video(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        video_format: Optional[VideoFormat] = None,
        video_style: Optional[VideoStyle] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate video overview.

        Args:
            notebook_id: The notebook ID.
            source_ids: Optional list of source IDs to use. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for video generation.
            video_format: Format type (EXPLAINER, BRIEF). Default: EXPLAINER.
            video_style: Visual style (AUTO_SELECT, CLASSIC, WHITEBOARD, etc.). Default: AUTO_SELECT.

        Returns:
            Dictionary containing artifact metadata with keys:
                - artifact_id: Unique artifact identifier
                - status: "in_progress" or "completed"
                - title: Artifact title (if available)
                - create_time: Creation timestamp (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        format_code = video_format.value if video_format else 1
        style_code = video_style.value if video_style else 1

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                3,
                source_ids_triple,
                None,
                None,
                None,
                None,
                [
                    None,
                    None,
                    [
                        source_ids_double,
                        language,
                        instructions,
                        None,
                        format_code,
                        style_code,
                    ],
                ],
            ],
        ]
        result = await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,  # Google may return null on rate limit or quota errors
        )

        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            return {
                "artifact_id": artifact_data[0],
                "status": "in_progress" if artifact_data[4] == 1 else "completed",
                "title": artifact_data[1] if len(artifact_data) > 1 else None,
                "create_time": artifact_data[2] if len(artifact_data) > 2 else None,
            }

        return None

    async def _act_on_sources(
        self,
        notebook_id: str,
        action: str,
        source_ids: Optional[list[str]] = None,
    ) -> Any:
        """Execute an action on notebook sources.

        Args:
            notebook_id: The notebook ID.
            action: Action to perform (e.g., "interactive_mindmap", "study_guide", "faq").
            source_ids: Optional list of source IDs. If None, fetches from notebook.

        Returns:
            Action result or None if action doesn't return data.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        params = [notebook_id, action, source_ids]
        return await self._rpc_call(
            RPCMethod.ACT_ON_SOURCES,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def generate_study_guide(
        self, notebook_id: str, source_ids: Optional[list[str]] = None
    ) -> Any:
        """Generate a study guide from notebook content."""
        return await self._act_on_sources(
            notebook_id, "notebook_guide_study_guide", source_ids
        )

    async def generate_faq(
        self, notebook_id: str, source_ids: Optional[list[str]] = None
    ) -> Any:
        """Generate FAQ from notebook content."""
        return await self._act_on_sources(notebook_id, "faq", source_ids)

    async def generate_briefing_doc(
        self, notebook_id: str, source_ids: Optional[list[str]] = None
    ) -> Any:
        """Generate a briefing document from notebook content."""
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        # Triple-nested for type array, double-nested for metadata
        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []
        source_ids_double = [[sid] for sid in source_ids] if source_ids else []

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                2,  # Type 2 = Briefing Doc
                source_ids_triple,
                None,
                None,
                None,
                [
                    None,
                    [
                        "Briefing Doc",
                        "Key insights and important quotes",
                        None,
                        source_ids_double,
                        "en",
                        "Create a comprehensive briefing document that synthesizes the main themes and ideas from the sources.",
                    ],
                ],
            ],
        ]
        return await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def generate_timeline(
        self, notebook_id: str, source_ids: Optional[list[str]] = None
    ) -> Any:
        """Generate a timeline from notebook content."""
        return await self._act_on_sources(notebook_id, "timeline", source_ids)

    async def generate_slide_deck(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        slide_deck_format: Optional[SlideDeckFormat] = None,
        slide_deck_length: Optional[SlideDeckLength] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate slide deck from notebook content.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for slide deck generation.
            slide_deck_format: DETAILED_DECK or PRESENTER_SLIDES.
            slide_deck_length: DEFAULT or SHORT.

        Returns:
            Dictionary containing artifact metadata with keys:
                - artifact_id: Unique artifact identifier
                - status: "in_progress" or "completed"
                - title: Artifact title (if available)
                - create_time: Creation timestamp (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []

        format_code = slide_deck_format.value if slide_deck_format else 1
        length_code = slide_deck_length.value if slide_deck_length else 1

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                8,
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [[instructions, language, format_code, length_code]],
            ],
        ]
        result = await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,  # Google may return null on rate limit or quota errors
        )

        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            return {
                "artifact_id": artifact_data[0],
                "status": "in_progress" if artifact_data[4] == 1 else "completed",
                "title": artifact_data[1] if len(artifact_data) > 1 else None,
                "create_time": artifact_data[2] if len(artifact_data) > 2 else None,
            }

        return None

    async def generate_quiz(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
        instructions: Optional[str] = None,
        quantity: Optional[QuizQuantity] = None,
        difficulty: Optional[QuizDifficulty] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate a quiz from notebook content.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to include. If None, uses all sources.
            instructions: Custom instructions for quiz generation.
            quantity: FEWER, STANDARD, or MORE questions.
            difficulty: EASY, MEDIUM, or HARD.

        Returns:
            Dictionary containing artifact metadata with keys:
                - artifact_id: Unique artifact identifier
                - status: "in_progress" or "completed"
                - title: Artifact title (if available)
                - create_time: Creation timestamp (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []

        # Default to STANDARD quantity (2) if not specified - API doesn't accept 0
        quantity_code = quantity.value if quantity else 2
        difficulty_code = difficulty.value if difficulty else 2

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                4,
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                [
                    None,
                    [
                        quantity_code,
                        None,
                        instructions,
                        None,
                        None,
                        None,
                        None,
                        [difficulty_code, difficulty_code],
                    ],
                ],
            ],
        ]
        result = await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,  # Google may return null on rate limit or quota errors
        )

        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            return {
                "artifact_id": artifact_data[0],
                "status": "in_progress" if artifact_data[4] == 1 else "completed",
                "title": artifact_data[1] if len(artifact_data) > 1 else None,
                "create_time": artifact_data[2] if len(artifact_data) > 2 else None,
            }

        return None

    async def generate_flashcards(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
        instructions: Optional[str] = None,
        quantity: Optional[QuizQuantity] = None,
        difficulty: Optional[QuizDifficulty] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate flashcards from notebook content.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to include. If None, uses all sources.
            instructions: Custom instructions for flashcard generation.
            quantity: FEWER, STANDARD, or MORE cards.
            difficulty: EASY, MEDIUM, or HARD.

        Returns:
            Dictionary containing artifact metadata with keys:
                - artifact_id: Unique artifact identifier
                - status: "in_progress" or "completed"
                - title: Artifact title (if available)
                - create_time: Creation timestamp (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []

        # Default to STANDARD quantity (2) if not specified - API doesn't accept 0
        quantity_code = quantity.value if quantity else 2
        difficulty_code = difficulty.value if difficulty else 2

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                4,
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                [
                    None,
                    [
                        quantity_code,
                        None,
                        instructions,
                        None,
                        None,
                        None,
                        [difficulty_code, difficulty_code],
                    ],
                ],
            ],
        ]
        result = await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,  # Google may return null on rate limit or quota errors
        )

        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            return {
                "artifact_id": artifact_data[0],
                "status": "in_progress" if artifact_data[4] == 1 else "completed",
                "title": artifact_data[1] if len(artifact_data) > 1 else None,
                "create_time": artifact_data[2] if len(artifact_data) > 2 else None,
            }

        return None

    async def generate_infographic(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
        orientation: Optional[InfographicOrientation] = None,
        detail_level: Optional[InfographicDetail] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate an infographic from notebook content.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions for infographic generation.
            orientation: LANDSCAPE, PORTRAIT, or SQUARE.
            detail_level: CONCISE, STANDARD, or DETAILED.

        Returns:
            Dictionary containing artifact metadata with keys:
                - artifact_id: Unique artifact identifier
                - status: "in_progress" or "completed"
                - title: Artifact title (if available)
                - create_time: Creation timestamp (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []

        orientation_code = orientation.value if orientation else 1
        detail_code = detail_level.value if detail_level else 2

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                7,  # StudioContentType.INFOGRAPHIC
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [
                    None,
                    [instructions, language, None, orientation_code, detail_code],
                ],
            ],
        ]
        result = await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,  # Google may return null on rate limit or quota errors
        )

        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            return {
                "artifact_id": artifact_data[0],
                "status": "in_progress" if artifact_data[4] == 1 else "completed",
                "title": artifact_data[1] if len(artifact_data) > 1 else None,
                "create_time": artifact_data[2] if len(artifact_data) > 2 else None,
            }

        return None

    async def generate_data_table(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
        language: str = "en",
        instructions: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate a data table from notebook content.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to include. If None, uses all sources.
            language: Language code (default: "en").
            instructions: Custom instructions describing desired table structure.

        Returns:
            Dictionary containing artifact metadata with keys:
                - artifact_id: Unique artifact identifier
                - status: "in_progress" or "completed"
                - title: Artifact title (if available)
                - create_time: Creation timestamp (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        source_ids_triple = [[[sid]] for sid in source_ids] if source_ids else []

        params = [
            [2],
            notebook_id,
            [
                None,
                None,
                9,
                source_ids_triple,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                [None, [instructions, language]],
            ],
        ]
        result = await self._rpc_call(
            RPCMethod.CREATE_VIDEO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,  # Google may return null on rate limit or quota errors
        )

        if result and isinstance(result, list) and len(result) > 0:
            artifact_data = result[0]
            return {
                "artifact_id": artifact_data[0],
                "status": "in_progress" if artifact_data[4] == 1 else "completed",
                "title": artifact_data[1] if len(artifact_data) > 1 else None,
                "create_time": artifact_data[2] if len(artifact_data) > 2 else None,
            }

        return None

    async def delete_studio_content(self, notebook_id: str, artifact_id: str) -> Any:
        """Delete generated studio content (audio, video, slides, etc.)."""
        params = [[2], artifact_id]
        return await self._rpc_call(
            RPCMethod.DELETE_STUDIO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def delete_note(self, notebook_id: str, note_id: str) -> Any:
        """Delete a note from the notebook."""
        params = [notebook_id, None, [note_id]]
        return await self._rpc_call(
            RPCMethod.DELETE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def delete_mind_map(self, notebook_id: str, mind_map_id: str) -> Any:
        """Delete a mind map from the notebook."""
        params = [notebook_id, None, [mind_map_id]]
        return await self._rpc_call(
            RPCMethod.DELETE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def rename_artifact(
        self, notebook_id: str, artifact_id: str, new_title: str
    ) -> Any:
        """Rename an artifact (note, mind map, data table, etc.)."""
        params = [[artifact_id, new_title], [["title"]]]
        return await self._rpc_call(
            RPCMethod.RENAME_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def update_artifact(
        self, notebook_id: str, artifact_id: str, updates: dict
    ) -> Any:
        """Update an artifact's metadata or content."""
        params = [artifact_id, updates]
        return await self._rpc_call(
            RPCMethod.UPDATE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def delete_artifact_alt(self, notebook_id: str, artifact_id: str) -> Any:
        """Alternative method to delete an artifact using WxBZtb RPC."""
        params = [artifact_id]
        return await self._rpc_call(
            RPCMethod.DELETE_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def export_artifact(
        self,
        notebook_id: str,
        artifact_id: Optional[str] = None,
        content: Optional[str] = None,
        title: str = "Export",
        export_type: int = 1,
    ) -> Any:
        """Export an artifact to Google Docs/Sheets."""
        params = [None, artifact_id, content, title, export_type]
        return await self._rpc_call(
            RPCMethod.EXPORT_ARTIFACT,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def poll_studio_status(
        self,
        notebook_id: str,
        task_id: str,
    ) -> Any:
        params = [task_id, notebook_id, [2]]
        return await self._rpc_call(
            RPCMethod.POLL_STUDIO,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def generate_mind_map(
        self,
        notebook_id: str,
        source_ids: Optional[list[str]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Generate an interactive mind map from notebook content.

        Args:
            notebook_id: The notebook ID.
            source_ids: List of source IDs to include. If None, uses all sources.

        Returns:
            Dictionary containing:
                - mind_map: The mind map data (JSON structure with name/children)
                - note_id: ID of the saved note (if available)
            Returns None if generation fails.
        """
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        # Format source IDs as triple-nested: [[["id1"]], [["id2"]]]
        source_ids_nested = [[[sid]] for sid in source_ids] if source_ids else []

        params = [
            source_ids_nested,
            None,
            None,
            None,
            None,
            ["interactive_mindmap", [["[CONTEXT]", ""]], ""],
            None,
            [2, None, [1]],
        ]

        result = await self._rpc_call(
            RPCMethod.ACT_ON_SOURCES,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

        if result and isinstance(result, list) and len(result) > 0:
            # Result structure: [[mind_map_json, null, [note_id, ...]]]
            inner = result[0]
            if isinstance(inner, list) and len(inner) > 0:
                mind_map_json = inner[0]
                note_info = inner[2] if len(inner) > 2 else None
                note_id = note_info[0] if note_info and isinstance(note_info, list) else None

                # Parse the mind map JSON if it's a string
                if isinstance(mind_map_json, str):
                    import json
                    try:
                        mind_map_data = json.loads(mind_map_json)
                    except json.JSONDecodeError:
                        mind_map_data = mind_map_json
                else:
                    mind_map_data = mind_map_json

                return {
                    "mind_map": mind_map_data,
                    "note_id": note_id,
                }

        return None

    async def save_mind_map(
        self,
        notebook_id: str,
        mind_map_data: dict,
        title: str = "Mind Map",
    ) -> Any:
        """Save a mind map to the notebook."""
        params = [notebook_id, mind_map_data, title, [2]]
        return await self._rpc_call(
            RPCMethod.CREATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

    async def save_note_content(
        self,
        notebook_id: str,
        note_id: str,
        content: str,
        title: str,
    ) -> Any:
        """Save/update content for an existing note."""
        params = [
            notebook_id,
            note_id,
            [[[content, title, [], 0]]],
        ]
        return await self._rpc_call(
            RPCMethod.MUTATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )

    async def create_note(
        self,
        notebook_id: str,
        title: str = "New Note",
        content: str = "",
    ) -> Any:
        """Create a new note in the notebook."""
        params = [notebook_id, "", [1], None, "New Note"]
        result = await self._rpc_call(
            RPCMethod.CREATE_NOTE,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if result and content:
            note_id = None
            if isinstance(result, list) and len(result) > 0:
                if isinstance(result[0], list) and len(result[0]) > 0:
                    note_id = result[0][0]
                elif isinstance(result[0], str):
                    note_id = result[0]

            if note_id:
                await self.save_note_content(notebook_id, note_id, content, title)

        return result

    async def _get_all_notes_and_mind_maps(self, notebook_id: str) -> list[Any]:
        """Internal method to fetch all notes and mind maps."""
        params = [notebook_id]
        result = await self._rpc_call(
            RPCMethod.GET_NOTES,
            params,
            source_path=f"/notebook/{notebook_id}",
            allow_null=True,
        )
        if (
            result
            and isinstance(result, list)
            and len(result) > 0
            and isinstance(result[0], list)
        ):
            notes_list = result[0]
            valid_notes = []
            for item in notes_list:
                if (
                    isinstance(item, list)
                    and len(item) > 0
                    and isinstance(item[0], str)
                ):
                    valid_notes.append(item)
            return valid_notes
        return []

    async def list_notes(self, notebook_id: str) -> list[Any]:
        """List all text notes in the notebook (excludes mind maps)."""
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        text_notes = []
        for item in all_items:
            content = None
            if len(item) > 1:
                if isinstance(item[1], str):
                    content = item[1]
                elif (
                    isinstance(item[1], list)
                    and len(item[1]) > 1
                    and isinstance(item[1][1], str)
                ):
                    content = item[1][1]

            is_mind_map = content and (
                '"children":' in content or '"nodes":' in content
            )
            if not is_mind_map:
                text_notes.append(item)
        return text_notes

    async def list_mind_maps(self, notebook_id: str) -> list[Any]:
        """List all mind maps in a notebook."""
        all_items = await self._get_all_notes_and_mind_maps(notebook_id)
        mind_maps = []
        for item in all_items:
            content = None
            if len(item) > 1:
                if isinstance(item[1], str):
                    content = item[1]
                elif (
                    isinstance(item[1], list)
                    and len(item[1]) > 1
                    and isinstance(item[1][1], str)
                ):
                    content = item[1][1]

            if content and ('"children":' in content or '"nodes":' in content):
                mind_maps.append(item)
        return mind_maps

    async def query(
        self,
        notebook_id: str,
        query_text: str,
        source_ids: Optional[list[str]] = None,
        conversation_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Query the notebook (ask a question)."""
        import uuid
        import json
        import os

        # If no source_ids provided, get them from the notebook
        if source_ids is None:
            notebook_data = await self.get_notebook(notebook_id)
            source_ids = self._extract_source_ids(notebook_data)

        # Determine if this is a new conversation or follow-up
        is_new_conversation = conversation_id is None
        if is_new_conversation:
            conversation_id = str(uuid.uuid4())
            conversation_history = None
        else:
            # Check if we have cached history for this conversation
            conversation_history = self._build_conversation_history(conversation_id)

        # Build source IDs structure: [[[sid]]] for each source
        sources_array = [[[sid]] for sid in source_ids] if source_ids else []

        params = [
            sources_array,
            query_text,
            conversation_history,
            [2, None, [1]],
            conversation_id,
        ]

        # Use compact JSON format
        params_json = json.dumps(params, separators=(",", ":"))
        f_req = [None, params_json]
        f_req_json = json.dumps(f_req, separators=(",", ":"))

        # Manual URL encoding to ensure safe characters are encoded
        from urllib.parse import quote

        encoded_req = quote(f_req_json, safe="")

        body_parts = [f"f.req={encoded_req}"]
        if self.auth.csrf_token:
            encoded_at = quote(self.auth.csrf_token, safe="")
            body_parts.append(f"at={encoded_at}")

        body = "&".join(body_parts) + "&"

        self._reqid_counter += 100000
        url_params = {
            "bl": os.environ.get(
                "NOTEBOOKLM_BL", "boq_labs-tailwind-frontend_20251221.14_p0"
            ),
            "hl": "en",
            "_reqid": str(self._reqid_counter),
            "rt": "c",
        }
        if self.auth.session_id:
            url_params["f.sid"] = self.auth.session_id

        query_string = urlencode(url_params)
        url = f"{QUERY_URL}?{query_string}"

        if not self._http_client:
            raise RuntimeError("Client not initialized. Use 'async with' context.")

        response = await self._http_client.post(url, content=body)
        response.raise_for_status()

        answer_text = self._parse_query_response(response.text)

        # Cache turn
        if answer_text:
            self._cache_conversation_turn(conversation_id, query_text, answer_text)

        turns = self._conversation_cache.get(conversation_id, [])
        turn_number = len(turns)

        return {
            "answer": answer_text,
            "conversation_id": conversation_id,
            "turn_number": turn_number,
            "is_follow_up": not is_new_conversation,
            "raw_response": response.text[:1000],
        }

    def _build_conversation_history(self, conversation_id: str) -> Optional[list]:
        turns = self._conversation_cache.get(conversation_id, [])
        if not turns:
            return None

        history = []
        for turn in turns:
            history.append([turn["answer"], None, 2])
            history.append([turn["query"], None, 1])
        return history

    def _cache_conversation_turn(self, conversation_id: str, query: str, answer: str):
        if conversation_id not in self._conversation_cache:
            self._conversation_cache[conversation_id] = []

        self._conversation_cache[conversation_id].append(
            {
                "query": query,
                "answer": answer,
                "turn_number": len(self._conversation_cache[conversation_id]) + 1,
            }
        )

    def _parse_query_response(self, response_text: str) -> str:
        if response_text.startswith(")]}'"):
            response_text = response_text[4:]

        import json

        lines = response_text.strip().split("\n")
        longest_answer = ""

        i = 0
        while i < len(lines):
            line = lines[i].strip()
            if not line:
                i += 1
                continue

            try:
                # Try parse byte count then JSON
                int(line)
                i += 1
                if i < len(lines):
                    json_str = lines[i]
                    text, is_answer = self._extract_answer_from_chunk(json_str)
                    if text and is_answer and len(text) > len(longest_answer):
                        longest_answer = text
                i += 1
            except ValueError:
                # Try parse JSON directly
                text, is_answer = self._extract_answer_from_chunk(line)
                if text and is_answer and len(text) > len(longest_answer):
                    longest_answer = text
                i += 1

        return longest_answer

    def _extract_answer_from_chunk(self, json_str: str) -> tuple[Optional[str], bool]:
        import json

        try:
            data = json.loads(json_str)
        except json.JSONDecodeError:
            return None, False

        if not isinstance(data, list):
            return None, False

        for item in data:
            if not isinstance(item, list) or len(item) < 3:
                continue
            if item[0] != "wrb.fr":
                continue

            inner_json = item[2]
            if not isinstance(inner_json, str):
                continue

            try:
                inner_data = json.loads(inner_json)
                if isinstance(inner_data, list) and len(inner_data) > 0:
                    first = inner_data[0]
                    if isinstance(first, list) and len(first) > 0:
                        text = first[0]
                        if isinstance(text, str) and len(text) > 20:
                            # Check type: 1=answer
                            is_answer = False
                            if len(first) > 4 and isinstance(first[4], list):
                                type_info = first[4]
                                if len(type_info) > 0 and type_info[-1] == 1:
                                    is_answer = True
                            return text, is_answer
            except json.JSONDecodeError:
                continue

        return None, False

    async def start_research(
        self,
        notebook_id: str,
        query: str,
        source: str = "web",
        mode: str = "fast",
    ) -> Optional[dict[str, Any]]:
        """Start a research session."""
        source_lower = source.lower()
        mode_lower = mode.lower()

        if source_lower not in ("web", "drive"):
            raise ValueError(f"Invalid source '{source}'. Use 'web' or 'drive'.")
        if mode_lower not in ("fast", "deep"):
            raise ValueError(f"Invalid mode '{mode}'. Use 'fast' or 'deep'.")
        if mode_lower == "deep" and source_lower == "drive":
            raise ValueError("Deep Research only supports Web sources.")

        # 1 = Web, 2 = Drive
        source_type = 1 if source_lower == "web" else 2

        if mode_lower == "fast":
            params = [[query, source_type], None, 1, notebook_id]
            rpc_id = RPCMethod.START_FAST_RESEARCH
        else:
            params = [None, [1], [query, source_type], 5, notebook_id]
            rpc_id = RPCMethod.START_DEEP_RESEARCH

        result = await self._rpc_call(
            rpc_id,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if result and isinstance(result, list) and len(result) > 0:
            task_id = result[0]
            report_id = result[1] if len(result) > 1 else None
            return {
                "task_id": task_id,
                "report_id": report_id,
                "notebook_id": notebook_id,
                "query": query,
                "mode": mode_lower,
            }
        return None

    async def poll_research(self, notebook_id: str) -> dict[str, Any]:
        """Poll for research results."""
        params = [None, None, notebook_id]
        result = await self._rpc_call(
            RPCMethod.POLL_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        if not result or not isinstance(result, list) or len(result) == 0:
            return {"status": "no_research"}

        # Unwrap if needed: [[task_id, task_info, status], [ts1], [ts2]]
        if (
            isinstance(result[0], list)
            and len(result[0]) > 0
            and isinstance(result[0][0], list)
        ):
            result = result[0]

        # Find most recent task
        for task_data in result:
            if not isinstance(task_data, list) or len(task_data) < 2:
                continue

            task_id = task_data[0]
            task_info = task_data[1]

            if not isinstance(task_id, str) or not isinstance(task_info, list):
                continue

            # Parse task info
            query_info = task_info[1] if len(task_info) > 1 else None
            research_mode = task_info[2] if len(task_info) > 2 else None
            sources_and_summary = task_info[3] if len(task_info) > 3 else []
            status_code = task_info[4] if len(task_info) > 4 else None

            query_text = query_info[0] if query_info else ""
            sources_data = []
            summary = ""

            if isinstance(sources_and_summary, list) and len(sources_and_summary) >= 1:
                sources_data = (
                    sources_and_summary[0]
                    if isinstance(sources_and_summary[0], list)
                    else []
                )
                if len(sources_and_summary) >= 2 and isinstance(
                    sources_and_summary[1], str
                ):
                    summary = sources_and_summary[1]

            parsed_sources = []
            for src in sources_data:
                if not isinstance(src, list) or len(src) < 2:
                    continue

                title = ""
                url = ""

                # Fast research: [url, title, desc, type, ...]
                # Deep research: [None, title, None, type, ..., [report]]

                if src[0] is None and len(src) > 1 and isinstance(src[1], str):
                    # Deep
                    title = src[1]
                    url = ""
                elif isinstance(src[0], str) or len(src) >= 3:
                    # Fast
                    url = src[0] if isinstance(src[0], str) else ""
                    title = src[1] if len(src) > 1 and isinstance(src[1], str) else ""

                if title or url:
                    parsed_sources.append({"url": url, "title": title})

            status = "completed" if status_code == 2 else "in_progress"

            return {
                "task_id": task_id,
                "status": status,
                "query": query_text,
                "sources": parsed_sources,
                "summary": summary,
            }

        return {"status": "no_research"}

    async def import_research_sources(
        self,
        notebook_id: str,
        task_id: str,
        sources: list[dict[str, str]],
    ) -> list[dict[str, str]]:
        """Import selected research sources."""
        if not sources:
            return []

        source_array = []
        for src in sources:
            url = src.get("url", "")
            title = src.get("title", "Untitled")

            # Web source structure
            source_data = [
                None,
                None,
                [url, title],
                None,
                None,
                None,
                None,
                None,
                None,
                None,
                2,
            ]
            source_array.append(source_data)

        params = [None, [1], task_id, notebook_id, source_array]

        result = await self._rpc_call(
            RPCMethod.IMPORT_RESEARCH,
            params,
            source_path=f"/notebook/{notebook_id}",
        )

        imported = []
        if result and isinstance(result, list):
            if (
                len(result) > 0
                and isinstance(result[0], list)
                and len(result[0]) > 0
                and isinstance(result[0][0], list)
            ):
                result = result[0]

            for src_data in result:
                if isinstance(src_data, list) and len(src_data) >= 2:
                    src_id = (
                        src_data[0][0]
                        if src_data[0] and isinstance(src_data[0], list)
                        else None
                    )
                    if src_id:
                        imported.append({"id": src_id, "title": src_data[1]})

        return imported

    @classmethod
    async def from_storage(cls, path=None) -> "NotebookLMClient":
        """Create a client from Playwright storage state file.

        This is the recommended way to create a client for programmatic use.
        Handles all authentication setup automatically.

        Args:
            path: Path to storage_state.json. If None, uses default location
                  (~/.notebooklm/storage_state.json).

        Returns:
            NotebookLMClient ready to use (already entered context).

        Example:
            client = await NotebookLMClient.from_storage()
            try:
                notebooks = await client.list_notebooks()
            finally:
                await client.__aexit__(None, None, None)

            # Or use as context manager after creation:
            client = await NotebookLMClient.from_storage()
            async with client:
                notebooks = await client.list_notebooks()
        """
        auth = await AuthTokens.from_storage(path)
        return cls(auth)
