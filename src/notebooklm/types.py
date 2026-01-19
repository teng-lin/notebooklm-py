"""Data types for NotebookLM API client.

This module contains all dataclasses and re-exports enums from rpc/types.py
for convenient access.

Usage:
    from notebooklm.types import Notebook, Source, Artifact, GenerationStatus
    from notebooklm.types import AudioFormat, VideoFormat, StudioContentType
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

# Re-export enums from rpc/types.py for convenience
from .rpc.types import (
    AudioFormat,
    AudioLength,
    ChatGoal,
    ChatResponseLength,
    DriveMimeType,
    ExportType,
    InfographicDetail,
    InfographicOrientation,
    QuizDifficulty,
    QuizQuantity,
    ReportFormat,
    SlideDeckFormat,
    SlideDeckLength,
    SourceStatus,
    SourceType,
    StudioContentType,
    VideoFormat,
    VideoStyle,
    artifact_status_to_str,
    source_status_to_str,
    source_type_code_to_str,
)

__all__ = [
    # Dataclasses
    "Notebook",
    "NotebookDescription",
    "SuggestedTopic",
    "Source",
    "SourceFulltext",
    "Artifact",
    "GenerationStatus",
    "ReportSuggestion",
    "Note",
    "ConversationTurn",
    "ChatReference",
    "AskResult",
    "ChatMode",
    # Exceptions
    "SourceError",
    "SourceAddError",
    "SourceProcessingError",
    "SourceTimeoutError",
    "SourceNotFoundError",
    "ArtifactError",
    "ArtifactNotFoundError",
    "ArtifactNotReadyError",
    "ArtifactParseError",
    "ArtifactDownloadError",
    # Re-exported enums
    "StudioContentType",
    "AudioFormat",
    "AudioLength",
    "VideoFormat",
    "VideoStyle",
    "QuizQuantity",
    "QuizDifficulty",
    "InfographicOrientation",
    "InfographicDetail",
    "SlideDeckFormat",
    "SlideDeckLength",
    "ReportFormat",
    "ChatGoal",
    "ChatResponseLength",
    "DriveMimeType",
    "ExportType",
    "SourceStatus",
    "SourceType",
    # Helper functions
    "artifact_status_to_str",
    "source_status_to_str",
    "source_type_code_to_str",
]


# =============================================================================
# Chat Mode Enum (service-level, not RPC-level)
# =============================================================================

from enum import Enum


class ChatMode(Enum):
    """Predefined chat modes for common use cases."""

    DEFAULT = "default"  # General purpose
    LEARNING_GUIDE = "learning_guide"  # Educational focus
    CONCISE = "concise"  # Brief responses
    DETAILED = "detailed"  # Verbose responses


# =============================================================================
# Notebook Types
# =============================================================================


@dataclass
class Notebook:
    """Represents a NotebookLM notebook."""

    id: str
    title: str
    created_at: datetime | None = None
    sources_count: int = 0
    is_owner: bool = True

    @classmethod
    def from_api_response(cls, data: list[Any]) -> "Notebook":
        """Parse notebook from API response.

        Args:
            data: Raw API response list.

        Returns:
            Notebook instance.
        """
        raw_title = data[0] if len(data) > 0 and isinstance(data[0], str) else ""
        title = raw_title.replace("thought\n", "").strip()
        notebook_id = data[2] if len(data) > 2 and isinstance(data[2], str) else ""

        created_at = None
        if len(data) > 5 and isinstance(data[5], list) and len(data[5]) > 5:
            ts_data = data[5][5]
            if isinstance(ts_data, list) and len(ts_data) > 0:
                try:
                    created_at = datetime.fromtimestamp(ts_data[0])
                except (TypeError, ValueError):
                    pass

        # Extract ownership - data[5][1] = False means owner, True means shared
        is_owner = True
        if len(data) > 5 and isinstance(data[5], list) and len(data[5]) > 1:
            is_owner = data[5][1] is False

        return cls(id=notebook_id, title=title, created_at=created_at, is_owner=is_owner)


@dataclass
class SuggestedTopic:
    """A suggested topic/question for the notebook."""

    question: str
    prompt: str


@dataclass
class NotebookDescription:
    """AI-generated description and suggested topics for a notebook."""

    summary: str
    suggested_topics: list[SuggestedTopic] = field(default_factory=list)

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "NotebookDescription":
        """Parse from get_notebook_description() response."""
        topics = [
            SuggestedTopic(question=t.get("question", ""), prompt=t.get("prompt", ""))
            for t in data.get("suggested_topics", [])
        ]
        return cls(
            summary=data.get("summary", ""),
            suggested_topics=topics,
        )


# =============================================================================
# Source Types
# =============================================================================


class SourceError(Exception):
    """Base exception for source-related errors."""

    pass


class SourceProcessingError(SourceError):
    """Raised when source processing fails (status=ERROR).

    Attributes:
        source_id: The ID of the source that failed.
        status: The status code (typically 3 for ERROR).
    """

    def __init__(self, source_id: str, status: int = 3, message: str = ""):
        self.source_id = source_id
        self.status = status
        msg = message or f"Source {source_id} failed to process"
        super().__init__(msg)


class SourceTimeoutError(SourceError):
    """Raised when waiting for source readiness times out.

    Attributes:
        source_id: The ID of the source.
        timeout: The timeout duration in seconds.
        last_status: The last observed status before timeout.
    """

    def __init__(self, source_id: str, timeout: float, last_status: int | None = None):
        self.source_id = source_id
        self.timeout = timeout
        self.last_status = last_status
        status_info = f" (last status: {last_status})" if last_status is not None else ""
        super().__init__(f"Source {source_id} not ready after {timeout:.1f}s{status_info}")


class SourceNotFoundError(SourceError):
    """Raised when a source is not found in the notebook.

    Attributes:
        source_id: The ID of the source that was not found.
    """

    def __init__(self, source_id: str):
        self.source_id = source_id
        super().__init__(f"Source {source_id} not found")


class SourceAddError(SourceError):
    """Raised when adding a source fails.

    Common causes include:
    - URL is invalid or inaccessible
    - Content is behind a paywall
    - Source content is empty or could not be parsed
    - Rate limiting or quota exceeded

    Attributes:
        url: The URL or identifier of the source that failed.
        cause: The underlying exception that caused the failure.
    """

    def __init__(self, url: str, cause: Exception | None = None, message: str | None = None):
        self.url = url
        self.cause = cause
        msg = message or (
            f"Failed to add source: {url}\n"
            "Possible causes:\n"
            "  - URL is invalid or inaccessible\n"
            "  - Content is behind a paywall or requires authentication\n"
            "  - Page content is empty or could not be parsed\n"
            "  - Rate limiting or quota exceeded"
        )
        super().__init__(msg)


# =============================================================================
# Artifact Error Types
# =============================================================================


class ArtifactError(Exception):
    """Base exception for artifact-related errors.

    This includes errors when generating, fetching, parsing, or downloading artifacts
    such as audio overviews, videos, reports, quizzes, and other generated content.
    """

    pass


class ArtifactNotFoundError(ArtifactError):
    """Raised when a specific artifact is not found.

    Attributes:
        artifact_id: The ID of the artifact that was not found.
        artifact_type: The type of artifact (e.g., "audio", "video", "report").
    """

    def __init__(self, artifact_id: str, artifact_type: str | None = None):
        self.artifact_id = artifact_id
        self.artifact_type = artifact_type
        type_info = f" {artifact_type}" if artifact_type else ""
        super().__init__(f"{type_info.capitalize()} artifact {artifact_id} not found")


class ArtifactNotReadyError(ArtifactError):
    """Raised when an artifact is not in a completed/ready state.

    This typically means the artifact is still being generated or has failed processing.

    Attributes:
        artifact_type: The type of artifact (e.g., "audio", "video").
        artifact_id: The ID of the artifact (if known).
        status: The current status of the artifact (if known).
    """

    def __init__(
        self,
        artifact_type: str,
        artifact_id: str | None = None,
        status: str | None = None,
    ):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.status = status

        if artifact_id:
            msg = f"{artifact_type.capitalize()} artifact {artifact_id} is not ready"
            if status:
                msg += f" (status: {status})"
        else:
            msg = f"No completed {artifact_type} found"

        super().__init__(msg)


def _build_artifact_error_message(
    action: str, artifact_type: str, artifact_id: str | None, details: str | None
) -> str:
    """Build a consistent error message for artifact operations."""
    msg = f"Failed to {action} {artifact_type} artifact"
    if artifact_id:
        msg += f" {artifact_id}"
    if details:
        msg += f": {details}"
    return msg


class ArtifactParseError(ArtifactError):
    """Raised when artifact data cannot be parsed or has invalid structure.

    This indicates the API returned data in an unexpected format, which may occur
    when the API structure changes or the response is malformed.

    Attributes:
        artifact_type: The type of artifact being parsed.
        artifact_id: The ID of the artifact (if known).
        details: Additional error details from the parsing attempt.
        cause: The underlying exception that caused the failure.
    """

    def __init__(
        self,
        artifact_type: str,
        details: str | None = None,
        artifact_id: str | None = None,
        cause: Exception | None = None,
    ):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.details = details
        self.cause = cause
        super().__init__(
            _build_artifact_error_message("parse", artifact_type, artifact_id, details)
        )


class ArtifactDownloadError(ArtifactError):
    """Raised when downloading artifact content fails.

    This occurs when the artifact exists but its content cannot be retrieved,
    such as missing download URLs or HTTP errors during download.

    Attributes:
        artifact_type: The type of artifact being downloaded.
        artifact_id: The ID of the artifact (if known).
        details: Additional error details.
        cause: The underlying exception that caused the failure.
    """

    def __init__(
        self,
        artifact_type: str,
        details: str | None = None,
        artifact_id: str | None = None,
        cause: Exception | None = None,
    ):
        self.artifact_type = artifact_type
        self.artifact_id = artifact_id
        self.details = details
        self.cause = cause
        super().__init__(
            _build_artifact_error_message("download", artifact_type, artifact_id, details)
        )


@dataclass
class Source:
    """Represents a NotebookLM source.

    Attributes:
        id: Unique source identifier.
        title: Source title (may be URL if not yet processed).
        url: Original URL for web/YouTube sources.
        source_type: Type of source as string (for display/backward compat).
        source_type_code: Integer type code from SourceType enum (if known).
        created_at: When the source was added.
        status: Processing status (1=processing, 2=ready, 3=error).

    Breaking changes in v0.3.0:
        source_type string values changed for consistency with SourceType enum:
        - "url" → "web_page"
        - "generated" → "markdown"
        - "text" → "docx"
        - "spreadsheet" → "google_spreadsheet"
        - Added: "csv"

        Migration: Use source_type_code (int) for stable comparisons instead of
        source_type (str). Example:
            # Old (breaks in 0.3.0):
            if source.source_type == "url": ...

            # New (stable API):
            from notebooklm.rpc.types import SourceType
            if source.source_type_code == SourceType.WEB_PAGE: ...
    """

    id: str
    title: str | None = None
    url: str | None = None
    source_type: str = "unknown"
    source_type_code: int | None = None
    created_at: datetime | None = None
    status: int = SourceStatus.READY  # Default to READY (2)

    @property
    def is_ready(self) -> bool:
        """Check if source is ready for use (status=READY)."""
        return self.status == SourceStatus.READY

    @property
    def is_processing(self) -> bool:
        """Check if source is still being processed (status=PROCESSING)."""
        return self.status == SourceStatus.PROCESSING

    @property
    def is_error(self) -> bool:
        """Check if source processing failed (status=ERROR)."""
        return self.status == SourceStatus.ERROR

    @classmethod
    def from_api_response(cls, data: list[Any], notebook_id: str | None = None) -> "Source":
        """Parse source data from various API response formats.

        The API returns different structures for different operations:
        - add_source: [[[[id], title, metadata]]] (deeply nested)
        - list_sources: [[[id], title, metadata], ...] (one level less nesting)
        - rename_source: May return simpler structure

        Note:
            This method does NOT parse the source status field. Sources created
            via this method will have status=READY by default. To get accurate
            status information (PROCESSING, READY, or ERROR), use
            `client.sources.list()` or `client.sources.get()` which parse
            status from the full notebook response structure.
        """
        if not data or not isinstance(data, list):
            raise ValueError(f"Invalid source data: {data}")

        # Try deeply nested format: [[[[id], title, metadata, ...]]]
        if isinstance(data[0], list) and len(data[0]) > 0:
            if isinstance(data[0][0], list) and len(data[0][0]) > 0:
                # Check if deeply nested vs medium nested
                if isinstance(data[0][0][0], list):
                    # Deeply nested: [[[[id], title, ...]]]
                    entry = data[0][0]
                    source_id = entry[0][0] if isinstance(entry[0], list) else entry[0]
                    title = entry[1] if len(entry) > 1 else None
                else:
                    # Medium nested: [[['id'], 'title', ...]]
                    entry = data[0]
                    source_id = entry[0][0] if isinstance(entry[0], list) else entry[0]
                    title = entry[1] if len(entry) > 1 else None

                    # Try to extract URL if present
                    url = None
                    if len(entry) > 2 and isinstance(entry[2], list):
                        if len(entry[2]) > 7 and isinstance(entry[2][7], list):
                            url = entry[2][7][0] if entry[2][7] else None

                    return cls(id=str(source_id), title=title, url=url, source_type="unknown")

                # Deeply nested: continue with URL and type code extraction
                url = None
                source_type_code = None
                if len(entry) > 2 and isinstance(entry[2], list):
                    if len(entry[2]) > 7:
                        url_list = entry[2][7]
                        if isinstance(url_list, list) and len(url_list) > 0:
                            url = url_list[0]
                    if not url and len(entry[2]) > 0:
                        if isinstance(entry[2][0], str) and entry[2][0].startswith("http"):
                            url = entry[2][0]
                    # Extract type code at entry[2][4] if available
                    if len(entry[2]) > 4 and isinstance(entry[2][4], int):
                        source_type_code = entry[2][4]

                # Derive source_type from type code (source of truth)
                source_type = source_type_code_to_str(source_type_code)

                return cls(
                    id=str(source_id),
                    title=title,
                    url=url,
                    source_type=source_type,
                    source_type_code=source_type_code,
                )

        # Simple flat format: [id, title] or [id, title, ...]
        source_id = data[0] if len(data) > 0 else ""
        title = data[1] if len(data) > 1 else None
        return cls(id=str(source_id), title=title, source_type="unknown")


@dataclass
class SourceFulltext:
    """Full text content of a source as indexed by NotebookLM.

    This is the raw text content that was extracted/indexed from the source,
    along with metadata. Returned by `client.sources.get_fulltext()`.

    Attributes:
        source_id: The source UUID.
        title: Source title.
        content: Full indexed text content.
        source_type: Integer type code. Use SourceType enum for comparison:
            SourceType.GOOGLE_DOCS (1), SourceType.PDF (3), SourceType.PASTED_TEXT (4),
            SourceType.WEB_PAGE (5), SourceType.YOUTUBE (9), etc.
        url: Original URL for web/YouTube sources.
        char_count: Number of characters in the content.
    """

    source_id: str
    title: str
    content: str
    source_type: int | None = None
    url: str | None = None
    char_count: int = 0

    def find_citation_context(
        self,
        cited_text: str,
        context_chars: int = 200,
    ) -> list[tuple[str, int]]:
        """Search for citation text and return matching contexts.

        Best-effort heuristic using substring search. May fail or return
        incorrect matches when:
        - cited_text appears multiple times (all occurrences returned)
        - NotebookLM truncated the citation during chunking
        - Formatting differs between citation and indexed content

        Note: ChatReference.start_char/end_char reference NotebookLM's internal
        chunked index, NOT positions in this fulltext. Use this method instead.

        Args:
            cited_text: Text to search for (from ChatReference.cited_text).
            context_chars: Surrounding context to include (default 200).

        Returns:
            List of (context, position) tuples for each match found.
            Empty list if no matches. Position is start of match in content.
        """
        if not cited_text or not self.content:
            return []

        # Use prefix for search (citations are often truncated)
        search_text = cited_text[: min(40, len(cited_text))]

        matches = []
        pos = 0
        while (idx := self.content.find(search_text, pos)) != -1:
            start = max(0, idx - context_chars)
            end = min(len(self.content), idx + len(search_text) + context_chars)
            matches.append((self.content[start:end], idx))
            pos = idx + len(search_text)  # Skip past match to avoid overlaps

        return matches


# =============================================================================
# Artifact Types
# =============================================================================


@dataclass
class Artifact:
    """Represents a NotebookLM artifact (studio content).

    Artifacts are AI-generated content like Audio Overviews, Video Overviews,
    Reports, Quizzes, Flashcards, Mind Maps, Infographics, Slide Decks, and
    Data Tables.
    """

    id: str
    title: str
    artifact_type: int  # StudioContentType enum value
    status: int  # 1=processing, 3=completed
    created_at: datetime | None = None
    url: str | None = None
    variant: int | None = None  # For type 4: 1=flashcards, 2=quiz

    @classmethod
    def from_api_response(cls, data: list[Any]) -> "Artifact":
        """Parse artifact from API response.

        Structure: [id, title, type, ..., status, ..., metadata, ...]
        Position 9 contains options with variant code at [9][1][0]:
          - For type 4: 1=flashcards, 2=quiz
        """
        artifact_id = data[0] if len(data) > 0 else ""
        title = data[1] if len(data) > 1 else ""
        artifact_type = data[2] if len(data) > 2 else 0
        status = data[4] if len(data) > 4 else 0

        # Extract timestamp from data[15][0]
        created_at = None
        if len(data) > 15 and isinstance(data[15], list) and len(data[15]) > 0:
            try:
                created_at = datetime.fromtimestamp(data[15][0])
            except (TypeError, ValueError):
                pass

        # Extract variant code from data[9][1][0] for quiz/flashcard distinction
        variant = None
        if len(data) > 9 and isinstance(data[9], list) and len(data[9]) > 1:
            options = data[9][1]
            if isinstance(options, list) and len(options) > 0:
                variant = options[0]

        return cls(
            id=str(artifact_id),
            title=str(title),
            artifact_type=artifact_type,
            status=status,
            created_at=created_at,
            variant=variant,
        )

    @classmethod
    def from_mind_map(cls, data: list[Any]) -> Optional["Artifact"]:
        """Parse artifact from mind map data (stored in notes system).

        Mind map structure:
        [
            "mind_map_id",
            [
                "mind_map_id",           # [1][0]: ID
                "JSON_content",          # [1][1]: Mind map JSON
                [1, "user_id", [ts, ns]],  # [1][2]: Metadata
                None,                    # [1][3]
                "title"                  # [1][4]: Title
            ]
        ]

        Deleted/cleared mind map: ["id", None, 2]

        Returns:
            Artifact object, or None if deleted (status=2).
        """
        if not isinstance(data, list) or len(data) < 1:
            return None

        mind_map_id = data[0] if len(data) > 0 else ""

        # Check for deleted status (item[1] is None with status=2)
        if len(data) >= 3 and data[1] is None and data[2] == 2:
            return None  # Deleted, don't include

        # Extract title and timestamp from nested structure
        title = ""
        created_at = None

        if len(data) > 1 and isinstance(data[1], list):
            inner = data[1]
            # Title is at position [4]
            if len(inner) > 4 and isinstance(inner[4], str):
                title = inner[4]
            # Timestamp is at [2][2][0]
            if len(inner) > 2 and isinstance(inner[2], list) and len(inner[2]) > 2:
                ts_data = inner[2][2]
                if isinstance(ts_data, list) and len(ts_data) > 0:
                    try:
                        created_at = datetime.fromtimestamp(ts_data[0])
                    except (TypeError, ValueError):
                        pass

        return cls(
            id=str(mind_map_id),
            title=title,
            artifact_type=5,  # StudioContentType.MIND_MAP
            status=3,  # Mind maps are always "completed" once created
            created_at=created_at,
            variant=None,
        )

    @property
    def is_completed(self) -> bool:
        """Check if artifact generation is complete (status=3)."""
        return self.status == 3

    @property
    def is_processing(self) -> bool:
        """Check if artifact is being generated (status=1)."""
        return self.status == 1

    @property
    def is_pending(self) -> bool:
        """Check if artifact is queued/transitional (status=2)."""
        return self.status == 2

    @property
    def status_str(self) -> str:
        """Get human-readable status string.

        Returns:
            "in_progress", "pending", "completed", or "unknown".
        """
        return artifact_status_to_str(self.status)

    @property
    def is_quiz(self) -> bool:
        """Check if this is a quiz (type 4, variant 2)."""
        return self.artifact_type == 4 and self.variant == 2

    @property
    def is_flashcards(self) -> bool:
        """Check if this is flashcards (type 4, variant 1)."""
        return self.artifact_type == 4 and self.variant == 1

    @property
    def report_subtype(self) -> str | None:
        """Get the report subtype for type 2 artifacts.

        Returns:
            'briefing_doc', 'study_guide', 'blog_post', or None if not a report.
        """
        if self.artifact_type != 2:
            return None
        title_lower = self.title.lower()
        if title_lower.startswith("briefing doc"):
            return "briefing_doc"
        elif title_lower.startswith("study guide"):
            return "study_guide"
        elif title_lower.startswith("blog post"):
            return "blog_post"
        return "report"


@dataclass
class GenerationStatus:
    """Status of an artifact generation task.

    Note: task_id and artifact_id are the same identifier. The API returns a single
    ID when generation starts, which is used both for polling the task status during
    generation and as the artifact's ID once complete. We use 'task_id' here to
    emphasize its role in tracking the generation task.
    """

    task_id: str  # Same as artifact_id - used for polling and becomes Artifact.id
    status: str  # "pending", "in_progress", "completed", "failed"
    url: str | None = None
    error: str | None = None
    error_code: str | None = None  # e.g., "USER_DISPLAYABLE_ERROR" for rate limits
    metadata: dict[str, Any] | None = None

    @property
    def is_complete(self) -> bool:
        """Check if generation is complete."""
        return self.status == "completed"

    @property
    def is_failed(self) -> bool:
        """Check if generation failed."""
        return self.status == "failed"

    @property
    def is_pending(self) -> bool:
        """Check if generation is pending."""
        return self.status == "pending"

    @property
    def is_in_progress(self) -> bool:
        """Check if generation is in progress."""
        return self.status == "in_progress"

    @property
    def is_rate_limited(self) -> bool:
        """Check if generation failed due to rate limiting or quota exceeded.

        Returns True when the API rejected the request, typically due to
        too many requests or quota exhaustion.
        """
        if not self.is_failed:
            return False

        # Prefer structured error code when available
        if self.error_code == "USER_DISPLAYABLE_ERROR":
            return True

        # Fall back to string matching for backwards compatibility
        if self.error is not None:
            error_lower = self.error.lower()
            return "rate limit" in error_lower or "quota" in error_lower

        return False


@dataclass
class ReportSuggestion:
    """AI-suggested report format based on notebook sources."""

    title: str
    description: str
    prompt: str
    audience_level: int = 2  # 1=beginner, 2=advanced

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> "ReportSuggestion":
        """Parse from get_suggested_report_formats() response item."""
        return cls(
            title=data.get("title", ""),
            description=data.get("description", ""),
            prompt=data.get("prompt", ""),
            audience_level=data.get("audience_level", 2),
        )


# =============================================================================
# Note Types
# =============================================================================


@dataclass
class Note:
    """Represents a user-created note in a notebook.

    Notes are distinct from artifacts - they are user-created content,
    not AI-generated. Notes support different operations than artifacts
    (export to Docs/Sheets, convert to source).
    """

    id: str
    notebook_id: str
    title: str
    content: str
    created_at: datetime | None = None

    @classmethod
    def from_api_response(cls, data: list[Any], notebook_id: str) -> "Note":
        """Parse note from API response.

        Args:
            data: Raw API response list.
            notebook_id: The parent notebook ID.

        Returns:
            Note instance.
        """
        note_id = data[0] if len(data) > 0 else ""
        title = data[1] if len(data) > 1 else ""
        content = data[2] if len(data) > 2 else ""

        created_at = None
        if len(data) > 3 and isinstance(data[3], list) and len(data[3]) > 0:
            try:
                created_at = datetime.fromtimestamp(data[3][0])
            except (TypeError, ValueError):
                pass

        return cls(
            id=str(note_id),
            notebook_id=notebook_id,
            title=str(title),
            content=str(content),
            created_at=created_at,
        )


# =============================================================================
# Conversation Types
# =============================================================================


@dataclass
class ConversationTurn:
    """Represents a single turn in a conversation."""

    query: str
    answer: str
    turn_number: int


@dataclass
class ChatReference:
    """A reference/citation in a chat response.

    References link parts of the answer to specific sources.
    When you click a reference in the NotebookLM UI, it shows
    the relevant passage from the source.

    Attributes:
        source_id: The source UUID this reference points to.
        citation_number: The citation number shown in the answer (e.g., [1], [2]).
        cited_text: The actual text passage from the source being cited.
        start_char: Start character position in the source content (if available).
        end_char: End character position in the source content (if available).
        chunk_id: Internal chunk ID (for debugging, not user-facing).
    """

    source_id: str
    citation_number: int | None = None
    cited_text: str | None = None
    start_char: int | None = None
    end_char: int | None = None
    chunk_id: str | None = None


@dataclass
class AskResult:
    """Result of asking the notebook a question.

    Attributes:
        answer: The AI-generated answer text.
        conversation_id: UUID for this conversation (used for follow-ups).
        turn_number: The turn number in the conversation.
        is_follow_up: Whether this was a follow-up question.
        references: List of source references cited in the answer.
        raw_response: First 1000 chars of raw API response (for debugging).
    """

    answer: str
    conversation_id: str
    turn_number: int
    is_follow_up: bool
    references: list["ChatReference"] = field(default_factory=list)
    raw_response: str = ""
