"""Private artifact type implementations."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .._row_adapters_artifacts import ArtifactRow
from ..rpc.types import (
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    ArtifactStatus,
    ArtifactTypeCode,
    artifact_status_to_str,
)
from .common import UnknownTypeWarning, _datetime_from_timestamp


class ArtifactType(str, Enum):
    """User-facing artifact types.

    This is a str enum that hides internal variant complexity. For example,
    quizzes and flashcards are both type 4 internally but distinguished by variant.

    Comparisons work with both enum members and strings:
        artifact.kind == ArtifactType.AUDIO  # True
        artifact.kind == "audio"             # Also True
    """

    AUDIO = "audio"
    VIDEO = "video"
    REPORT = "report"
    QUIZ = "quiz"
    FLASHCARDS = "flashcards"
    MIND_MAP = "mind_map"
    INFOGRAPHIC = "infographic"
    SLIDE_DECK = "slide_deck"
    DATA_TABLE = "data_table"
    UNKNOWN = "unknown"


_warned_artifact_types: set[tuple[int, int | None]] = set()


_ARTIFACT_TYPE_CODE_MAP: dict[int, ArtifactType] = {
    1: ArtifactType.AUDIO,
    2: ArtifactType.REPORT,
    3: ArtifactType.VIDEO,
    5: ArtifactType.MIND_MAP,
    7: ArtifactType.INFOGRAPHIC,
    8: ArtifactType.SLIDE_DECK,
    9: ArtifactType.DATA_TABLE,
}


def _map_artifact_kind(artifact_type: int, variant: int | None) -> ArtifactType:
    """Convert internal artifact type and variant to user-facing ArtifactType.

    Args:
        artifact_type: ArtifactTypeCode integer value from API.
        variant: Optional variant code (e.g., for quiz vs flashcards).

    Returns:
        ArtifactType enum member. Returns UNKNOWN for unrecognized types.
    """
    # Handle QUIZ/FLASHCARDS distinction.
    if artifact_type == ArtifactTypeCode.QUIZ.value:
        if variant == FLASHCARDS_VARIANT:
            return ArtifactType.FLASHCARDS
        elif variant == QUIZ_VARIANT:
            return ArtifactType.QUIZ
        elif variant == INTERACTIVE_MIND_MAP_VARIANT:
            # Interactive mind map: a studio artifact in the type-4 family,
            # distinct from the note-backed mind map (synthetic type 5).
            return ArtifactType.MIND_MAP
        else:
            key = (artifact_type, variant)
            if key not in _warned_artifact_types:
                _warned_artifact_types.add(key)
                warnings.warn(
                    f"Unknown QUIZ variant {variant}. "
                    "Consider updating notebooklm-py to the latest version.",
                    UnknownTypeWarning,
                    stacklevel=3,
                )
            return ArtifactType.UNKNOWN

    result = _ARTIFACT_TYPE_CODE_MAP.get(artifact_type)
    if result is None:
        key = (artifact_type, variant)
        if key not in _warned_artifact_types:
            _warned_artifact_types.add(key)
            warnings.warn(
                f"Unknown artifact type {artifact_type}. "
                "Consider updating notebooklm-py to the latest version.",
                UnknownTypeWarning,
                stacklevel=3,
            )
        return ArtifactType.UNKNOWN
    return result


def _is_valid_artifact_url(value: Any) -> bool:
    """Return True when ``value`` looks like a downloadable artifact URL."""
    return isinstance(value, str) and value.startswith(("http://", "https://"))


def _extract_audio_artifact_url(data: list[Any]) -> str | None:
    return ArtifactRow(data).artifact_url(ArtifactTypeCode.AUDIO.value, suppress_drift=True)


def _extract_video_artifact_url(data: list[Any]) -> str | None:
    return ArtifactRow(data).artifact_url(ArtifactTypeCode.VIDEO.value, suppress_drift=True)


def _extract_infographic_artifact_url(data: list[Any]) -> str | None:
    return ArtifactRow(data).artifact_url(ArtifactTypeCode.INFOGRAPHIC.value, suppress_drift=True)


def _extract_slide_deck_artifact_url(data: list[Any]) -> str | None:
    """Extract the slide-deck PDF URL. The PPTX URL at ``data[16][4]`` is not
    surfaced — callers wanting PPTX should use ``download_slide_deck(output_format="pptx")``."""
    return ArtifactRow(data).artifact_url(ArtifactTypeCode.SLIDE_DECK.value, suppress_drift=True)


def _extract_artifact_url(data: list[Any], artifact_type: int | None) -> str | None:
    """Extract a public download URL from known artifact response shapes."""
    if artifact_type is None:
        return None
    return ArtifactRow(data).artifact_url(artifact_type, suppress_drift=True)


@dataclass
class Artifact:
    """Represents a NotebookLM artifact (studio content).

    Artifacts are AI-generated content like Audio Overviews, Video Overviews,
    Reports, Quizzes, Flashcards, Mind Maps, Infographics, Slide Decks, and
    Data Tables.

    Attributes:
        id: Unique artifact identifier.
        title: Artifact title.
        kind: Artifact type as ArtifactType enum (str enum, comparable to strings).
        status: Processing status (1=processing, 2=pending, 3=completed, 4=failed).
        created_at: When the artifact was created.
        url: Download URL (if available). For slide decks this is the PDF URL
            only — PPTX is fetched separately via ``download_slide_deck(output_format="pptx")``.

    Example:
        artifact.kind == ArtifactType.AUDIO  # True
        artifact.kind == "audio"             # Also True (str enum)
        f"Type: {artifact.kind}"             # "Type: audio"
    """

    id: str
    title: str
    _artifact_type: int = field(repr=False)  # ArtifactTypeCode enum value
    status: int  # 1=processing, 2=pending, 3=completed, 4=failed
    created_at: datetime | None = None
    url: str | None = None
    _variant: int | None = field(
        default=None, repr=False
    )  # For type 4: 1=flashcards, 2=quiz, 4=interactive_mind_map

    @property
    def kind(self) -> ArtifactType:
        """Get artifact type as ArtifactType enum.

        Returns:
            ArtifactType enum member. Returns ArtifactType.UNKNOWN for
            unrecognized type codes (with a warning on first occurrence).
        """
        return _map_artifact_kind(self._artifact_type, self._variant)

    @classmethod
    def from_api_response(cls, data: list[Any]) -> Artifact:
        """Parse artifact from API response.

        Position knowledge for ``id`` / ``title`` / ``type`` / ``status``
        / ``variant`` / ``timestamp`` lives in
        :class:`notebooklm._row_adapters_artifacts.ArtifactRow`. This factory wraps
        the raw row in an adapter and reads through its typed properties,
        so any wire-shape change touches the adapter constants only.

        URL extraction reads through :class:`ArtifactRow`; the private
        ``_extract_artifact_url`` helper remains only as a compatibility
        shim for downstream private imports.
        """
        row = ArtifactRow(data)
        artifact_type = row.type_code
        # ``row.type_code`` is statically typed ``int`` and normalises
        # non-ints to ``0``; ``row.artifact_url`` then falls through to
        # ``None`` for unrecognised codes — no separate ``isinstance``
        # guard is needed here.
        url = row.artifact_url(artifact_type, suppress_drift=True)

        return cls(
            id=row.id,
            title=row.title,
            _artifact_type=artifact_type,
            status=row.status,
            created_at=row.created_at,
            url=url,
            _variant=row.variant,
        )

    @classmethod
    def from_mind_map(cls, data: list[Any]) -> Artifact | None:
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
                    created_at = _datetime_from_timestamp(ts_data[0])

        return cls(
            id=str(mind_map_id),
            title=title,
            _artifact_type=ArtifactTypeCode.MIND_MAP.value,
            status=3,  # Mind maps are always "completed" once created
            created_at=created_at,
            _variant=None,
        )

    @property
    def is_completed(self) -> bool:
        """Check if artifact generation is complete (status=COMPLETED)."""
        return self.status == ArtifactStatus.COMPLETED

    @property
    def is_processing(self) -> bool:
        """Check if artifact is being generated (status=PROCESSING)."""
        return self.status == ArtifactStatus.PROCESSING

    @property
    def is_pending(self) -> bool:
        """Check if artifact is queued/transitional (status=PENDING)."""
        return self.status == ArtifactStatus.PENDING

    @property
    def is_failed(self) -> bool:
        """Check if artifact generation failed (status=FAILED)."""
        return self.status == ArtifactStatus.FAILED

    @property
    def status_str(self) -> str:
        """Get human-readable status string.

        Returns:
            "in_progress", "pending", "completed", "failed", or "unknown".
        """
        return artifact_status_to_str(self.status)

    @property
    def is_quiz(self) -> bool:
        """Check if this is a quiz (type 4, variant 2)."""
        return self._artifact_type == ArtifactTypeCode.QUIZ.value and self._variant == QUIZ_VARIANT

    @property
    def is_flashcards(self) -> bool:
        """Check if this is flashcards (type 4, variant 1)."""
        return (
            self._artifact_type == ArtifactTypeCode.QUIZ.value
            and self._variant == FLASHCARDS_VARIANT
        )

    @property
    def is_interactive_mind_map(self) -> bool:
        """Whether this is an interactive (studio-artifact) mind map.

        Interactive mind maps are studio artifacts in the type-4 family
        (``type 4 / variant 4``), as opposed to note-backed mind maps which
        the library surfaces with the synthetic type code 5. Both report
        ``kind == ArtifactType.MIND_MAP``; this distinguishes the backing.
        """
        return (
            self._artifact_type == ArtifactTypeCode.QUIZ.value
            and self._variant == INTERACTIVE_MIND_MAP_VARIANT
        )

    @property
    def report_subtype(self) -> str | None:
        """Get the report subtype for type 2 artifacts.

        Returns:
            'briefing_doc', 'study_guide', 'blog_post', or None if not a report.
        """
        if self._artifact_type != ArtifactTypeCode.REPORT.value:
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
    status: str  # "pending", "in_progress", "completed", "failed", "not_found", "removed"
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
    def is_not_found(self) -> bool:
        """Check if the artifact was not found in the poll response.

        This status is set by ``poll_status()`` when the artifact ID is
        absent from the artifact list.  It differs from ``is_pending``:
        a ``pending`` artifact exists in the list and is queued, while a
        ``not_found`` artifact has either not yet appeared (brief lag after
        creation) or was silently removed by the server (e.g. after a
        daily-quota rejection).

        ``wait_for_completion`` treats a sustained run of ``not_found``
        responses as a *removal* — see its ``max_not_found`` parameter and
        :attr:`is_removed`.
        """
        return self.status == "not_found"

    @property
    def is_removed(self) -> bool:
        """Check if the artifact was delisted by the server.

        This status is set by ``wait_for_completion()`` when an artifact
        disappears from the listing for a sustained run of polls (see its
        ``max_not_found`` parameter). It is deliberately *distinct* from
        :attr:`is_failed`: a ``failed`` artifact still exists in the listing
        with a terminal FAILED status, whereas a ``removed`` artifact vanished
        from the listing entirely — typically after a daily-quota rejection,
        but possibly a transient list omission. Conflating the two would mask
        a genuine terminal failure as a transient hiccup, or vice versa, so
        callers that need to react differently can branch on this property.
        """
        return self.status == "removed"

    @property
    def is_rate_limited(self) -> bool:
        """Check if generation failed due to rate limiting or quota exceeded.

        Returns True when the API rejected the request, typically due to
        too many requests or quota exhaustion. A ``removed`` status (the
        artifact was delisted, often after a quota rejection) is treated the
        same as a ``failed`` status here so that rate-limit retry policies
        keep working when the server silently drops the artifact.
        """
        if not (self.is_failed or self.is_removed):
            return False

        # Prefer structured error code when available
        if self.error_code == "USER_DISPLAYABLE_ERROR":
            return True

        # Fall back to string matching for backwards compatibility
        if self.error is not None:
            error_lower = self.error.lower()
            return (
                "rate limit" in error_lower
                or "quota" in error_lower
                or "limit exceeded" in error_lower
            )

        return False


@dataclass
class ReportSuggestion:
    """AI-suggested report format based on notebook sources."""

    title: str
    description: str
    prompt: str
    audience_level: int = 2  # 1=beginner, 2=advanced

    @classmethod
    def from_api_response(cls, data: dict[str, Any]) -> ReportSuggestion:
        """Parse a dict item from get_suggested_report_formats()."""
        return cls(
            title=data.get("title", ""),
            description=data.get("description", ""),
            prompt=data.get("prompt", ""),
            audience_level=data.get("audience_level", 2),
        )
