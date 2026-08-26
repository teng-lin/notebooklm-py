"""Resolution and validation of the Studio generation inputs (P10 R5.1a).

ADR-0035 addendum D1(a) makes the eight ``artifact.generate_*`` definitions take
pre-resolved inputs: the port never sees "sources unspecified" or "language
unspecified", and it never judges an option vocabulary.  All three resolutions
live here.

Two of them are documented service-level defaults.  ``source_ids is None`` means
*every* source in the notebook, read through :class:`NotebookReadService` with
the family's own diagnostics mode; ``language is None`` means
:func:`~notebooklm._env.get_default_language`.  The third is option validation,
which rejects a value outside the reviewed vocabulary with the same
``BackendContractError`` the codec used to raise.

The *order* of validation and resolution is behaviour, not detail, and it is not
uniform: the media families reject an unreviewed option before spending a read,
while the document families resolve the source set first.  Each family states
its own order below, and
``tests/unit/test_semantic_studio_generation_characterization.py`` pins it.
"""

from __future__ import annotations

from typing import Final, Protocol

from .._backend import BackendContractError
from .._deadline import RuntimeDeadline, RuntimeDeadlineFactory
from .._env import get_default_language
from .._operations import Operation
from .._read_services import NotebookReadService
from .._records import (
    AudioGenerateInput,
    AudioGenerateRequest,
    DataTableGenerateInput,
    DataTableGenerateRequest,
    InfographicGenerateInput,
    InfographicGenerateRequest,
    InteractiveGenerateInput,
    InteractiveGenerateRequest,
    ReportGenerateInput,
    ReportGenerateRequest,
    SlideDeckGenerateInput,
    SlideDeckGenerateRequest,
    SourceIdDiagnostics,
    VideoGenerateInput,
    VideoGenerateRequest,
)

#: The reviewed neutral option vocabularies.  ``tests/unit/test_generation_option_vocabularies.py``
#: pins each one against the wire-enum map the codec keys by the same strings, so
#: the two cannot drift apart across the port.
AUDIO_FORMATS: Final = frozenset({"deep_dive", "brief", "critique", "debate"})
AUDIO_LENGTHS: Final = frozenset({"short", "default", "long"})
QUIZ_QUANTITIES: Final = frozenset({"fewer", "standard", "more"})
QUIZ_DIFFICULTIES: Final = frozenset({"easy", "medium", "hard"})
INFOGRAPHIC_ORIENTATIONS: Final = frozenset({"landscape", "portrait", "square"})
INFOGRAPHIC_DETAILS: Final = frozenset({"concise", "standard", "detailed"})
INFOGRAPHIC_STYLES: Final = frozenset(
    {
        "auto_select",
        "sketch_note",
        "professional",
        "bento_grid",
        "editorial",
        "instructional",
        "bricks",
        "clay",
        "anime",
        "kawaii",
        "scientific",
    }
)
SLIDE_DECK_FORMATS: Final = frozenset({"detailed_deck", "presenter_slides"})
SLIDE_DECK_LENGTHS: Final = frozenset({"default", "short"})
VIDEO_FORMATS: Final = frozenset({"explainer", "brief", "cinematic", "short"})
VIDEO_STYLES: Final = frozenset(
    {
        "auto_select",
        "custom",
        "classic",
        "whiteboard",
        "kawaii",
        "anime",
        "watercolor",
        "retro_print",
        "heritage",
        "paper_craft",
    }
)
REPORT_FORMATS: Final = frozenset(
    {"briefing_doc", "study_guide", "blog_post", "concept_explanation", "custom"}
)


class _SourceScopedRequest(Protocol):
    """The two fields the source-set default reads off every request."""

    @property
    def notebook_id(self) -> str: ...

    @property
    def source_ids(self) -> tuple[str, ...] | None: ...


def _check(
    value: str | None,
    vocabulary: frozenset[str],
    *,
    description: str,
    operation: Operation,
) -> None:
    """Reject one option outside its reviewed vocabulary; ``None`` takes the default."""
    if value is not None and value not in vocabulary:
        raise BackendContractError(
            f"unrecognized {description} {value!r}",
            operation=operation,
        )


def _validate_audio_options(request: AudioGenerateRequest) -> None:
    """Reject unreviewed audio options, format before length."""
    operation = Operation.ARTIFACT_GENERATE_AUDIO
    _check(request.audio_format, AUDIO_FORMATS, description="audio format", operation=operation)
    _check(request.audio_length, AUDIO_LENGTHS, description="audio length", operation=operation)


def _validate_interactive_options(
    request: InteractiveGenerateRequest, *, operation: Operation
) -> None:
    """Reject unreviewed quiz/flashcard options, quantity before difficulty."""
    _check(
        request.quantity,
        QUIZ_QUANTITIES,
        description="interactive quantity",
        operation=operation,
    )
    _check(
        request.difficulty,
        QUIZ_DIFFICULTIES,
        description="interactive difficulty",
        operation=operation,
    )


def _validate_infographic_options(request: InfographicGenerateRequest) -> None:
    """Reject unreviewed infographic options: orientation, detail level, style."""
    operation = Operation.ARTIFACT_GENERATE_INFOGRAPHIC
    _check(
        request.orientation,
        INFOGRAPHIC_ORIENTATIONS,
        description="visual orientation",
        operation=operation,
    )
    _check(
        request.detail_level,
        INFOGRAPHIC_DETAILS,
        description="visual detail level",
        operation=operation,
    )
    _check(request.style, INFOGRAPHIC_STYLES, description="visual style", operation=operation)


def _validate_slide_deck_options(request: SlideDeckGenerateRequest) -> None:
    """Reject unreviewed slide-deck options, format before length."""
    operation = Operation.ARTIFACT_GENERATE_SLIDE_DECK
    _check(
        request.slide_format,
        SLIDE_DECK_FORMATS,
        description="visual format",
        operation=operation,
    )
    _check(
        request.slide_length,
        SLIDE_DECK_LENGTHS,
        description="visual length",
        operation=operation,
    )


def _validate_video_options(request: VideoGenerateRequest) -> None:
    """Reject unreviewed video options.

    The cinematic route encodes no style, so — exactly as the payload encoder
    did — it validates the format and stops there.
    """
    operation = Operation.ARTIFACT_GENERATE_VIDEO
    _check(request.video_format, VIDEO_FORMATS, description="video option", operation=operation)
    if request.cinematic_route:
        return
    _check(request.video_style, VIDEO_STYLES, description="video option", operation=operation)


def _validate_report_format(request: ReportGenerateRequest) -> None:
    """Reject a report format outside the reviewed set."""
    _check(
        request.report_format,
        REPORT_FORMATS,
        description="report format",
        operation=Operation.ARTIFACT_GENERATE_REPORT,
    )


class StudioGenerationInputs:
    """Turn one caller's generation request into the port's pre-resolved input."""

    __slots__ = ("_deadline_factory", "_notebooks")

    def __init__(
        self,
        notebooks: NotebookReadService,
        *,
        deadline_factory: RuntimeDeadlineFactory | None = None,
    ) -> None:
        self._notebooks = notebooks
        # The default-source read and the kickoff it feeds are one caller
        # operation, so they share one budget — see :func:`_generation_budget`.
        self._deadline_factory = deadline_factory

    async def audio(
        self, request: AudioGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> AudioGenerateInput:
        """Audio validates first, then reads — and its read never warns."""
        _validate_audio_options(request)
        return AudioGenerateInput(
            request.notebook_id,
            await self._source_ids(
                request, diagnostics=SourceIdDiagnostics.SILENT, deadline=deadline
            ),
            self._language(request.language),
            request.instructions,
            request.audio_format,
            request.audio_length,
        )

    async def quiz(
        self, request: InteractiveGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> InteractiveGenerateInput:
        """Quizzes validate first, then read."""
        return await self._interactive(
            request, operation=Operation.ARTIFACT_GENERATE_QUIZ, deadline=deadline
        )

    async def flashcards(
        self, request: InteractiveGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> InteractiveGenerateInput:
        """Flashcards validate first, then read."""
        return await self._interactive(
            request, operation=Operation.ARTIFACT_GENERATE_FLASHCARDS, deadline=deadline
        )

    async def infographic(
        self, request: InfographicGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> InfographicGenerateInput:
        """Infographics validate first, then read."""
        _validate_infographic_options(request)
        return InfographicGenerateInput(
            request.notebook_id,
            await self._source_ids(
                request, diagnostics=SourceIdDiagnostics.WARN, deadline=deadline
            ),
            self._language(request.language),
            request.instructions,
            request.orientation,
            request.detail_level,
            request.style,
        )

    async def slide_deck(
        self, request: SlideDeckGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> SlideDeckGenerateInput:
        """Slide decks validate first, then read."""
        _validate_slide_deck_options(request)
        return SlideDeckGenerateInput(
            request.notebook_id,
            await self._source_ids(
                request, diagnostics=SourceIdDiagnostics.WARN, deadline=deadline
            ),
            self._language(request.language),
            request.instructions,
            request.slide_format,
            request.slide_length,
        )

    async def data_table(
        self, request: DataTableGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> DataTableGenerateInput:
        """The data-table family validates nothing; it only resolves."""
        return DataTableGenerateInput(
            request.notebook_id,
            await self._source_ids(
                request, diagnostics=SourceIdDiagnostics.WARN, deadline=deadline
            ),
            self._language(request.language),
            request.instructions,
        )

    async def report(
        self, request: ReportGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> ReportGenerateInput:
        """The document families resolve the source set *before* they validate."""
        source_ids = await self._source_ids(
            request, diagnostics=SourceIdDiagnostics.WARN, deadline=deadline
        )
        _validate_report_format(request)
        return ReportGenerateInput(
            request.notebook_id,
            source_ids,
            self._language(request.language),
            request.report_format,
            request.custom_prompt,
            request.extra_instructions,
        )

    async def video(
        self, request: VideoGenerateRequest, *, deadline: RuntimeDeadline | None
    ) -> VideoGenerateInput:
        """The document families resolve the source set *before* they validate."""
        source_ids = await self._source_ids(
            request, diagnostics=SourceIdDiagnostics.WARN, deadline=deadline
        )
        _validate_video_options(request)
        return VideoGenerateInput(
            request.notebook_id,
            source_ids,
            self._language(request.language),
            request.instructions,
            request.video_format,
            request.video_style,
            request.style_prompt,
            request.cinematic_route,
        )

    async def _interactive(
        self,
        request: InteractiveGenerateRequest,
        *,
        operation: Operation,
        deadline: RuntimeDeadline | None,
    ) -> InteractiveGenerateInput:
        _validate_interactive_options(request, operation=operation)
        return InteractiveGenerateInput(
            request.notebook_id,
            await self._source_ids(
                request, diagnostics=SourceIdDiagnostics.WARN, deadline=deadline
            ),
            request.instructions,
            request.quantity,
            request.difficulty,
        )

    async def _source_ids(
        self,
        request: _SourceScopedRequest,
        *,
        diagnostics: SourceIdDiagnostics,
        deadline: RuntimeDeadline | None,
    ) -> tuple[str, ...]:
        """``None`` sources mean the notebook's whole embedded source set."""
        if request.source_ids is not None:
            return request.source_ids
        return tuple(
            await self._notebooks.get_source_ids(
                request.notebook_id, diagnostics=diagnostics, deadline=deadline
            )
        )

    @staticmethod
    def _language(language: str | None) -> str:
        """``None`` language means the environment default (never ``"en"`` here)."""
        return get_default_language() if language is None else language


def _generation_budget(
    inputs: StudioGenerationInputs, deadline: RuntimeDeadline | None
) -> RuntimeDeadline | None:
    """Capture the one client budget a generate call spends, before the read.

    Until P10 R5.1a the row itself issued both natives, so ``WebRpcBackend``
    seeded the client timeout once for the whole operation (the multi-native
    deadline ledger).  The rows are single-native now and the default-source
    read happens up here, so the family service captures that budget instead —
    the same absolute identity reaches ``NOTEBOOK_GET`` and ``CREATE_ARTIFACT``.
    Package-private: the families import it, but a public callable returning a
    ``RuntimeDeadline`` would breach P10 invariant I1.
    """
    if deadline is not None or inputs._deadline_factory is None:
        return deadline
    return inputs._deadline_factory.start()


__all__ = ["StudioGenerationInputs"]
