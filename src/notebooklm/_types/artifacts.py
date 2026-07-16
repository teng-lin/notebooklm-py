"""Private artifact type implementations."""

from __future__ import annotations

import logging
import reprlib
import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from .._row_adapters.artifacts import ArtifactRow
from .._row_adapters.notes import NoteRow
from ..exceptions import UnknownRPCMethodError
from ..rpc.types import (
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    ArtifactStatus,
    ArtifactTypeCode,
    artifact_status_to_str,
)
from .common import UnknownTypeWarning

logger = logging.getLogger(__name__)


class ArtifactType(str, Enum):
    """User-facing artifact types.

    This is a str enum that hides internal variant complexity. For example,
    quizzes, flashcards, and interactive mind maps are all type 4 internally,
    distinguished by variant.

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
        artifact_type: Raw ArtifactTypeCode integer from LIST_ARTIFACTS, or the
            library's synthetic note-backed mind-map code.
        variant: Optional variant code (e.g., for quiz vs flashcards vs
            interactive mind map).

    Returns:
        ArtifactType enum member. Returns UNKNOWN for unrecognized types.
    """
    # Resolve the type-4 family (QUIZ / FLASHCARDS / interactive mind map) by variant.
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
                    f"Unknown type-4 (quiz / flashcards / mind-map) variant {variant}. "
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
    #: The free-text prompt this artifact was generated from, or ``None`` (e.g. a
    #: note-backed mind map, or a type whose prompt slot is absent). Decoded from
    #: the listing row so a listing surfaces it without a per-artifact fetch
    #: (#1925). ``None`` on prompt-position drift — the read is guarded so a
    #: reshaped payload never breaks ``artifacts.list``.
    generation_prompt: str | None = None

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
        :class:`notebooklm._row_adapters.artifacts.ArtifactRow`. This factory wraps
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

        # The generation prompt is a nice-to-have listing field, not core to an
        # artifact's identity — guard its (type-specific) nested read so a prompt-
        # position drift degrades to ``None`` rather than breaking every listing.
        try:
            generation_prompt = row.generation_prompt
        except UnknownRPCMethodError:
            generation_prompt = None

        return cls(
            id=row.id,
            title=row.title,
            _artifact_type=artifact_type,
            status=row.status,
            created_at=row.created_at,
            url=url,
            _variant=row.variant,
            generation_prompt=generation_prompt,
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

        Mind-map rows ARE note-system rows (they come from
        ``GET_NOTES_AND_MIND_MAPS``), so the id slot, the title, and the
        deleted-tombstone predicate are read through
        :class:`notebooklm._row_adapters.notes.NoteRow` — position knowledge
        lives in the adapter, not here. A ``None`` content slot *without* the
        recognised ``[id, None, 2]`` tombstone is sentinel drift (a deleted
        mind map would otherwise silently leak as live): it logs a WARNING and
        conservatively keeps the historical treat-as-live fallthrough.

        Returns:
            Artifact object, or None for the note-system delete tombstone
            ``[id, None, 2]``.
        """
        if not isinstance(data, list) or len(data) < 1:
            return None

        row = NoteRow(data)

        # Deleted tombstone ([id, None, 2]): excluded from listings.
        if row.is_deleted:
            return None
        if row.has_unrecognized_tombstone:
            logger.warning(
                "Mind-map row %s has a null content slot without the "
                "soft-delete sentinel (tombstone drift? a deleted mind map "
                "may be leaking as live): %s",
                row.id,
                reprlib.repr(data),
            )

        # Title and the creation timestamp both come through the adapter:
        # the timestamp slot (``row[1][2][2][0]``) is the SAME one ``NoteRow``
        # decodes for the note path (issue #1529), so the position knowledge
        # lives in one place rather than re-open-coding the inner descent here.
        title = row.title
        created_at = row.created_at

        return cls(
            id=row.id,
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
    def is_unclassified_type4(self) -> bool:
        """Whether this is a type-4 artifact whose variant slot is not yet populated.

        A just-created interactive mind map (or quiz/flashcards) is a type-4
        (QUIZ-family) artifact, but the variant code at ``[9][1][0]`` may read
        ``None`` for a brief window after creation before the options block
        fills in. During that window the row is neither classifiable as
        interactive-mind-map nor quiz/flashcards. Callers that resolved a
        concrete id (e.g. ``MindMapsAPI._find_interactive`` after
        ``CREATE_ARTIFACT``) use this to id-match the settling artifact rather
        than degrading to a placeholder (issue #1270).
        """
        return self._artifact_type == ArtifactTypeCode.QUIZ.value and self._variant is None

    @property
    def report_subtype(self) -> str | None:
        """Get the report subtype for type 2 artifacts.

        Returns:
            'briefing_doc', 'study_guide', 'blog_post', 'report', or None if
            not a report.
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


class GenerationState(str, Enum):
    """The status string of an artifact generation task.

    A ``str`` enum so existing string comparisons (``status == "completed"``),
    membership checks, ``json.dumps``, ``f"{status}"``, and ``str(status)`` all
    keep working unchanged. Member names map 1:1 onto the historical status
    strings emitted by the poll/wait loops.
    """

    # poll-set: emitted by poll_status() / the generation parsers
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"
    # wait-only: emitted by wait_for_completion() on a sustained delisting
    REMOVED = "removed"

    def __str__(self) -> str:
        # Keep str(member) == member.value (e.g. "completed", not
        # "GenerationState.COMPLETED") so display/serialization is unchanged.
        return self.value

    def __repr__(self) -> str:
        # Keep repr(member) == repr(member.value) so console.print(status)
        # of a GenerationStatus dataclass renders identically to the old
        # plain-string field.
        return repr(self.value)


def _status_from_code(
    code: int | None, *, none_status: GenerationState = GenerationState.PENDING
) -> GenerationState:
    """Map an API status code to a :class:`GenerationState` member.

    ``None`` (no status reported yet) maps to ``none_status`` (``PENDING`` by
    default). Any recognized code maps via :func:`artifact_status_to_str`;
    unrecognized codes funnel to ``GenerationState.UNKNOWN``.

    The range pin in ``tests/unit/test_generation_state.py`` proves that every
    string ``artifact_status_to_str`` can return is a defined member today, so
    the ``GenerationState(...)`` call cannot raise. The ``ValueError`` guard is
    pure defense-in-depth against future schema drift (a new entry added to
    ``_ARTIFACT_STATUS_MAP`` without a matching member): rather than blowing up
    a poll loop, an unmapped string degrades to ``UNKNOWN``, mirroring
    ``artifact_status_to_str``'s own "unknown for unrecognized codes" contract.
    """
    if code is None:
        return none_status
    try:
        return GenerationState(artifact_status_to_str(code))
    except ValueError:  # pragma: no cover - unreachable today (range pin), future-drift guard
        return GenerationState.UNKNOWN


@dataclass
class GenerationStatus:
    """Status of an artifact generation task.

    Note: task_id and artifact_id are the same identifier. The API returns a single
    ID when generation starts, which is used both for polling the task status during
    generation and as the artifact's ID once complete. We use 'task_id' here to
    emphasize its role in tracking the generation task.
    """

    task_id: str  # Same as artifact_id - used for polling and becomes Artifact.id
    # "pending", "in_progress", "completed", "failed", "not_found", "removed", "unknown".
    # Typed as GenerationState, but stays raw-string-permissive: instances built
    # with a plain str keep working (the .is_* predicates compare with ==).
    status: GenerationState
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
