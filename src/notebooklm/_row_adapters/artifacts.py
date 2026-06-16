"""Artifact row adapter for raw ``LIST_ARTIFACTS`` response rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, ClassVar

from .._types.common import _datetime_from_timestamp
from ..exceptions import UnknownRPCMethodError
from ..rpc import ArtifactStatus, ArtifactTypeCode, RPCMethod, safe_index

__all__ = [
    "MIND_MAP_LEAF_ABSENT",
    "ArtifactRow",
    "ReportSuggestionRow",
    "unwrap_artifact_rows",
    "unwrap_mind_map_generation_leaf",
]


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
    3      failed-artifact plain error text (when present)
    4      processing status (int — see :class:`notebooklm.rpc.ArtifactStatus`)
    5      failed-artifact nested error payload (when present)
    6      audio metadata; ``[6][5]`` is the audio media list
    7      report markdown payload (string or one-element wrapper)
    8      video metadata; nested media variants
    9      options block; ``[9][1][0]`` is the variant code (used to
           distinguish among QUIZ, FLASHCARDS, and the interactive mind map
           (variant 4) when type == 4); ``[9][1][2]`` is the generation prompt
    14     infographic metadata; ``[14][0][0]`` is the generation prompt
    15     timestamp block; ``[15][0]`` is the creation timestamp
           (seconds since epoch)
    16     slide deck metadata; ``[16][3]`` is PDF URL, ``[16][4]`` is PPTX
           URL, and ``[16][0][0]`` is the generation prompt
    18     data table raw rich-text payload; ``[18][1][0]`` is the
           generation prompt
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
    _ERROR_TEXT_POS: ClassVar[int] = 3
    _STATUS_POS: ClassVar[int] = 4
    _ERROR_PAYLOAD_POS: ClassVar[int] = 5
    _AUDIO_METADATA_POS: ClassVar[int] = 6
    _REPORT_MARKDOWN_POS: ClassVar[int] = 7
    _VIDEO_METADATA_POS: ClassVar[int] = 8
    _OPTIONS_POS: ClassVar[int] = 9
    _INFOGRAPHIC_METADATA_POS: ClassVar[int] = 14
    _TIMESTAMP_POS: ClassVar[int] = 15
    _SLIDE_DECK_METADATA_POS: ClassVar[int] = 16
    _DATA_TABLE_PAYLOAD_POS: ClassVar[int] = 18

    # Per-type location of the user's generation prompt: the top-level block
    # index that holds the artifact's content, followed by the sub-path to the
    # prompt leaf inside it. The type-4 key (QUIZ) covers quizzes, flashcards,
    # and the interactive mind map, which share one options block. Verified live
    # across every studio artifact type; note-backed mind maps (synthetic type
    # 5) are absent here and therefore have no prompt.
    _PROMPT_LOCATION: ClassVar[dict[int, tuple[int, ...]]] = {
        ArtifactTypeCode.AUDIO.value: (_AUDIO_METADATA_POS, 1, 0),
        ArtifactTypeCode.REPORT.value: (_REPORT_MARKDOWN_POS, 1, 5),
        ArtifactTypeCode.VIDEO.value: (_VIDEO_METADATA_POS, 2, 2),
        ArtifactTypeCode.QUIZ.value: (_OPTIONS_POS, 1, 2),
        ArtifactTypeCode.INFOGRAPHIC.value: (_INFOGRAPHIC_METADATA_POS, 0, 0),
        ArtifactTypeCode.SLIDE_DECK.value: (_SLIDE_DECK_METADATA_POS, 0, 0),
        ArtifactTypeCode.DATA_TABLE.value: (_DATA_TABLE_PAYLOAD_POS, 1, 0),
    }

    _AUDIO_MEDIA_LIST_POS: ClassVar[int] = 5
    _MEDIA_URL_POS: ClassVar[int] = 0
    _MEDIA_KIND_POS: ClassVar[int] = 1
    _MEDIA_MIME_POS: ClassVar[int] = 2
    _VIDEO_PREFERRED_KIND: ClassVar[int] = 4
    _INFOGRAPHIC_CONTENT_POS: ClassVar[int] = 2
    _INFOGRAPHIC_FIRST_CONTENT_POS: ClassVar[int] = 0
    _INFOGRAPHIC_IMAGE_DATA_POS: ClassVar[int] = 1
    _SLIDE_DECK_PDF_URL_POS: ClassVar[int] = 3
    _SLIDE_DECK_PPTX_URL_POS: ClassVar[int] = 4
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
        """Processing status code (see :class:`ArtifactStatus`); ``0`` when absent."""
        if len(self._raw) <= self._STATUS_POS:
            return 0
        value = self._raw[self._STATUS_POS]
        return value if isinstance(value, int) else 0

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
            1,
            0,
            method_id=self.method_id,
            source="ArtifactRow.variant",
        )
        return value if isinstance(value, int) else None

    @property
    def created_at_raw(self) -> int | float | None:
        """Raw creation timestamp (seconds since epoch) at ``data[15][0]``.

        Exposed separately from :attr:`created_at` because callers that
        sort artifact rows by recency need a value that compares cleanly
        even when the timestamp is missing or ``None``. The
        :meth:`~notebooklm._artifact.listing.ArtifactListingService.select_artifact`
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
        for item in self._raw:
            if not isinstance(item, list) or len(item) <= self._INFOGRAPHIC_CONTENT_POS:
                continue
            content = item[self._INFOGRAPHIC_CONTENT_POS]
            if not isinstance(content, list) or not content:
                continue
            first_content = safe_index(
                content,
                self._INFOGRAPHIC_FIRST_CONTENT_POS,
                method_id=self.method_id,
                source="ArtifactRow.infographic_url",
            )
            if (
                not isinstance(first_content, list)
                or len(first_content) <= self._INFOGRAPHIC_IMAGE_DATA_POS
            ):
                continue
            img_data = first_content[self._INFOGRAPHIC_IMAGE_DATA_POS]
            if (
                isinstance(img_data, list)
                and img_data
                and self._is_valid_artifact_url(img_data[self._MEDIA_URL_POS])
            ):
                return img_data[self._MEDIA_URL_POS]
        return None

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

        * the type has no known prompt location (e.g. note-backed mind maps,
          synthetic type 5, or an unrecognised type code), or
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

    @property
    def failed_error_text(self) -> str | None:
        """Human-readable error text from a failed artifact row, when present."""
        if len(self._raw) > self._ERROR_TEXT_POS:
            direct = self._raw[self._ERROR_TEXT_POS]
            if isinstance(direct, str) and direct.strip():
                return direct.strip()

        if len(self._raw) <= self._ERROR_PAYLOAD_POS:
            return None
        nested = self._raw[self._ERROR_PAYLOAD_POS]
        if not isinstance(nested, list):
            return None
        for item in nested:
            if isinstance(item, str) and item.strip():
                return item.strip()
            if isinstance(item, list):
                for sub_item in item:
                    if isinstance(sub_item, str) and sub_item.strip():
                        return sub_item.strip()
        return None

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
                :meth:`~notebooklm._artifact.listing.ArtifactListingService.select_artifact`
                to pick downloadable artifacts.

        Note:
            This is a *raw* type-code match. The QUIZ vs FLASHCARDS vs
            interactive mind map (variant 4) distinction lives one layer up in
            ``_artifact.listing._matches_artifact_type`` because it
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
