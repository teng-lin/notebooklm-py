"""Artifact row adapter for raw ``LIST_ARTIFACTS`` response rows."""

from __future__ import annotations

import logging
import reprlib
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from ..._types.artifact_content import (
    ArtifactInfographic,
    ArtifactMedia,
    ArtifactMediaType,
    ArtifactSlide,
    ArtifactUserState,
    AudioArtifactUserState,
    FlashcardArtifactUserState,
    UnknownArtifactUserState,
)
from ..._types.common import _datetime_from_timestamp
from ..._types.enums import FLASHCARDS_VARIANT, ArtifactStatus, ArtifactTypeCode
from ...exceptions import UnknownRPCMethodError
from ...rpc import RPCMethod, safe_index

__all__ = [
    "MIND_MAP_LEAF_ABSENT",
    "ArtifactRow",
    "QuizOptionPair",
    "ReportSuggestionRow",
    "extract_interactive_tree_leaf",
    "unwrap_artifact_rows",
    "unwrap_mind_map_generation_leaf",
]

_INTERACTIVE_TREE_LEAF_POS = 3

# Preserve the historical observable logger name after the decoder move.
_mind_maps_logger = logging.getLogger("notebooklm._mind_maps_api")


def extract_interactive_tree_leaf(result: Any, *, source: str) -> Any | None:
    """Return the raw ``[0][9][3]`` interactive mind-map tree leaf, or ``None``.

    Distinguishes a *genuinely absent leaf* (the options block is a list but
    its ``[3]`` tree slot is not populated yet — the legitimate "not ready"
    window) from *real shape drift* (``[0]`` or ``[0][9]`` moved out from under
    us, or ``[0][9]`` is no longer a list). Drift re-raises
    ``UnknownRPCMethodError`` so the library fails loud like the sibling HTML
    accessor ``_get_artifact_content`` (issue #1270); only the missing ``[3]``
    leaf within a present *list* options block is tolerated. A tolerated-but-
    absent leaf emits a WARNING with the rpcid/source so a reshape that drops
    just the leaf position still leaves a drift signal in the logs.
    """
    if result is None:
        return None
    # Descend to the options block (``[0][9]``) strictly: if Google moves the
    # interactive payload off ``[0][9]`` entirely, this raises and surfaces the
    # drift instead of masquerading as "not ready".
    options_block = safe_index(
        result,
        0,
        9,
        method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
        source=source,
    )
    # Only a *list* options block too short for index 3 is the legitimate
    # "tree not populated yet" window — a non-list ``[0][9]`` is genuine drift,
    # so fail loud rather than masking it as not-ready. (We raise explicitly
    # rather than via ``safe_index`` because some non-list types — e.g. ``str``
    # — are subscriptable and would not trip ``safe_index``'s descent guard.)
    if not isinstance(options_block, list):
        raise UnknownRPCMethodError(
            f"safe_index drift at path (0, 9): options block is "
            f"{type(options_block).__name__}, not a list",
            method_id=RPCMethod.GET_INTERACTIVE_HTML.value,
            path=(0, 9),
            source=source,
            # ``reprlib.repr`` bounds the diagnostic preview without first
            # materialising the full repr of a pathologically large/deep
            # ``options_block`` (mirrors ``safe_index``'s own ``_truncate``).
            data_at_failure=reprlib.repr(options_block),
        )
    if len(options_block) <= _INTERACTIVE_TREE_LEAF_POS:
        _mind_maps_logger.warning(
            "Interactive mind-map tree leaf absent at [0][9][%d] (rpcid=%s, source=%s); "
            "treating as not-yet-populated. If this persists, Google may have reshaped "
            "the %s response.",
            _INTERACTIVE_TREE_LEAF_POS,
            RPCMethod.GET_INTERACTIVE_HTML.value,
            source,
            RPCMethod.GET_INTERACTIVE_HTML.name,
        )
        return None
    return options_block[_INTERACTIVE_TREE_LEAF_POS]


@dataclass(frozen=True)
class QuizOptionPair:
    """The quantity/difficulty pair a quiz or flashcards artifact was generated with.

    The backend stores this pair inside the artifact and echoes it back on
    ``LIST_ARTIFACTS``, which makes it the one place the *sent* options can be
    verified against the *stored* options. Nothing read it before #2195, so the
    only thing standing between a wrong pair and the user was a hand-written
    fixture — which is exactly how #2116 (a transposed pair) survived.

    Both ``QuizGenerationOptions`` and ``FlashcardsGenerationOptions`` declare
    the same two fields in the same order, so one type covers both families —
    mirroring the shared :class:`~notebooklm.rpc.QuizQuantity` /
    :class:`~notebooklm.rpc.QuizDifficulty` enums.

    The fields are **named, not positional**, on purpose: a bare
    ``tuple[int, int]`` read-back would reintroduce on the decode side the very
    ambiguity that produced the transposition on the encode side.

    Values are raw ``int`` codes rather than enum members (matching
    :attr:`ArtifactRow.type_code` / :attr:`ArtifactRow.status`) so a value
    Google adds before this client models it decodes as itself instead of
    raising. ``QuizQuantity`` / ``QuizDifficulty`` are ``int`` enums, so
    ``pair.quantity == QuizQuantity.FEWER`` compares as expected.

    Either field is ``None`` when the stored options message leaves it unset —
    live-observed: a request carrying the proto3 default pair ``[0, 0]`` echoes
    back as an empty list, since default-valued fields are dropped from the
    JSON encoding. ``None`` *also* covers a leaf that is present but not an
    integer code; the two are not distinguishable here, and
    :meth:`ArtifactRow._option_code` records why that narrow conflation is
    accepted while malformed *containers* raise instead.

    Because the fields are plain ``int``, they compare equal across the two
    option enums — ``QuizOptionPair(quantity=3, ...).quantity ==
    QuizDifficulty.HARD`` is ``True``, since ``QuizQuantity.MORE`` and
    ``QuizDifficulty.HARD`` are both ``3``. Compare against the enum matching
    the field you are reading. The encode side rejects that mix-up outright
    (:func:`~notebooklm._web.params.artifacts._quiz_option_code`); the decode side
    cannot, because it has only the wire integer to go on.
    """

    quantity: int | None
    difficulty: int | None


def unwrap_artifact_rows(result: list[Any], *, method_id: str, source: str) -> list[Any]:
    """Unwrap a single-element ``[[row, ...]]`` artifact-list envelope.

    Both ``LIST_ARTIFACTS`` (``gArtLc``) and ``GET_SUGGESTED_REPORTS``
    (``ciyUvf``) return their rows as either a wrapped single-element envelope
    (``[[row1, row2, ...]]``) or an already-flat list of rows. This centralises
    the ``result[0]`` / ``inner[0]`` envelope-probe positions both call sites
    previously open-coded (issue #1491) so the wrap-detection knowledge lives in
    one place.

    The caller is responsible for the absence / drift policy on the *outer*
    payload (a falsy or non-list ``result`` never reaches here); this helper is a
    pure shape probe over a list and **never raises**:

    * the wrapped case (a single outer element whose first inner element is
      itself a list — a row — *or* an empty inner list) returns the unwrapped
      inner list; and
    * every other shape (already-flat rows, or an outer list whose lone element
      is a scalar) returns ``result`` unchanged.

    ``method_id`` / ``source`` are accepted for parity with the ``safe_index``
    seam and to localise future drift diagnostics, but are unused on the happy
    path because the probe only reads positions it has already length/`isinstance`
    guarded — so it degrades softly exactly as the prior inline reads did.

    Args:
        result: A truthy ``list`` payload (the caller guards falsy / non-list).
        method_id: RPC method id of the producing call (drift-diagnostic parity).
        source: Caller label for drift diagnostics.
    """
    # ``result`` is a non-empty list here (caller-guaranteed). The wrap probe
    # reads ``result[0]`` and ``inner[0]`` only after the matching length /
    # ``isinstance`` guards, so neither read can raise — this preserves the
    # historical permissive unwrap contract (no drift raise from the probe).
    if len(result) == 1 and isinstance(result[0], list):
        inner = safe_index(result, 0, method_id=method_id, source=source)
        if not inner or isinstance(safe_index(inner, 0, method_id=method_id, source=source), list):
            return inner
    return result


#: Sentinel returned by :func:`unwrap_mind_map_generation_leaf` when the
#: ``[[mind_map_json]]`` envelope structure is absent (a short / non-list
#: payload or inner list). It is distinct from a *present* leaf that is itself
#: ``None`` — the caller must process a present ``None`` leaf (it serialises to
#: a ``"null"`` note body) but skip the absent case, so the two cannot collapse
#: to a single ``None`` return.
MIND_MAP_LEAF_ABSENT: Any = object()


def unwrap_mind_map_generation_leaf(result: Any, *, method_id: str, source: str) -> Any:
    """Return the JSON leaf at ``result[0][0]`` of a ``GENERATE_MIND_MAP`` reply.

    The ``GENERATE_MIND_MAP`` (``yyryJe``, live method ``ActOnSources``) reply
    nests the mind-map JSON payload two levels deep (``[[mind_map_json]]``). This
    centralises the ``result[0]`` / ``inner[0]`` descent
    ``ArtifactsAPI.generate_mind_map`` previously open-coded (issue #1491).

    The descent is **soft** (preserving the historical contract): a short /
    non-list payload, or a short / non-list inner list, returns the
    :data:`MIND_MAP_LEAF_ABSENT` sentinel rather than raising — the caller maps
    that to its "no mind-map content produced" fall-through. A *present* leaf is
    returned verbatim, **including a present ``None``/``""`` leaf**, because the
    historical code processes those (a ``None`` leaf serialises to a ``"null"``
    note body); collapsing them into a plain ``None`` return would silently drop
    that case, so the sentinel is required. The two inner reads are guarded by
    ``isinstance`` + ``len`` so the ``safe_index`` seam never fires on the
    absence shapes.

    Args:
        result: Raw decoded ``GENERATE_MIND_MAP`` payload.
        method_id: RPC method id (drift-diagnostic parity).
        source: Caller label for drift diagnostics.
    """
    if not (isinstance(result, list) and len(result) > 0):
        return MIND_MAP_LEAF_ABSENT
    inner = safe_index(result, 0, method_id=method_id, source=source)
    if not (isinstance(inner, list) and len(inner) > 0):
        return MIND_MAP_LEAF_ABSENT
    return safe_index(inner, 0, method_id=method_id, source=source)


@dataclass(frozen=True)
class ArtifactRow:
    """Typed view of a raw artifact row from a ``LIST_ARTIFACTS`` response.

    The wrapped row is the per-artifact list returned by the ``gArtLc``
    (``LIST_ARTIFACTS``) RPC. Position layout:

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      artifact id (str)
    1      artifact title (str)
    2      type code (int — see :class:`notebooklm.rpc.ArtifactTypeCode`)
    3      source references; IDs are exposed through :attr:`source_ids`
    4      processing status (int — see :class:`notebooklm.rpc.ArtifactStatus`)
    5      isPubliclyReadable (bool — unread; see #2134)
    6      audio metadata; ``[6][5]`` is the audio media list
    7      report markdown payload (string or one-element wrapper)
    8      video metadata; nested media variants
    9      options block; ``[9][1][0]`` is the variant code (used to
           distinguish among QUIZ, FLASHCARDS, and the interactive mind map
           (variant 4) when type == 4); ``[9][1][2]`` is the generation prompt;
           ``[9][1][6]`` and ``[9][1][7]`` are the stored flashcards / quiz
           ``[quantity, difficulty]`` pairs
    10     last-modified timestamp (``[seconds, nanos]``)
    14     infographic metadata; ``[14][0][0]`` is the generation prompt
    15     timestamp block; ``[15][0]`` is the creation timestamp
           (seconds since epoch)
    16     slide deck metadata; ``[16][3]`` is PDF URL, ``[16][4]`` is PPTX
           URL, and ``[16][0][0]`` is the generation prompt
    17     per-user playback or app study state
    18     data table raw rich-text payload; ``[18][1][0]`` is the
           generation prompt
    21     etag (when returned)
    =====  ============================================================

    Each artifact also carries the free-text prompt that produced it, at a
    type-specific position inside the same top-level block that holds its
    rendered content (audio ``[6][1][0]``, report ``[7][1][5]``, video
    ``[8][2][2]``, type-4 ``[9][1][2]``, infographic ``[14][0][0]``, slide
    deck ``[16][0][0]``, data table ``[18][1][0]``). These are read through
    :attr:`generation_prompt`.

    Position knowledge is centralised here. Consumer sites should NEVER
    open-code ``data[2]`` / ``data[4]`` / ``data[15]`` — wrap the row in
    an :class:`ArtifactRow` and read through the typed properties
    instead.

    The dataclass is frozen so accidentally mutating the wrapped row is
    impossible through the adapter; the adapter itself never copies the
    raw row, so it is cheap to construct.
    """

    # Wrapped row; ``repr=False`` so logs don't explode with the entire
    # batchexecute payload when an ArtifactRow appears in a stack trace.
    _raw: list[Any] = field(repr=False)
    # ``method_id`` is intentionally a public extension point: callers
    # wrapping a row that came from a non-LIST_ARTIFACTS method override
    # it so ``safe_index`` drift diagnostics point at the correct RPC.
    # No leading underscore — see the related test
    # ``TestMethodIdPropagation::test_custom_method_id_propagates``.
    method_id: str = RPCMethod.LIST_ARTIFACTS.value

    # ---- Position constants (the canary contract) ------------------------
    # These are ClassVar so the frozen dataclass treats them as class-level
    # constants rather than instance fields. If any of these change,
    # ``tests/unit/test_row_adapters.py::TestPositionContract`` MUST be
    # updated in the same commit — that failure is the wire-shape change
    # signal.
    _ID_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = 1
    _TYPE_POS: ClassVar[int] = 2
    _SOURCES_POS: ClassVar[int] = 3
    _STATUS_POS: ClassVar[int] = 4
    _AUDIO_METADATA_POS: ClassVar[int] = 6
    _REPORT_MARKDOWN_POS: ClassVar[int] = 7
    _VIDEO_METADATA_POS: ClassVar[int] = 8
    _OPTIONS_POS: ClassVar[int] = 9
    _LAST_MODIFIED_TIMESTAMP_POS: ClassVar[int] = 10
    _INFOGRAPHIC_METADATA_POS: ClassVar[int] = 14
    _TIMESTAMP_POS: ClassVar[int] = 15
    _SLIDE_DECK_METADATA_POS: ClassVar[int] = 16
    _ARTIFACT_USER_STATE_POS: ClassVar[int] = 17
    _DATA_TABLE_PAYLOAD_POS: ClassVar[int] = 18
    _ETAG_POS: ClassVar[int] = 21

    # Per-type location of the user's generation prompt: the top-level block
    # index that holds the artifact's content, followed by the sub-path to the
    # prompt leaf inside it. The type-4 key (QUIZ) covers quizzes, flashcards,
    # and the interactive mind map, which share one options block. Verified live
    # across every studio artifact type; adapted note-backed mind maps (using
    # the genuine backend mind-map code 5) are absent here and have no prompt.
    # ---- Inside the type-4 options block (``data[9]``) ---------------------
    # ``AppArtifact.generationOptions`` and the four leaves this adapter reads
    # from it. The whole family is quantity-then-difficulty: both
    # ``QuizGenerationOptions`` and ``FlashcardsGenerationOptions`` number
    # quantity 1 and difficulty 2, so one pair of constants indexes both (the
    # shared numbering is asserted in test_wire_contract.py).
    _GENERATION_OPTIONS_POS: ClassVar[int] = 1
    _APP_TYPE_POS: ClassVar[int] = 0
    _FLASHCARDS_OPTIONS_POS: ClassVar[int] = 6
    _QUIZ_OPTIONS_POS: ClassVar[int] = 7
    _OPTION_QUANTITY_POS: ClassVar[int] = 0
    _OPTION_DIFFICULTY_POS: ClassVar[int] = 1

    _PROMPT_LOCATION: ClassVar[dict[int, tuple[int, ...]]] = {
        ArtifactTypeCode.AUDIO.value: (_AUDIO_METADATA_POS, 1, 0),
        ArtifactTypeCode.REPORT.value: (_REPORT_MARKDOWN_POS, 1, 5),
        ArtifactTypeCode.VIDEO.value: (_VIDEO_METADATA_POS, 2, 2),
        ArtifactTypeCode.QUIZ.value: (_OPTIONS_POS, _GENERATION_OPTIONS_POS, 2),
        ArtifactTypeCode.INFOGRAPHIC.value: (_INFOGRAPHIC_METADATA_POS, 0, 0),
        ArtifactTypeCode.SLIDE_DECK.value: (_SLIDE_DECK_METADATA_POS, 0, 0),
        ArtifactTypeCode.DATA_TABLE.value: (_DATA_TABLE_PAYLOAD_POS, 1, 0),
    }

    _AUDIO_MEDIA_LIST_POS: ClassVar[int] = 5
    _AUDIO_DURATION_POS: ClassVar[int] = 6
    _VIDEO_MEDIA_LIST_POS: ClassVar[int] = 4
    _VIDEO_DURATION_POS: ClassVar[int] = 5
    _MEDIA_URL_POS: ClassVar[int] = 0
    _MEDIA_KIND_POS: ClassVar[int] = 1
    _MEDIA_MIME_POS: ClassVar[int] = 2
    _VIDEO_PREFERRED_KIND: ClassVar[int] = 4
    _MEDIA_TYPE_MAP: ClassVar[dict[int, ArtifactMediaType]] = {
        1: ArtifactMediaType.PROGRESSIVE,
        2: ArtifactMediaType.HLS,
        3: ArtifactMediaType.DASH,
        4: ArtifactMediaType.DOWNLOAD,
    }
    _REPORT_GENERATION_OPTIONS_POS: ClassVar[int] = 1
    _REPORT_KIND_POS: ClassVar[int] = 0
    _INFOGRAPHIC_ITEMS_POS: ClassVar[int] = 2
    # Historical compatibility fallback: before the exact infographic block
    # was modeled, the URL extractor scanned any top-level list's slot 2.
    _INFOGRAPHIC_CONTENT_POS: ClassVar[int] = 2
    _INFOGRAPHIC_FIRST_CONTENT_POS: ClassVar[int] = 0
    _INFOGRAPHIC_IMAGE_DATA_POS: ClassVar[int] = 1
    _INFOGRAPHIC_TITLE_POS: ClassVar[int] = 0
    _INFOGRAPHIC_IMAGE_POS: ClassVar[int] = 1
    _INFOGRAPHIC_ALT_TEXT_POS: ClassVar[int] = 2
    _INFOGRAPHIC_TEXT_POS: ClassVar[int] = 3
    _SLIDE_ITEMS_POS: ClassVar[int] = 2
    _SLIDE_IMAGE_POS: ClassVar[int] = 0
    _SLIDE_ALT_TEXT_POS: ClassVar[int] = 1
    _SLIDE_TEXT_POS: ClassVar[int] = 2
    _IMAGE_URL_POS: ClassVar[int] = 0
    _IMAGE_WIDTH_POS: ClassVar[int] = 1
    _IMAGE_HEIGHT_POS: ClassVar[int] = 2
    _SLIDE_DECK_PDF_URL_POS: ClassVar[int] = 3
    _SLIDE_DECK_PPTX_URL_POS: ClassVar[int] = 4
    _AUDIO_USER_STATE_POS: ClassVar[int] = 0
    _APP_USER_STATE_POS: ClassVar[int] = 2
    _PLAYBACK_POSITION_POS: ClassVar[int] = 0
    _APP_STATE_POS: ClassVar[int] = 0
    _DURATION_SECONDS_POS: ClassVar[int] = 0
    _DURATION_NANOS_POS: ClassVar[int] = 1
    _MEDIA_ARTIFACT_TYPES: ClassVar[frozenset[int]] = frozenset(
        {
            ArtifactTypeCode.AUDIO.value,
            ArtifactTypeCode.VIDEO.value,
            ArtifactTypeCode.INFOGRAPHIC.value,
            ArtifactTypeCode.SLIDE_DECK.value,
        }
    )

    # ---- Top-level required positions ------------------------------------
    # These use length guards (not ``safe_index``) so short rows continue to
    # receive sensible defaults under the current strict-only drift policy.

    @property
    def id(self) -> str:
        """Artifact identifier — empty string when absent."""
        if len(self._raw) <= self._ID_POS:
            return ""
        return str(self._raw[self._ID_POS])

    @property
    def title(self) -> str:
        """Artifact title — empty string when absent."""
        if len(self._raw) <= self._TITLE_POS:
            return ""
        return str(self._raw[self._TITLE_POS])

    @property
    def raw(self) -> list[Any]:
        """The wrapped raw row, for legacy APIs that still return list payloads."""
        return self._raw

    @property
    def type_code(self) -> int:
        """Type code (see :class:`ArtifactTypeCode`); ``0`` when absent.

        Returned as the raw ``int``, not the enum, because consumers
        compare against either enum members or raw ints depending on
        context.
        """
        if len(self._raw) <= self._TYPE_POS:
            return 0
        value = self._raw[self._TYPE_POS]
        return value if isinstance(value, int) else 0

    @property
    def status(self) -> int:
        """Processing status code (see :class:`ArtifactStatus`); ``0`` when absent.

        Caveat: since #2127 modeled ``ArtifactStatus.UNKNOWN = 0``, this
        synthetic ``0`` is indistinguishable from the backend genuinely
        reporting ``ARTIFACT_STATUS_UNKNOWN``. The conflation is invisible in
        ``status_str`` (both render ``"unknown"``), but not at the enum: a
        caller doing ``ArtifactStatus(artifact.status)`` on a truncated or
        malformed row used to get a ``ValueError`` — a signal — and now gets
        ``ArtifactStatus.UNKNOWN``, and ``artifact.status ==
        ArtifactStatus.UNKNOWN`` is now ``True`` for such a row. Narrowing this
        to ``int | None`` (so ``_status_from_code``'s existing ``none_status``
        parameter decides, as the CREATE_ARTIFACT path already does) is the
        clean fix and is deliberately left out of the #2127 wire-decode
        correction.
        """
        if len(self._raw) <= self._STATUS_POS:
            return 0
        value = self._raw[self._STATUS_POS]
        return value if isinstance(value, int) else 0

    @property
    def source_ids(self) -> tuple[str, ...]:
        """Source IDs referenced by the artifact, in backend order."""
        sources = self._list_at_top_level(self._SOURCES_POS)
        if sources is None:
            return ()
        result: list[str] = []
        for source in sources:
            if not isinstance(source, list) or not source:
                continue
            source_id = source[0]
            if isinstance(source_id, list) and source_id:
                source_id = source_id[0]
            if isinstance(source_id, str):
                result.append(source_id)
        return tuple(result)

    # ---- Nested descents (delegated to safe_index) -----------------------
    # The outer ``len`` guard preserves the "optional trailing positions"
    # contract; the deeper descent goes through ``safe_index`` so strict
    # mode raises on genuine shape drift.

    @property
    def variant(self) -> int | None:
        """Variant code at ``data[9][1][0]`` — distinguishes QUIZ vs FLASHCARDS
        vs the interactive mind map (variant 4) within the type-4 family.

        Returns ``None`` when:

        * position 9 is absent (short row), or
        * descent through ``[1][0]`` returns an actual ``None`` leaf, or
        * the resulting value is not an ``int``.

        Raises :class:`UnknownRPCMethodError` when position 9 is present but
        its inner shape does not match — that is the signal that Google reshaped
        the options block.
        """
        if len(self._raw) <= self._OPTIONS_POS:
            return None
        options_block = self._raw[self._OPTIONS_POS]
        if not isinstance(options_block, list):
            # Preserves legacy soft-degrade for ``data[9] = None`` rows
            # (observed in older cassettes) without invoking ``safe_index``
            # against a non-list root.
            return None
        value = safe_index(
            options_block,
            self._GENERATION_OPTIONS_POS,
            self._APP_TYPE_POS,
            method_id=self.method_id,
            source="ArtifactRow.variant",
        )
        return value if isinstance(value, int) else None

    @property
    def flashcards_options(self) -> QuizOptionPair | None:
        """Stored flashcards ``[quantity, difficulty]`` pair at ``data[9][1][6]``.

        Returns ``None`` for every row that does not carry the flashcards
        options message — a short row, ``data[9]`` absent or ``null``, or a
        non-flashcards artifact (the slot is ``null`` on a quiz or mind-map
        row).

        Raises :class:`UnknownRPCMethodError` when a container that IS present
        no longer has the expected shape, matching :attr:`variant`'s policy: a
        reshaped payload is drift, and reporting it as "no options" would hide
        exactly what this accessor exists to surface.

        See :attr:`quiz_options` for why this is worth reading at all.
        """
        return self._option_pair(self._FLASHCARDS_OPTIONS_POS, "ArtifactRow.flashcards_options")

    @property
    def quiz_options(self) -> QuizOptionPair | None:
        """Stored quiz ``[quantity, difficulty]`` pair at ``data[9][1][7]``.

        This is the backend's own echo of the options the artifact was created
        with, so it is the only client-side read that can check a
        :func:`~notebooklm._web.params.artifacts.build_quiz_artifact_params`
        payload against reality rather than against a fixture (#2195). It is
        deliberately a *decode* of what the server stored, never a
        reconstruction of what we sent.

        Returns ``None`` — and raises on drift — under the same conditions as
        :attr:`flashcards_options`.
        """
        return self._option_pair(self._QUIZ_OPTIONS_POS, "ArtifactRow.quiz_options")

    def _option_pair(self, position: int, source: str) -> QuizOptionPair | None:
        """Decode one ``[quantity, difficulty]`` leaf out of the options block."""
        if len(self._raw) <= self._OPTIONS_POS:
            return None
        options_block = self._raw[self._OPTIONS_POS]
        if not isinstance(options_block, list):
            # Same legacy soft-degrade as ``variant`` for ``data[9] = None``.
            return None
        generation_options = safe_index(
            options_block,
            self._GENERATION_OPTIONS_POS,
            method_id=self.method_id,
            source=source,
        )
        # ``None`` is a genuine absence (a row with no generation options at
        # all); any OTHER non-list here is a message that changed shape, which
        # is drift and must not be reported as "no options" — that would make a
        # reshaped payload indistinguishable from an unset one, in the accessor
        # whose entire job is saying what the server stored.
        if generation_options is None:
            return None
        if not isinstance(generation_options, list):
            raise UnknownRPCMethodError(
                "expected a list at the generation-options position, got "
                f"{type(generation_options).__name__}",
                method_id=self.method_id,
                path=(self._OPTIONS_POS, self._GENERATION_OPTIONS_POS),
                source=source,
                data_at_failure=repr(generation_options)[:200],
            )
        # The two option slots ARE optional trailing positions inside that
        # message (a mind-map row carries neither, and each family's row leaves
        # the sibling's slot null), so a short block or a null slot is absence.
        if len(generation_options) <= position:
            return None
        pair = generation_options[position]
        if pair is None:
            return None
        if not isinstance(pair, list):
            raise UnknownRPCMethodError(
                f"expected a list at the option-pair position, got {type(pair).__name__}",
                method_id=self.method_id,
                path=(self._OPTIONS_POS, self._GENERATION_OPTIONS_POS, position),
                source=source,
                data_at_failure=repr(pair)[:200],
            )
        return QuizOptionPair(
            quantity=self._option_code(pair, self._OPTION_QUANTITY_POS),
            difficulty=self._option_code(pair, self._OPTION_DIFFICULTY_POS),
        )

    @staticmethod
    def _option_code(pair: list[Any], position: int) -> int | None:
        """Read one option code, or ``None`` when the leaf is absent/unusable.

        ``None`` covers two cases the caller cannot tell apart, which is a
        deliberate narrowing of the strictness applied to the *containers*
        above: the field was genuinely unset (proto3 drops default-valued
        fields, so an ``[0, 0]`` pair arrives as ``[]``), or the leaf is not an
        integer code. The container shapes are the ones that signal a reshaped
        payload; a single odd scalar is not worth raising from a read-back
        accessor, so it degrades and the pair simply reports the field as unset.

        ``bool`` is excluded explicitly: ``isinstance(True, int)`` is ``True``
        in Python, and this row genuinely carries a bool two slots further
        along, so an unguarded read would happily decode ``difficulty=True``.
        """
        if len(pair) <= position:
            return None
        value = pair[position]
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    @property
    def created_at_raw(self) -> int | float | None:
        """Raw creation timestamp (seconds since epoch) at ``data[15][0]``.

        Exposed separately from :attr:`created_at` because callers that
        sort artifact rows by recency need a value that compares cleanly
        even when the timestamp is missing or ``None``. The
        :meth:`~notebooklm._web.artifact.listing.ArtifactListingService.select_artifact`
        sort key uses ``row.created_at_raw or 0`` to coerce missing
        values to ``0`` without crashing the comparison.

        Returns ``None`` when:

        * position 15 is absent (short row), or
        * descent through ``[0]`` returns an actual ``None`` leaf, or
        * the resulting value is not numeric.
        """
        if len(self._raw) <= self._TIMESTAMP_POS:
            return None
        timestamp_block = self._raw[self._TIMESTAMP_POS]
        if not isinstance(timestamp_block, list) or not timestamp_block:
            # Mirrors the legacy
            # ``len(a) > 15 and isinstance(a[15], list) and a[15]``
            # guard. ``not timestamp_block`` short-circuits an empty
            # ``[]`` envelope so we never invoke ``safe_index`` against
            # it — an empty list at this position is an accepted
            # edge-case rather than drift (some cassettes legitimately
            # have ``data[15] = []``).
            return None
        value = safe_index(
            timestamp_block,
            0,
            method_id=self.method_id,
            source="ArtifactRow.created_at_raw",
        )
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    @property
    def created_at(self) -> datetime | None:
        """Creation timestamp as a :class:`~datetime.datetime`, or ``None``.

        Wraps :attr:`created_at_raw` and converts via
        :func:`_datetime_from_timestamp`, which returns ``None`` for
        out-of-range / non-numeric values.
        """
        raw = self.created_at_raw
        if raw is None:
            return None
        return _datetime_from_timestamp(raw)

    @staticmethod
    def _duration_seconds(value: Any) -> float | None:
        """Decode a protobuf ``Duration``/``Timestamp`` seconds-nanos pair."""
        if not isinstance(value, list) or not value:
            return None
        seconds = value[ArtifactRow._DURATION_SECONDS_POS]
        if not isinstance(seconds, (int, float)) or isinstance(seconds, bool):
            return None
        nanos: int | float = 0
        if len(value) > ArtifactRow._DURATION_NANOS_POS:
            raw_nanos = value[ArtifactRow._DURATION_NANOS_POS]
            if isinstance(raw_nanos, (int, float)) and not isinstance(raw_nanos, bool):
                nanos = raw_nanos
        return float(seconds) + float(nanos) / 1_000_000_000

    @property
    def last_modified_at(self) -> datetime | None:
        """Last-modified timestamp at ``data[10]``, or ``None`` when absent."""
        if len(self._raw) <= self._LAST_MODIFIED_TIMESTAMP_POS:
            return None
        seconds = self._duration_seconds(self._raw[self._LAST_MODIFIED_TIMESTAMP_POS])
        return _datetime_from_timestamp(seconds) if seconds is not None else None

    @property
    def etag(self) -> str | None:
        """Artifact etag at ``data[21]``, when the listing includes it."""
        if len(self._raw) <= self._ETAG_POS:
            return None
        value = self._raw[self._ETAG_POS]
        return value if isinstance(value, str) else None

    # ---- Downloadable / content payload accessors ----------------------------

    @staticmethod
    def _is_valid_artifact_url(value: Any) -> bool:
        """Return True when ``value`` looks like a downloadable artifact URL."""
        return isinstance(value, str) and value.startswith(("http://", "https://"))

    def _list_at_top_level(self, position: int) -> list[Any] | None:
        """Return a top-level list envelope when present.

        Missing trailing positions and non-list envelopes are treated as
        absent for compatibility with the historical permissive extractors.
        Once a list envelope is present, deeper required leaves use
        ``safe_index`` so strict mode can surface genuine nested drift.
        """
        if len(self._raw) <= position:
            return None
        value = self._raw[position]
        if not isinstance(value, list):
            return None
        return value

    @property
    def audio_url(self) -> str | None:
        """Audio Overview media URL, preferring the ``audio/mp4`` entry."""
        audio_block = self._list_at_top_level(self._AUDIO_METADATA_POS)
        if audio_block is None:
            return None

        if len(audio_block) <= self._AUDIO_MEDIA_LIST_POS:
            return None
        media_list = safe_index(
            audio_block,
            self._AUDIO_MEDIA_LIST_POS,
            method_id=self.method_id,
            source="ArtifactRow.audio_url",
        )
        if not isinstance(media_list, list):
            return None

        fallback_url = None
        for item in media_list:
            if not isinstance(item, list):
                continue
            if item and fallback_url is None and self._is_valid_artifact_url(item[0]):
                fallback_url = item[0]
            if (
                len(item) > self._MEDIA_MIME_POS
                and item[self._MEDIA_MIME_POS] == "audio/mp4"
                and item
                and self._is_valid_artifact_url(item[self._MEDIA_URL_POS])
            ):
                return item[self._MEDIA_URL_POS]
        return fallback_url

    def _media_entries(self, media_list: Any) -> tuple[ArtifactMedia, ...]:
        """Decode every URL-bearing entry in one media-list message."""
        if not isinstance(media_list, list):
            return ()
        entries: list[ArtifactMedia] = []
        for item in media_list:
            if not isinstance(item, list) or not item:
                continue
            url = item[self._MEDIA_URL_POS]
            if not self._is_valid_artifact_url(url):
                continue
            type_code: int | None = None
            if len(item) > self._MEDIA_KIND_POS:
                raw_type = item[self._MEDIA_KIND_POS]
                if isinstance(raw_type, int) and not isinstance(raw_type, bool):
                    type_code = raw_type
            mime_type = None
            if len(item) > self._MEDIA_MIME_POS and isinstance(item[self._MEDIA_MIME_POS], str):
                mime_type = item[self._MEDIA_MIME_POS]
            entries.append(
                ArtifactMedia(
                    url=url,
                    kind=(
                        self._MEDIA_TYPE_MAP.get(type_code, ArtifactMediaType.UNKNOWN)
                        if type_code is not None
                        else ArtifactMediaType.UNKNOWN
                    ),
                    type_code=type_code,
                    mime_type=mime_type,
                )
            )
        return tuple(entries)

    @property
    def media_urls(self) -> tuple[ArtifactMedia, ...]:
        """All streaming/download URLs for an audio or video artifact."""
        if self.type_code == ArtifactTypeCode.AUDIO.value:
            block = self._list_at_top_level(self._AUDIO_METADATA_POS)
            if block is None or len(block) <= self._AUDIO_MEDIA_LIST_POS:
                return ()
            return self._media_entries(block[self._AUDIO_MEDIA_LIST_POS])
        if self.type_code == ArtifactTypeCode.VIDEO.value:
            block = self._list_at_top_level(self._VIDEO_METADATA_POS)
            if block is None or len(block) <= self._VIDEO_MEDIA_LIST_POS:
                return ()
            return self._media_entries(block[self._VIDEO_MEDIA_LIST_POS])
        return ()

    @property
    def duration_seconds(self) -> float | None:
        """Audio/video duration, including the nanosecond fraction."""
        if self.type_code == ArtifactTypeCode.AUDIO.value:
            block = self._list_at_top_level(self._AUDIO_METADATA_POS)
            position = self._AUDIO_DURATION_POS
        elif self.type_code == ArtifactTypeCode.VIDEO.value:
            block = self._list_at_top_level(self._VIDEO_METADATA_POS)
            position = self._VIDEO_DURATION_POS
        else:
            return None
        if block is None or len(block) <= position:
            return None
        return self._duration_seconds(block[position])

    @property
    def video_url(self) -> str | None:
        """Video Overview media URL, preferring the primary ``video/mp4`` entry."""
        video_variants = self._list_at_top_level(self._VIDEO_METADATA_POS)
        if video_variants is None:
            return None

        fallback_url = None
        for media_list in video_variants:
            if not isinstance(media_list, list):
                continue
            for item in media_list:
                if (
                    not isinstance(item, list)
                    or not item
                    or not self._is_valid_artifact_url(item[self._MEDIA_URL_POS])
                ):
                    continue
                if fallback_url is None:
                    fallback_url = item[self._MEDIA_URL_POS]
                if len(item) > self._MEDIA_MIME_POS and item[self._MEDIA_MIME_POS] == "video/mp4":
                    if (
                        len(item) > self._MEDIA_KIND_POS
                        and item[self._MEDIA_KIND_POS] == self._VIDEO_PREFERRED_KIND
                    ):
                        return item[self._MEDIA_URL_POS]
                    fallback_url = item[self._MEDIA_URL_POS]
        return fallback_url

    @property
    def infographic_url(self) -> str | None:
        """Infographic image URL from the first URL-bearing content block."""
        for item in self.infographics:
            if item.image_url is not None:
                return item.image_url
        # Preserve the permissive private-extractor behavior for historical
        # minimal rows that placed the infographic content block elsewhere.
        for block in self._raw:
            if not isinstance(block, list) or len(block) <= self._INFOGRAPHIC_CONTENT_POS:
                continue
            content = block[self._INFOGRAPHIC_CONTENT_POS]
            if not isinstance(content, list) or not content:
                continue
            first = content[self._INFOGRAPHIC_FIRST_CONTENT_POS]
            if not isinstance(first, list) or len(first) <= self._INFOGRAPHIC_IMAGE_DATA_POS:
                continue
            image = first[self._INFOGRAPHIC_IMAGE_DATA_POS]
            if isinstance(image, list) and image and self._is_valid_artifact_url(image[0]):
                return image[0]
        return None

    @classmethod
    def _image_fields(cls, value: Any) -> tuple[str | None, int | None, int | None]:
        if not isinstance(value, list):
            return None, None, None
        url = value[cls._IMAGE_URL_POS] if value else None
        width = value[cls._IMAGE_WIDTH_POS] if len(value) > cls._IMAGE_WIDTH_POS else None
        height = value[cls._IMAGE_HEIGHT_POS] if len(value) > cls._IMAGE_HEIGHT_POS else None
        return (
            url if cls._is_valid_artifact_url(url) else None,
            width if isinstance(width, int) and not isinstance(width, bool) else None,
            height if isinstance(height, int) and not isinstance(height, bool) else None,
        )

    @property
    def infographics(self) -> tuple[ArtifactInfographic, ...]:
        """Rendered infographic images plus their alt and full text."""
        block = self._list_at_top_level(self._INFOGRAPHIC_METADATA_POS)
        if block is None or len(block) <= self._INFOGRAPHIC_ITEMS_POS:
            return ()
        items = block[self._INFOGRAPHIC_ITEMS_POS]
        if not isinstance(items, list):
            return ()
        result: list[ArtifactInfographic] = []
        for item in items:
            if not isinstance(item, list):
                continue
            title = item[self._INFOGRAPHIC_TITLE_POS] if item else None
            image = item[self._INFOGRAPHIC_IMAGE_POS] if len(item) > 1 else None
            image_url, width, height = self._image_fields(image)
            alt_text = item[self._INFOGRAPHIC_ALT_TEXT_POS] if len(item) > 2 else None
            text = item[self._INFOGRAPHIC_TEXT_POS] if len(item) > 3 else None
            result.append(
                ArtifactInfographic(
                    title=title if isinstance(title, str) else None,
                    image_url=image_url,
                    width=width,
                    height=height,
                    alt_text=alt_text if isinstance(alt_text, str) else None,
                    text=text if isinstance(text, str) else None,
                )
            )
        return tuple(result)

    @property
    def slides(self) -> tuple[ArtifactSlide, ...]:
        """Rendered slide images plus their alt and full text."""
        block = self._list_at_top_level(self._SLIDE_DECK_METADATA_POS)
        if block is None or len(block) <= self._SLIDE_ITEMS_POS:
            return ()
        items = block[self._SLIDE_ITEMS_POS]
        if not isinstance(items, list):
            return ()
        result: list[ArtifactSlide] = []
        for item in items:
            if not isinstance(item, list):
                continue
            image = item[self._SLIDE_IMAGE_POS] if item else None
            image_url, width, height = self._image_fields(image)
            alt_text = item[self._SLIDE_ALT_TEXT_POS] if len(item) > 1 else None
            text = item[self._SLIDE_TEXT_POS] if len(item) > 2 else None
            result.append(
                ArtifactSlide(
                    image_url=image_url,
                    width=width,
                    height=height,
                    alt_text=alt_text if isinstance(alt_text, str) else None,
                    text=text if isinstance(text, str) else None,
                )
            )
        return tuple(result)

    @property
    def slide_deck_pdf_url(self) -> str | None:
        """Slide deck PDF URL."""
        metadata = self._list_at_top_level(self._SLIDE_DECK_METADATA_POS)
        if metadata is None:
            return None
        url = safe_index(
            metadata,
            self._SLIDE_DECK_PDF_URL_POS,
            method_id=self.method_id,
            source="ArtifactRow.slide_deck_pdf_url",
        )
        return url if self._is_valid_artifact_url(url) else None

    @property
    def slide_deck_pptx_url(self) -> str | None:
        """Slide deck PPTX URL."""
        metadata = self._list_at_top_level(self._SLIDE_DECK_METADATA_POS)
        if metadata is None:
            return None
        if len(metadata) <= self._SLIDE_DECK_PPTX_URL_POS:
            return None
        url = safe_index(
            metadata,
            self._SLIDE_DECK_PPTX_URL_POS,
            method_id=self.method_id,
            source="ArtifactRow.slide_deck_pptx_url",
        )
        return url if self._is_valid_artifact_url(url) else None

    @property
    def report_markdown(self) -> str | None:
        """Report markdown, accepting the direct-string and one-element wrapper shapes."""
        if len(self._raw) <= self._REPORT_MARKDOWN_POS:
            return None
        content_wrapper = self._raw[self._REPORT_MARKDOWN_POS]
        if isinstance(content_wrapper, str):
            return content_wrapper
        if isinstance(content_wrapper, list):
            markdown = safe_index(
                content_wrapper,
                0,
                method_id=self.method_id,
                source="ArtifactRow.report_markdown",
            )
            return markdown if isinstance(markdown, str) else None
        return None

    @property
    def report_kind(self) -> str | None:
        """Backend report-kind label at ``data[7][1][0]``."""
        block = self._list_at_top_level(self._REPORT_MARKDOWN_POS)
        if block is None or len(block) <= self._REPORT_GENERATION_OPTIONS_POS:
            return None
        options = block[self._REPORT_GENERATION_OPTIONS_POS]
        if not isinstance(options, list) or len(options) <= self._REPORT_KIND_POS:
            return None
        value = options[self._REPORT_KIND_POS]
        return value if isinstance(value, str) else None

    @staticmethod
    def _int_tuple(value: Any) -> tuple[int, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(item for item in value if isinstance(item, int) and not isinstance(item, bool))

    @property
    def user_state(self) -> ArtifactUserState | None:
        """Per-user playback/study state at ``data[17]``.

        Known audio and flashcard shapes decode to tagged public dataclasses.
        Any other populated shape is retained as
        :class:`UnknownArtifactUserState` for forward compatibility.
        """
        if len(self._raw) <= self._ARTIFACT_USER_STATE_POS:
            return None
        raw = self._raw[self._ARTIFACT_USER_STATE_POS]
        if raw is None:
            return None
        if not isinstance(raw, list):
            return UnknownArtifactUserState(raw=raw)

        if self.type_code == ArtifactTypeCode.AUDIO.value and len(raw) > self._AUDIO_USER_STATE_POS:
            audio_state = raw[self._AUDIO_USER_STATE_POS]
            if isinstance(audio_state, list) and len(audio_state) > self._PLAYBACK_POSITION_POS:
                position = self._duration_seconds(audio_state[self._PLAYBACK_POSITION_POS])
                if position is not None:
                    return AudioArtifactUserState(playback_position_seconds=position)

        if (
            self.type_code == ArtifactTypeCode.QUIZ.value
            and self.variant == FLASHCARDS_VARIANT
            and len(raw) > self._APP_USER_STATE_POS
        ):
            app_state = raw[self._APP_USER_STATE_POS]
            if isinstance(app_state, list) and len(app_state) > self._APP_STATE_POS:
                state = app_state[self._APP_STATE_POS]
                if isinstance(state, dict):
                    acquisitions = state.get("cardAcquisitionsMapping")
                    known_keys = {
                        "cardAcquisitionsMapping",
                        "currentCardIndex",
                        "hiddenCardIndices",
                        "lastShownOrder",
                        "currentView",
                    }
                    if known_keys.intersection(state):
                        normalized_acquisitions = (
                            {
                                str(key): value
                                for key, value in acquisitions.items()
                                if isinstance(value, str)
                            }
                            if isinstance(acquisitions, dict)
                            else {}
                        )
                        current_index = state.get("currentCardIndex")
                        return FlashcardArtifactUserState(
                            card_acquisitions=normalized_acquisitions,
                            current_card_index=(
                                current_index
                                if isinstance(current_index, int)
                                and not isinstance(current_index, bool)
                                else None
                            ),
                            hidden_card_indices=self._int_tuple(state.get("hiddenCardIndices")),
                            last_shown_order=self._int_tuple(state.get("lastShownOrder")),
                            current_view=(
                                state.get("currentView")
                                if isinstance(state.get("currentView"), str)
                                else None
                            ),
                        )

        return UnknownArtifactUserState(raw=raw)

    @property
    def data_table_raw_payload(self) -> Any:
        """Raw rich-text payload for a data table artifact."""
        if len(self._raw) <= self._DATA_TABLE_PAYLOAD_POS:
            return None
        return self._raw[self._DATA_TABLE_PAYLOAD_POS]

    @property
    def generation_prompt(self) -> str | None:
        """The free-text prompt that generated this artifact, or ``None``.

        Every studio artifact stores the prompt it was generated from at a
        type-specific position inside its content block (see
        :data:`_PROMPT_LOCATION`). This returns that prompt verbatim.

        Returns ``None`` when:

        * the type has no known prompt location (e.g. adapted note-backed mind
          maps using type 5, or an unrecognised type code), or
        * the content block is absent (a short or minimal row), or
        * the prompt leaf is present but not a string.

        Raises :class:`UnknownRPCMethodError` in the same circumstance as the
        other nested accessors: the content block is present but its inner
        shape no longer matches the recorded path — the signal that Google
        reshaped the artifact payload.
        """
        location = self._PROMPT_LOCATION.get(self.type_code)
        if location is None:
            return None
        top_pos, *sub_path = location
        block = self._list_at_top_level(top_pos)
        if block is None:
            return None
        value = safe_index(
            block,
            *sub_path,
            method_id=self.method_id,
            source="ArtifactRow.generation_prompt",
        )
        return value if isinstance(value, str) else None

    def artifact_url(
        self,
        artifact_type: int | None = None,
        *,
        suppress_drift: bool = False,
    ) -> str | None:
        """Download URL for ``artifact_type`` using the known artifact URL shapes."""
        type_code = self.type_code if artifact_type is None else artifact_type
        try:
            if type_code == ArtifactTypeCode.AUDIO.value:
                return self.audio_url
            if type_code == ArtifactTypeCode.VIDEO.value:
                return self.video_url
            if type_code == ArtifactTypeCode.INFOGRAPHIC.value:
                return self.infographic_url
            if type_code == ArtifactTypeCode.SLIDE_DECK.value:
                return self.slide_deck_pdf_url
            return None
        except UnknownRPCMethodError:
            if suppress_drift:
                return None
            raise

    def is_media_ready(self, artifact_type: int | None = None) -> bool:
        """Return whether media URLs are populated enough to report completion."""
        type_code = self.type_code if artifact_type is None else artifact_type
        if type_code not in self._MEDIA_ARTIFACT_TYPES:
            return True
        return self.artifact_url(type_code, suppress_drift=True) is not None

    # ---- Type-matching helper --------------------------------------------

    def matches_type(self, type_code: int, *, completed_only: bool = False) -> bool:
        """Return whether this row matches ``type_code``.

        Args:
            type_code: Raw :class:`ArtifactTypeCode` integer (or any int)
                to compare against the row's :attr:`type_code`.
            completed_only: When ``True``, also require :attr:`status`
                to equal :data:`ArtifactStatus.COMPLETED` (``3``). This
                is the predicate used by
                :meth:`~notebooklm._web.artifact.listing.ArtifactListingService.select_artifact`
                to pick downloadable artifacts.

        Note:
            This is a *raw* type-code match. The QUIZ vs FLASHCARDS vs
            interactive mind map (variant 4) distinction lives one layer up in
            ``_web.artifact.listing._matches_artifact_type`` because it
            operates on :class:`Artifact` objects (which know variant
            mapping), not raw rows. Keep that separation intentional —
            the adapter exposes the variant via :attr:`variant` if
            callers need it.
        """
        if self.type_code != type_code:
            return False
        if completed_only:
            return self.status == ArtifactStatus.COMPLETED
        return True


@dataclass(frozen=True)
class ReportSuggestionRow:
    """Typed view of one raw ``GET_SUGGESTED_REPORTS`` suggestion row.

    The wrapped row is a single AI-suggested report-format entry returned by
    the ``ciyUvf`` (``GET_SUGGESTED_REPORTS``) RPC. Position layout:

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      title (str)
    1      description (str)
    4      prompt (str)
    5      audience level (int; defaults to ``2`` when absent)
    =====  ============================================================

    Position knowledge is centralised here so ``ArtifactsAPI.suggest_reports``
    stops open-coding ``item[0]`` / ``item[1]`` / ``item[4]`` / ``item[5]``
    (issue #1491). Short / malformed rows degrade to the documented defaults
    rather than raising — a suggestion list is best-effort UI sugar, not a
    load-bearing decode, so the historical permissive contract is preserved.
    """

    _raw: list[Any] = field(repr=False)

    _TITLE_POS: ClassVar[int] = 0
    _DESCRIPTION_POS: ClassVar[int] = 1
    _PROMPT_POS: ClassVar[int] = 4
    _AUDIENCE_LEVEL_POS: ClassVar[int] = 5
    _DEFAULT_AUDIENCE_LEVEL: ClassVar[int] = 2
    # A row must carry at least the prompt slot (index 4) to be usable.
    _MIN_LEN: ClassVar[int] = 5

    @property
    def is_well_formed(self) -> bool:
        """Whether the row is a list long enough to carry title…prompt."""
        return isinstance(self._raw, list) and len(self._raw) >= self._MIN_LEN

    def _str_at(self, position: int) -> str:
        """Return ``self._raw[position]`` when it is a str, else ``""``.

        Bounds-guarded so a short / malformed row degrades to ``""`` (the
        documented contract) instead of raising ``IndexError`` when a property
        is read without first checking :attr:`is_well_formed`.
        """
        if not isinstance(self._raw, list) or len(self._raw) <= position:
            return ""
        value = self._raw[position]
        return value if isinstance(value, str) else ""

    @property
    def title(self) -> str:
        """Suggestion title — empty string when absent / non-string."""
        return self._str_at(self._TITLE_POS)

    @property
    def description(self) -> str:
        """Suggestion description — empty string when absent / non-string."""
        return self._str_at(self._DESCRIPTION_POS)

    @property
    def prompt(self) -> str:
        """Suggestion prompt — empty string when absent / non-string."""
        return self._str_at(self._PROMPT_POS)

    @property
    def audience_level(self) -> int:
        """Audience level at ``[5]``; the default ``2`` when the slot is absent.

        Matches the historical contract: only the *presence* of the slot is
        checked (``len(row) > 5``); a present-but-non-int value is returned
        verbatim, exactly as the prior inline ``item[5] if len(item) > 5 else 2``
        read did.
        """
        if len(self._raw) <= self._AUDIENCE_LEVEL_POS:
            return self._DEFAULT_AUDIENCE_LEVEL
        return self._raw[self._AUDIENCE_LEVEL_POS]
