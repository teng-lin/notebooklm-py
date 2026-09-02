"""Source row adapters for raw NotebookLM source response rows."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any, ClassVar

from ..._types.common import _datetime_from_timestamp
from ..._types.enums import DriveSourceStatus, SourceStatus
from ...exceptions import DecodingError
from ...rpc import RPCMethod, safe_index

if TYPE_CHECKING:
    from ..._types.sources import ExpertIntelligenceSourceMetadata

logger = logging.getLogger("notebooklm._row_adapters.sources")

#: Unmapped status codes already warned about, so a polled source does not
#: re-emit the same drift line on every decode. Mirrors
#: ``_types/sources.py::_warned_source_types``.
_warned_status_codes: set[int] = set()

#: ``(method_id, repr(value))`` pairs already warned about by
#: :meth:`SourceRow._warn_unmapped_drive_status`, so a polled Drive source does
#: not re-emit the same drift line on every decode. ``repr`` rather than the
#: value itself because a drifted slot can hold an unhashable payload (e.g. a
#: list), and because ``repr`` keeps ``3`` and ``"3"`` distinct. Capped at
#: :data:`_MAX_DRIFT_REPR_LEN` so a pathological payload cannot retain an
#: unbounded string.
_warned_drive_status_codes: set[tuple[str, str]] = set()

#: Cap on the rendered drift value used as a warn-once key and logged. Long
#: enough to identify any plausible scalar or short list verbatim.
_MAX_DRIFT_REPR_LEN: int = 120

#: Backend ``DRIVE_SOURCE_STATUS_UNSPECIFIED``. Not a
#: :class:`~notebooklm.rpc.types.DriveSourceStatus` member on purpose — it means
#: "no claim", which is what ``None`` already means, so
#: :attr:`SourceRow.drive_status` normalizes it rather than giving one state two
#: representations. See ``_wire_contract.py::ENUM_GAPS``.
_DRIVE_STATUS_UNSPECIFIED: int = 0

#: ``(method_id, block_pos)`` pairs already warned about by
#: :meth:`SourceRow._document_id_at` — one line per drifted slot, not per row.
_warned_drive_id_slots: set[tuple[str, int]] = set()

__all__ = [
    "SourceFulltextRow",
    "SourceGuideRow",
    "SourceRow",
    "SourceRowShape",
    "unwrap_add_source_rows",
]


# ---------------------------------------------------------------------------
# SourceRow
# ---------------------------------------------------------------------------


class SourceRowShape(str, Enum):
    """The wire shape that a :class:`SourceRow` was extracted from.

    Source rows arrive over three distinct shapes; the shape is tracked
    on the row only for diagnostics (so drift logs can name the path
    that was taken). All three normalize to the same :class:`SourceRow`
    interface — consumer sites read named properties regardless of
    shape.

    See :meth:`SourceRow.from_unknown_shape` for the dispatcher and
    :class:`SourceRow` for the position contract on the **normalized
    entry** form that the adapter wraps internally.
    """

    #: ``[[[[id], title, metadata, ...]]]`` — deeply-nested response,
    #: e.g. some ``ADD_SOURCE`` shapes where the entry is wrapped in an
    #: extra outer list.
    DEEPLY_NESTED = "deeply_nested"

    #: ``[[[id], title, metadata, ...]]`` — medium-nested, the most
    #: common shape used by ``GET_NOTEBOOK`` and ``ADD_SOURCE``.
    MEDIUM_NESTED = "medium_nested"

    #: ``[id, title, ...]`` — flat shape. Used by some callers that pre-
    #: extracted the entry envelope.
    FLAT = "flat"

    #: A pre-extracted ``[[id], title, metadata, ...]`` entry — what
    #: :meth:`SourceRow.from_entry` wraps directly without dispatching.
    #: Identical layout to ``MEDIUM_NESTED`` after one unwrap; tracked
    #: separately so drift logs can distinguish "dispatcher produced
    #: this" from "caller handed us an already-unwrapped entry".
    ENTRY = "entry"


def _looks_like_source_entry(value: Any) -> bool:
    """Whether ``value`` resembles one already-unwrapped Source row."""
    if not isinstance(value, list) or not value:
        return False
    # An entry's second field is its title. Reject a nested list here so a
    # repeated collection of short rows (``[[id-a], [id-b]]``) is not mistaken
    # for one entry whose id envelope happens to be the first row.
    if len(value) > 1 and value[1] is not None and not isinstance(value[1], str):
        return False
    raw_id = value[0]
    if isinstance(raw_id, str):
        return True
    if not isinstance(raw_id, list) or not raw_id:
        return False
    if isinstance(raw_id[0], str):
        return True
    return (
        len(raw_id) == 3
        and raw_id[0] is None
        and isinstance(raw_id[2], list)
        and bool(raw_id[2])
        and isinstance(raw_id[2][0], str)
    )


def _unwrap_source_entry(value: Any) -> list[Any] | None:
    """Unwrap one flat/medium/deep Source payload to its entry, if recognized."""
    candidate = value
    for _ in range(3):
        if _looks_like_source_entry(candidate):
            return candidate
        if not isinstance(candidate, list) or len(candidate) != 1:
            return None
        candidate = candidate[0]
    return None


def unwrap_add_source_rows(payload: Any) -> list[list[Any]]:
    """Extract repeated Source payloads from known ``AddSources`` envelopes.

    A single result historically decodes as one medium/deep Source payload;
    a multi-entry response may be either a flat repeated-row list or carry one
    additional outer list. Anything else is schema drift: the general Source
    parser intentionally degrades malformed listing rows, which is unsafe for a
    mutating response whose returned ids are the only proof of which writes
    committed. Recognized per-row wrappers are deliberately retained so
    :meth:`SourceRow.from_unknown_shape` can preserve shape-dependent decoding,
    including the legacy deeply-nested ``metadata[0]`` URL fallback.
    """
    if not isinstance(payload, list) or not payload:
        raise DecodingError(
            "Unrecognized ADD_SOURCE response envelope",
            raw_response=repr(payload),
            method_id=RPCMethod.ADD_SOURCE.value,
        )
    single = _unwrap_source_entry(payload)
    if single is not None:
        return [payload]

    repeated = [_unwrap_source_entry(item) for item in payload]
    if all(item is not None for item in repeated):
        return list(payload)

    if len(payload) == 1 and isinstance(payload[0], list) and payload[0]:
        wrapped_repeated = [_unwrap_source_entry(item) for item in payload[0]]
        if all(item is not None for item in wrapped_repeated):
            return list(payload[0])
    raise DecodingError(
        "Unrecognized ADD_SOURCE response envelope",
        raw_response=repr(payload),
        method_id=RPCMethod.ADD_SOURCE.value,
    )


@dataclass(frozen=True)
class SourceRow:
    """Typed view of a single source row.

    Source rows arrive over three wire shapes (see
    :class:`SourceRowShape`); the :meth:`from_unknown_shape` classmethod
    dispatches the three into a single **normalized entry** layout that
    this adapter wraps:

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      source-id envelope. Variants:

           * ``"id"`` — bare string (legacy / flat shape).
           * ``["id"]`` — typical wrapping.
           * ``[None, True, ["id"]]`` — drive-backed entries nest the
             id one level deeper at ``raw_id[2][0]``. Surfaced by
             :attr:`id` transparently.
    1      title (str) — may be ``None`` / missing on short rows.
    2      metadata sub-list (see below).
    3      ``Source.settings`` block (``SourceSettings``). ``[3][1]`` is the
           :class:`~notebooklm.rpc.types.SourceStatus` ingestion code (used by
           ``GET_NOTEBOOK`` source-list rows); ``[3][3]`` is the
           :class:`~notebooklm.rpc.types.DriveSourceStatus` Drive-side code,
           populated on Drive-backed rows only (see :attr:`drive_status`).
    5      Direct download URL for the original uploaded file, when present
           (see :attr:`download_url`).
    6      Drive viewer URL for the uploaded file, when present (see
           :attr:`viewer_url`).
    7      Content blob descriptor; ``[7][2]`` is the source's true content
           MIME type (see :attr:`content_mime`).
    =====  ============================================================

    **Metadata sub-list layout** (``self._raw[2]``):

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      ``SourceMetadata.googleDocsMetadata`` — a Google-native Doc /
           Slides block whose ``[0][0]`` is the ``documentId`` (see
           :attr:`drive_document_id`). Legacy shapes put a bare
           ``http(s)://...`` URL here, honored only under
           ``url_allow_bare_http``.
    1      Source word/size count (see :attr:`word_count`). The meaning is
           inferred from live values because the mobile schema leaves the
           field unnamed.
    2      timestamp block; ``[2][0]`` is the creation timestamp
           (seconds since epoch).
    3      Revision handle ``[revision_id, [seconds, nanos]]`` (see
           :attr:`revision_id` and :attr:`revision_timestamp`).
    4      type code (int — see
           :class:`notebooklm._types.sources.SourceType` mapping in
           ``_types/sources.py``).
    5      youtube/source-specific block; ``[5][0]`` is a YouTube URL.
    7      url block; ``[7][0]`` is the canonical source URL when
           present (takes precedence over ``metadata[5][0]`` and
           ``metadata[0]``).
    9      ``SourceMetadata.googleDriveSourceMetadata`` — the descriptor
           for Drive-hosted files (native Sheets and Drive PDFs alike):
           ``[document_id, kind_int, mime, ""]``; ``[9][0]`` is the
           ``documentId`` (see :attr:`drive_document_id`), ``[9][2]`` the
           MIME.
    14     Last-modified timestamp block ``[seconds, nanos]`` (see
           :attr:`last_modified_at`).
    19     top-level MIME string for Drive-hosted sources. Used with
           ``[9][2]`` to disambiguate the type-code ``14`` overload
           (native Sheet vs Drive-hosted PDF) — see :attr:`mime`.
    =====  ============================================================

    Position knowledge is centralised here. Consumer sites should NEVER
    open-code ``data[0][0]`` / ``data[0][0][0]`` / ``metadata[4]`` —
    wrap the row in a :class:`SourceRow` and read through the typed
    properties instead.

    The dataclass is frozen so accidentally mutating the wrapped row is
    impossible through the adapter; the adapter itself never copies the
    raw row, so it is cheap to construct.
    """

    # Wrapped normalized entry; ``repr=False`` so logs don't explode
    # with the entire batchexecute payload.
    _raw: list[Any] = field(repr=False)
    # ``method_id`` is a public extension point: callers wrapping a row
    # that came from a non-default RPC override it so ``safe_index``
    # drift diagnostics point at the correct method.
    method_id: str = RPCMethod.GET_NOTEBOOK.value
    # Records which dispatcher branch produced this row. Default is
    # ``ENTRY`` because direct construction (``SourceRow(entry)``)
    # bypasses dispatch.
    shape: SourceRowShape = SourceRowShape.ENTRY
    # The deeply-nested ``ADD_SOURCE``-style path historically allowed
    # a bare ``http(s)://...`` value at ``metadata[0]`` to act as the
    # URL when no ``metadata[7]``/``metadata[5]`` entry was present.
    # Medium-nested and entry-shaped rows (``GET_NOTEBOOK`` source list
    # + most ``ADD_SOURCE`` shapes) pack unrelated content into
    # ``metadata[0]`` and must NOT honor it as a URL.
    url_allow_bare_http: bool = False

    # ---- Position constants (the canary contract) ------------------------
    # ClassVar so the frozen dataclass treats them as class-level
    # constants. If any of these change,
    # ``tests/unit/test_row_adapters.py::TestSourceRowPositionContract``
    # MUST be updated in the same commit — that failure is the wire-shape
    # change signal.

    # Top-level (entry) positions.
    _ID_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = 1
    _METADATA_POS: ClassVar[int] = 2
    _STATUS_BLOCK_POS: ClassVar[int] = 3
    _STATUS_INNER_POS: ClassVar[int] = 1
    # ``SourceSettings.userDriveSourceStatus`` (tag 4) inside the same settings
    # block the ingestion status is read from (#2111).
    _DRIVE_STATUS_INNER_POS: ClassVar[int] = 3
    # Source tags 6-8 are unnamed ``addUnused()`` slots in the recovered
    # mobile schema, but are populated together on uploaded-file rows (#2112).
    _DOWNLOAD_URL_POS: ClassVar[int] = 5
    _VIEWER_URL_POS: ClassVar[int] = 6
    _CONTENT_DESCRIPTOR_POS: ClassVar[int] = 7
    _CONTENT_DESCRIPTOR_MIME_POS: ClassVar[int] = 2

    # Metadata sub-list positions. Index 0 is googleDocsMetadata, not a URL
    # slot (the old _META_BARE_URL_POS name meant only the legacy fallback).
    _META_GOOGLE_DOCS_POS: ClassVar[int] = 0
    # These three fields are also unnamed in the recovered mobile schema. Their
    # semantics are inferred from live values and context (#2114).
    _META_WORD_COUNT_POS: ClassVar[int] = 1
    _META_TIMESTAMP_POS: ClassVar[int] = 2
    _META_REVISION_POS: ClassVar[int] = 3
    _META_TYPE_POS: ClassVar[int] = 4
    _META_YOUTUBE_POS: ClassVar[int] = 5
    _META_URL_POS: ClassVar[int] = 7
    # Drive-hosted sources carry the true file MIME here (#1832 live capture):
    # the descriptor ``[document_id, kind_int, mime, ""]`` at [9] and a
    # top-level MIME string at [19]. Used to disambiguate the type_code==14
    # overload (native Sheet vs Drive PDF), which the URL slots can't — Drive
    # rows have no URL (metadata[5]/[7] null; metadata[0] is the Drive block).
    _META_DRIVE_DESCRIPTOR_POS: ClassVar[int] = 9
    _META_LAST_MODIFIED_POS: ClassVar[int] = 14
    # ``SourceMetadata.expertIntelligenceSourceMetadata`` (proto tag 19, index
    # 18) — Play Books provenance on an Expert-Intelligence source (#2292).
    _META_EXPERT_INTELLIGENCE_POS: ClassVar[int] = 18
    _META_MIME_POS: ClassVar[int] = 19
    # Inner positions of the ExpertIntelligenceSourceMetadata sub-array at [18].
    _EI_CONTENT_ID_POS: ClassVar[int] = 0
    _EI_PROVIDER_POS: ClassVar[int] = 1
    _EI_TITLE_POS: ClassVar[int] = 2
    _EI_AUTHORS_POS: ClassVar[int] = 3
    _EI_THUMBNAIL_POS: ClassVar[int] = 4
    _EI_DESCRIPTION_POS: ClassVar[int] = 5
    _EI_FIELD_TYPE_POS: ClassVar[int] = 6
    # Position of the MIME string inside the drive-file descriptor at [9].
    _DRIVE_DESCRIPTOR_MIME_POS: ClassVar[int] = 2
    # Drive ``documentId`` inside either Drive metadata block (#2113):
    # GoogleDocsSourceMetadata and GoogleDriveSourceMetadata both declare it as
    # tag 1, so one constant covers both blocks.
    _DRIVE_DOCUMENT_ID_POS: ClassVar[int] = 0
    _REVISION_ID_POS: ClassVar[int] = 0
    _REVISION_TIMESTAMP_POS: ClassVar[int] = 1

    # Id-envelope inner positions (the three layouts at ``self._raw[0]``).
    _ID_ENVELOPE_PLAIN_POS: ClassVar[int] = 0
    _ID_ENVELOPE_DRIVE_PAYLOAD_POS: ClassVar[int] = 2
    _ID_ENVELOPE_DRIVE_INNER_POS: ClassVar[int] = 0

    # Neutral "first element of a single-item list" index, used by url
    # helpers that pull the leading element from ``metadata[7]``,
    # ``metadata[5]``, etc. Kept separate from ``_ID_ENVELOPE_PLAIN_POS``
    # (also ``0``) so a future id-envelope reshape doesn't accidentally
    # break URL extraction.
    _LIST_FIRST_POS: ClassVar[int] = 0

    # ---- Dispatchers -----------------------------------------------------

    @classmethod
    def from_unknown_shape(
        cls,
        data: list[Any],
        *,
        method_id: str | None = None,
    ) -> SourceRow:
        """Normalize any of the three source wire shapes into a
        :class:`SourceRow`.

        Shapes handled (matching the legacy ``Source.from_api_response``
        branches):

        1. **Deeply nested** — ``[[[[id], title, metadata, ...]]]``.
           Unwraps ``data[0][0]`` to reach the entry. Honors the legacy
           ``url_allow_bare_http=True`` policy (only this shape lets a
           bare ``http(s)://...`` at ``metadata[0]`` act as the URL).
        2. **Medium nested** — ``[[[id], title, metadata, ...]]``.
           Unwraps ``data[0]`` to reach the entry.
        3. **Flat** — ``[id, title, ...]``. Wraps directly with the id at
           ``self._raw[0]``; metadata-dependent properties are absent only when
           no metadata list is present at position 2.

        Args:
            data: Raw decoded payload. Must be a non-empty list.
            method_id: Override for diagnostics; defaults to the class
                default (``GET_NOTEBOOK``) when ``None``.

        Returns:
            A :class:`SourceRow` wrapping the normalized entry.

        Raises:
            ValueError: When ``data`` is empty or not a list.
        """
        if not data or not isinstance(data, list):
            raise ValueError(f"Invalid source data: {data!r}")

        mid = method_id if method_id is not None else RPCMethod.GET_NOTEBOOK.value

        outer = data[cls._ID_POS]
        # The medium/deep dispatch mirrors the legacy
        # ``Source.from_api_response`` two-level guard:
        #   data[0] is a non-empty list, AND data[0][0] is a non-empty list.
        # If data[0][0][0] is *itself* a list, we have an extra wrapper
        # (deeply-nested): the entry lives at data[0][0] and its id
        # envelope at data[0][0][0]. Otherwise the entry lives at
        # data[0] and its id envelope at data[0][0].
        if (
            isinstance(outer, list)
            and outer
            and isinstance(outer[cls._ID_POS], list)
            and outer[cls._ID_POS]
        ):
            inner = outer[cls._ID_POS]
            if isinstance(inner[cls._ID_ENVELOPE_PLAIN_POS], list):
                # Deeply nested: data[0][0] IS the entry; its [0] is
                # itself a list (the id envelope), so we have an extra
                # outer wrapper around the entry.
                return cls(
                    _raw=inner,
                    method_id=mid,
                    shape=SourceRowShape.DEEPLY_NESTED,
                    url_allow_bare_http=True,
                )
            # Medium nested: data[0] IS the entry; data[0][0] is its
            # id envelope.
            return cls(
                _raw=outer,
                method_id=mid,
                shape=SourceRowShape.MEDIUM_NESTED,
                url_allow_bare_http=False,
            )

        # Flat: [id, title, ...]
        return cls(
            _raw=data,
            method_id=mid,
            shape=SourceRowShape.FLAT,
            url_allow_bare_http=False,
        )

    @classmethod
    def from_entry(
        cls,
        entry: list[Any],
        *,
        method_id: str | None = None,
    ) -> SourceRow:
        """Wrap an already-extracted entry (``[[id], title, metadata, ...]``).

        Used by callers that walked the response envelope themselves —
        e.g. :class:`notebooklm._web.sources.listing.SourceLister` iterating
        over ``notebook[0][1]`` and
        :meth:`notebooklm._notebooks.NotebooksAPI.get_source_ids`
        iterating over the same envelope. Shape is recorded as
        :attr:`SourceRowShape.ENTRY`.
        """
        mid = method_id if method_id is not None else RPCMethod.GET_NOTEBOOK.value
        return cls(
            _raw=entry,
            method_id=mid,
            shape=SourceRowShape.ENTRY,
            url_allow_bare_http=False,
        )

    # ---- Top-level required positions ------------------------------------
    # Length guards (not ``safe_index``) so short rows continue to receive
    # sensible defaults under the current strict-only drift policy.

    @property
    def id(self) -> str:
        """Source identifier — empty string when the envelope is malformed.

        Handles three id-envelope variants transparently:

        * Bare string at ``self._raw[0]`` (flat shape).
        * ``["id"]`` at ``self._raw[0]`` (typical).
        * ``[None, True, ["id"]]`` at ``self._raw[0]`` (drive-backed).
        """
        raw_id = self._id_envelope()
        if raw_id is None:
            return ""
        if not isinstance(raw_id, list):
            # Flat shape: id is the entry element directly.
            return str(raw_id)
        # ``[id, ...]`` — typical wrapping.
        if raw_id and raw_id[self._ID_ENVELOPE_PLAIN_POS] is not None:
            return str(raw_id[self._ID_ENVELOPE_PLAIN_POS])
        # ``[None, True, [id]]`` — drive-backed nesting.
        if (
            len(raw_id) > self._ID_ENVELOPE_DRIVE_PAYLOAD_POS
            and isinstance(raw_id[self._ID_ENVELOPE_DRIVE_PAYLOAD_POS], list)
            and raw_id[self._ID_ENVELOPE_DRIVE_PAYLOAD_POS]
        ):
            inner = raw_id[self._ID_ENVELOPE_DRIVE_PAYLOAD_POS][self._ID_ENVELOPE_DRIVE_INNER_POS]
            return str(inner) if inner is not None else ""
        return ""

    def _id_envelope(self) -> Any:
        """Return the raw id envelope (``self._raw[0]``) or ``None``."""
        if len(self._raw) <= self._ID_POS:
            return None
        return self._raw[self._ID_POS]

    @property
    def has_id(self) -> bool:
        """Whether the row resolves to a non-empty :attr:`id`.

        Used by :class:`notebooklm._web.sources.listing.SourceLister` to skip
        rows whose id envelopes legacy ``_extract_source_id`` would
        have rejected (returning ``None``) — including the rare
        ``[None, True, [None]]`` drive-payload-with-``None``-inner case
        that :attr:`id` decodes to ``""``.

        Equivalent to ``bool(self.id)``; exposed as a named predicate
        so consumer call sites read intent-first.
        """
        return bool(self.id)

    def listing_shape_error(self) -> str | None:
        """Return why this row cannot support a strict source-list snapshot.

        The general row adapter intentionally degrades incomplete shapes to
        ``None`` / ``UNKNOWN`` because non-listing RPCs may omit metadata or a
        status block. ``GET_NOTEBOOK`` source-list rows have a stronger shape:
        strict filtering/counting needs both the type and status discriminants
        to be present and integer-valued. Keeping this check on the adapter
        preserves its ownership of all positional wire knowledge.
        """
        metadata = self.metadata
        if metadata is None:
            return "metadata block is missing or malformed"
        if len(metadata) <= self._META_TYPE_POS:
            return "metadata type code is missing"
        type_code = metadata[self._META_TYPE_POS]
        if not isinstance(type_code, int) or isinstance(type_code, bool):
            return "metadata type code is malformed"

        if len(self._raw) <= self._STATUS_BLOCK_POS:
            return "status block is missing"
        status_block = self._raw[self._STATUS_BLOCK_POS]
        if not isinstance(status_block, list):
            return "status block is malformed"
        if len(status_block) <= self._STATUS_INNER_POS:
            return "status code is missing"
        status_code = status_block[self._STATUS_INNER_POS]
        if not isinstance(status_code, int) or isinstance(status_code, bool):
            return "status code is malformed"
        return None

    @property
    def title(self) -> str | None:
        """Source title — ``None`` when absent (preserves legacy contract).

        Unlike :attr:`ArtifactRow.title`, this returns ``None`` rather
        than an empty string because the legacy
        ``Source.from_api_response`` carried ``title: str | None`` and
        downstream consumers (CLI table renderers, etc.) branch on the
        ``None`` case.

        Non-``None`` non-string values are coerced via ``str()`` so the
        ``str | None`` annotation is honored at runtime — aligns with
        :attr:`ArtifactRow.title`'s coercion. ``None`` is preserved as-is
        so the legacy "missing
        title" sentinel still distinguishes from "title is empty string".
        """
        if len(self._raw) <= self._TITLE_POS:
            return None
        value = self._raw[self._TITLE_POS]
        if value is None:
            return None
        return value if isinstance(value, str) else str(value)

    @property
    def metadata(self) -> list[Any] | None:
        """The metadata sub-list at ``self._raw[2]``, or ``None``.

        Returned as ``None`` (not ``[]``) when absent or non-list, so
        callers can distinguish "no metadata block" from "metadata
        block exists but is empty".
        """
        if len(self._raw) <= self._METADATA_POS:
            return None
        value = self._raw[self._METADATA_POS]
        return value if isinstance(value, list) else None

    def _top_level_string(self, position: int) -> str | None:
        """Return a non-empty string from a top-level source slot."""
        if len(self._raw) <= position:
            return None
        value = self._raw[position]
        return value if isinstance(value, str) and value else None

    @property
    def download_url(self) -> str | None:
        """Direct URL for downloading the original uploaded file.

        This is ``Source`` tag 6 (row index 5), populated only for source kinds
        whose original bytes remain downloadable. The mobile schema leaves the
        slot unnamed; the meaning is established by live URLs on the
        ``contribution.usercontent.google.com/download`` endpoint (#2112).
        """
        return self._top_level_string(self._DOWNLOAD_URL_POS)

    @property
    def viewer_url(self) -> str | None:
        """Drive viewer URL for the original uploaded file, when available.

        This is the live-confirmed ``Source`` tag-7 slot (row index 6). It is
        independent of :attr:`url`, which identifies URL/YouTube sources.
        """
        return self._top_level_string(self._VIEWER_URL_POS)

    @property
    def content_mime(self) -> str | None:
        """True content MIME from the source blob descriptor, when present.

        ``Source`` tag 8 carries a blob descriptor whose third element is the
        MIME. Unlike :attr:`mime`, this is not Drive-specific and therefore
        covers uploaded files such as Markdown sources (#2112).
        """
        if len(self._raw) <= self._CONTENT_DESCRIPTOR_POS:
            return None
        descriptor = self._raw[self._CONTENT_DESCRIPTOR_POS]
        if not isinstance(descriptor, list) or len(descriptor) <= self._CONTENT_DESCRIPTOR_MIME_POS:
            return None
        value = descriptor[self._CONTENT_DESCRIPTOR_MIME_POS]
        return value if isinstance(value, str) and value else None

    @property
    def word_count(self) -> int | None:
        """Inferred source word/size count from ``metadata[1]``.

        The field is populated on every sampled source row, but its proto name
        was not recovered. Values and the surrounding per-source word-limit
        contract strongly indicate a word count; callers should treat that
        semantic label as inferred rather than schema-confirmed (#2114).
        """
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_WORD_COUNT_POS:
            return None
        value = metadata[self._META_WORD_COUNT_POS]
        return value if isinstance(value, int) and not isinstance(value, bool) else None

    def _revision_handle(self) -> list[Any] | None:
        """Return the inferred ``[revision_id, timestamp]`` block."""
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_REVISION_POS:
            return None
        value = metadata[self._META_REVISION_POS]
        return value if isinstance(value, list) else None

    @property
    def revision_id(self) -> str | None:
        """Opaque source revision identifier from ``metadata[3][0]``."""
        handle = self._revision_handle()
        if handle is None or len(handle) <= self._REVISION_ID_POS:
            return None
        value = handle[self._REVISION_ID_POS]
        return value if isinstance(value, str) and value else None

    @property
    def revision_timestamp_raw(self) -> int | float | None:
        """Raw revision timestamp seconds from ``metadata[3][1][0]``."""
        handle = self._revision_handle()
        if handle is None or len(handle) <= self._REVISION_TIMESTAMP_POS:
            return None
        return self._timestamp_raw_from_block(
            handle[self._REVISION_TIMESTAMP_POS],
            source="SourceRow.revision_timestamp_raw",
        )

    @property
    def revision_timestamp(self) -> datetime | None:
        """Revision timestamp as a timezone-aware UTC datetime."""
        raw = self.revision_timestamp_raw
        return _datetime_from_timestamp(raw) if raw is not None else None

    @property
    def type_code(self) -> int | None:
        """Type code at ``metadata[4]`` (int) or ``None`` when absent.

        Returned as raw ``int``; callers map via
        :func:`notebooklm._types.sources._safe_source_type` to get the
        :class:`~notebooklm._types.sources.SourceType` enum.
        """
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_TYPE_POS:
            return None
        value = metadata[self._META_TYPE_POS]
        return value if isinstance(value, int) else None

    @property
    def mime(self) -> str | None:
        """Source MIME type for Drive-hosted sources — ``None`` when absent.

        Precedence:

        1. :meth:`_mime_from_top_level` — ``metadata[19]`` (top-level slot).
        2. :meth:`_mime_from_drive_descriptor` — ``metadata[9][2]`` (MIME
           inside the drive-file descriptor).

        Only Drive-hosted sources populate these; native uploads / web pages
        leave them empty. Used to disambiguate the ``type_code == 14`` overload
        (native Google Sheet vs Drive-hosted PDF) at decode time — see
        :func:`notebooklm._types.sources._disambiguate_type_code` (#1832).
        """
        metadata = self.metadata
        if metadata is None:
            return None
        return self._mime_from_top_level(metadata) or self._mime_from_drive_descriptor(metadata)

    def _mime_from_top_level(self, metadata: list[Any]) -> str | None:
        """Extract the MIME from ``metadata[19]`` (top-level slot).

        Returns ``None`` when position 19 is absent or not a non-empty string.
        """
        if len(metadata) <= self._META_MIME_POS:
            return None
        value = metadata[self._META_MIME_POS]
        return value if isinstance(value, str) and value else None

    def _mime_from_drive_descriptor(self, metadata: list[Any]) -> str | None:
        """Extract the MIME from ``metadata[9][2]`` (drive-file descriptor).

        Returns ``None`` unless position 9 is a list long enough to hold the
        descriptor MIME and that slot is a non-empty string.
        """
        if len(metadata) <= self._META_DRIVE_DESCRIPTOR_POS:
            return None
        descriptor = metadata[self._META_DRIVE_DESCRIPTOR_POS]
        if not isinstance(descriptor, list) or len(descriptor) <= self._DRIVE_DESCRIPTOR_MIME_POS:
            return None
        value = descriptor[self._DRIVE_DESCRIPTOR_MIME_POS]
        return value if isinstance(value, str) and value else None

    @property
    def expert_intelligence(self) -> ExpertIntelligenceSourceMetadata | None:
        """Play Books provenance from ``metadata[18]`` — ``None`` when absent.

        Decodes ``SourceMetadata.expertIntelligenceSourceMetadata`` (proto tag
        19), populated only on Expert-Intelligence sources. The sub-array is
        ``[content_id, provider, title, [authors], thumbnail_url, description,
        field_type, context_type]`` (live-captured #2292); the trailing
        ``context_type`` enum string is not surfaced. Returns ``None`` unless
        position 18 holds a non-empty list.
        """
        # Local import avoids a module-load cycle: ``_types.sources`` imports
        # this row adapter lazily, so importing its dataclasses at module scope
        # here would close the loop.
        from ..._types.sources import ExpertIntelligenceSourceMetadata

        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_EXPERT_INTELLIGENCE_POS:
            return None
        block = metadata[self._META_EXPERT_INTELLIGENCE_POS]
        if not isinstance(block, list) or not block:
            return None

        def _str(pos: int) -> str | None:
            if len(block) <= pos:
                return None
            value = block[pos]
            return value if isinstance(value, str) and value else None

        def _num(pos: int) -> float | None:
            if len(block) <= pos:
                return None
            value = block[pos]
            return float(value) if isinstance(value, (int, float)) else None

        authors_raw = block[self._EI_AUTHORS_POS] if len(block) > self._EI_AUTHORS_POS else None
        authors = (
            tuple(a for a in authors_raw if isinstance(a, str))
            if isinstance(authors_raw, list)
            else ()
        )
        provider = _num(self._EI_PROVIDER_POS)
        return ExpertIntelligenceSourceMetadata(
            content_id=_str(self._EI_CONTENT_ID_POS),
            provider=int(provider) if provider is not None else None,
            title=_str(self._EI_TITLE_POS),
            authors=authors,
            thumbnail_image_url=_str(self._EI_THUMBNAIL_POS),
            description=_str(self._EI_DESCRIPTION_POS),
            field_type=_num(self._EI_FIELD_TYPE_POS),
        )

    @property
    def drive_document_id(self) -> str | None:
        """Google Drive file id of a Drive-backed source — ``None`` otherwise.

        Read from whichever Drive block the row carries: ``metadata[0][0]``
        (``googleDocsMetadata.documentId``; corpus-observed on type codes 1/2),
        else ``metadata[9][0]`` (``googleDriveSourceMetadata.documentId``; type
        code 14 — Drive-hosted files, native Sheet and Drive PDF alike per the
        #1832 overload). No corpus row populates both, so the order is an
        arbitrary-but-pinned tie-break, not a precedence claim.

        The only identity-bearing echo of the Drive ``file_id``: the URL slots
        are empty and ``metadata[0]`` holds the Drive block, not a URL, so
        :attr:`url` is ``None`` on every Drive row. ``sources.add_drive`` probes
        on it (#2113); non-Drive rows return ``None`` and never match a file_id.
        """
        metadata = self.metadata
        if metadata is None:
            return None
        return self._document_id_at(metadata, self._META_GOOGLE_DOCS_POS) or self._document_id_at(
            metadata, self._META_DRIVE_DESCRIPTOR_POS
        )

    def _document_id_at(self, metadata: list[Any], block_pos: int) -> str | None:
        """Read a ``documentId`` from the Drive block at ``block_pos``.

        Structural absence degrades silently — a missing / non-list block just
        means "not a Drive row". Semantic drift warns once (like the unmapped-
        status path): a present block IS a Drive row, so a non-string / blank id
        means the slot moved — the #2113 signature, where the probe then reports
        "not committed" forever.
        """
        if len(metadata) <= block_pos:
            return None
        block = metadata[block_pos]
        if not isinstance(block, list) or len(block) <= self._DRIVE_DOCUMENT_ID_POS:
            return None
        value = block[self._DRIVE_DOCUMENT_ID_POS]
        # Test with ``.strip()`` but return the wire value verbatim: a blank or
        # whitespace-only id is unmatchable (``add_drive`` rejects such a
        # ``file_id`` outright), so it is drift, not an id. No emptiness guard
        # below — the length check above already returned for a short block.
        if isinstance(value, str) and value.strip():
            return value
        key = (self.method_id, block_pos)
        if key not in _warned_drive_id_slots:
            _warned_drive_id_slots.add(key)
            logger.warning(
                "Drive metadata block at metadata[%d] carries no usable documentId "
                "(got %s) from RPC %s; sources.add_drive cannot dedupe these rows "
                "— the wire shape may have changed",
                block_pos,
                type(value).__name__,
                self.method_id,
            )
        return None

    @property
    def url(self) -> str | None:
        """Canonical source URL — ``None`` when absent.

        Precedence (matches the legacy ``_extract_source_url`` logic):

        1. :meth:`_url_from_canonical_block` — ``metadata[7][0]`` (typical
           canonical URL slot, present on every modern source).
        2. :meth:`_url_from_youtube_block` — ``metadata[5][0]`` (YouTube-
           style block, only when its first element is a string).
        3. :meth:`_url_from_bare_metadata_zero` — ``metadata[0]`` —
           only honored when :attr:`url_allow_bare_http` is ``True`` AND
           the value starts with ``http``. This restricted fallback
           exists for the deeply-nested ``ADD_SOURCE`` shape.

        Each precedence level is a tiny named helper so the dispatch
        reads at the same level of abstraction: the property body is the
        precedence order, and each
        helper owns one slot's positional knowledge.
        """
        metadata = self.metadata
        if metadata is None:
            return None
        return (
            self._url_from_canonical_block(metadata)
            or self._url_from_youtube_block(metadata)
            or self._url_from_bare_metadata_zero(metadata)
        )

    def _url_from_canonical_block(self, metadata: list[Any]) -> str | None:
        """Extract the URL from ``metadata[7][0]`` (canonical slot).

        Returns ``None`` when position 7 is absent, non-list, empty, or
        when its first element is falsy. Non-string truthy values are
        stringified to honor the legacy
        ``_extract_source_url`` contract where ``url`` is whatever the
        wire stored at this position.
        """
        if len(metadata) <= self._META_URL_POS:
            return None
        url_list = metadata[self._META_URL_POS]
        if not isinstance(url_list, list) or not url_list:
            return None
        first = url_list[self._LIST_FIRST_POS]
        if not first:
            return None
        return first if isinstance(first, str) else str(first)

    def _url_from_youtube_block(self, metadata: list[Any]) -> str | None:
        """Extract the URL from ``metadata[5][0]`` (YouTube-style block).

        Returns ``None`` unless position 5 is a non-empty list whose
        first element is a string. The string requirement preserves
        legacy behavior where non-string YouTube-block elements (e.g.
        the video id at ``[5][1]`` or channel name at ``[5][2]``) are
        not interpreted as URLs.
        """
        if len(metadata) <= self._META_YOUTUBE_POS:
            return None
        yt_block = metadata[self._META_YOUTUBE_POS]
        if (
            isinstance(yt_block, list)
            and yt_block
            and isinstance(yt_block[self._LIST_FIRST_POS], str)
        ):
            return yt_block[self._LIST_FIRST_POS]
        return None

    def _url_from_bare_metadata_zero(self, metadata: list[Any]) -> str | None:
        """Extract the URL from ``metadata[0]`` — restricted fallback.

        Returns ``None`` unless ALL of:

        * :attr:`url_allow_bare_http` is ``True`` (only the deeply-
          nested ``ADD_SOURCE`` shape sets this), AND
        * position 0 exists, is a string, and starts with ``http``.

        The ``http`` prefix guard avoids treating arbitrary
        ``metadata[0]`` strings (e.g. drive ids, mime types) as URLs
        on shapes where this slot packs unrelated content.
        """
        if not self.url_allow_bare_http or len(metadata) <= self._META_GOOGLE_DOCS_POS:
            return None
        candidate = metadata[self._META_GOOGLE_DOCS_POS]
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
        return None

    def _timestamp_raw_from_block(
        self,
        timestamp_block: Any,
        *,
        source: str,
        allow_bool: bool = False,
    ) -> int | float | None:
        """Decode seconds from a ``[seconds, nanos]`` block.

        ``allow_bool`` exists only for the legacy metadata helper, whose
        long-standing public behavior treats booleans as integers.
        """
        if not isinstance(timestamp_block, list) or not timestamp_block:
            return None
        value = safe_index(
            timestamp_block,
            0,
            method_id=self.method_id,
            source=source,
        )
        return (
            value
            if isinstance(value, (int, float)) and (allow_bool or not isinstance(value, bool))
            else None
        )

    @property
    def created_at_raw(self) -> int | float | None:
        """Raw creation timestamp (seconds since epoch) at ``metadata[2][0]``.

        Returns ``None`` when:

        * metadata is absent / non-list, or
        * ``metadata[2]`` is absent / non-list / empty, or
        * the resulting value is not numeric.

        An empty ``metadata[2] = []`` envelope is treated as a soft
        edge-case (not strict-mode drift), mirroring
        :attr:`ArtifactRow.created_at_raw`.
        """
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_TIMESTAMP_POS:
            return None
        return self._timestamp_raw_from_block(
            metadata[self._META_TIMESTAMP_POS],
            source="SourceRow.created_at_raw",
        )

    @property
    def created_at(self) -> datetime | None:
        """Creation timestamp as a :class:`~datetime.datetime`, or ``None``."""
        raw = self.created_at_raw
        if raw is None:
            return None
        return _datetime_from_timestamp(raw)

    @property
    def last_modified_at_raw(self) -> int | float | None:
        """Raw last-modified timestamp seconds from ``metadata[14][0]``."""
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._META_LAST_MODIFIED_POS:
            return None
        return self._timestamp_raw_from_block(
            metadata[self._META_LAST_MODIFIED_POS],
            source="SourceRow.last_modified_at_raw",
        )

    @property
    def last_modified_at(self) -> datetime | None:
        """Last-modified timestamp as a timezone-aware UTC datetime."""
        raw = self.last_modified_at_raw
        return _datetime_from_timestamp(raw) if raw is not None else None

    # ---- Metadata-only entry points (legacy ``_types.sources`` helpers) --
    # ``_types/sources._extract_source_url`` / ``_extract_source_created_at``
    # receive a **bare metadata sub-list** (``src[2]``) directly rather than a
    # whole row. They are re-exported public surface
    # (``notebooklm.types._extract_source_url`` /
    # ``…_extract_source_created_at``) with a soft return-``None`` contract, so
    # they cannot move their position knowledge into the strict row properties
    # above without a behavior change. These entry points centralise that
    # position knowledge here instead while preserving the EXACT legacy
    # semantics (verified field-by-field against the originals).

    @classmethod
    def _from_metadata(cls, metadata: Any) -> SourceRow:
        """Wrap a bare metadata sub-list as a row whose ``metadata`` is it.

        Used only by :meth:`created_at_from_metadata`. ``_raw[0]`` / ``_raw[1]``
        are placeholders the timestamp path never reads.
        """
        return cls(_raw=[None, None, metadata])

    @classmethod
    def created_at_from_metadata(cls, metadata: Any) -> datetime | None:
        """Creation timestamp from a bare ``src[2]`` metadata list.

        Centralises the ``metadata[2][0]`` timestamp position for the legacy
        ``_types.sources._extract_source_created_at`` helper. Behavior is
        identical to that helper (verified exhaustively): a non-list metadata,
        an absent / non-list / empty ``metadata[2]``, or a non-numeric inner
        value all yield ``None`` (the latter via ``_datetime_from_timestamp``,
        which both paths funnel through).
        """
        if not isinstance(metadata, list):
            return None
        if len(metadata) <= cls._META_TIMESTAMP_POS:
            return None
        row = cls._from_metadata(metadata)
        raw = row._timestamp_raw_from_block(
            metadata[cls._META_TIMESTAMP_POS],
            source="SourceRow.created_at_raw",
            # The legacy public helper treated bool as an int. Preserve that
            # compatibility here while real SourceRow properties reject it.
            allow_bool=True,
        )
        return _datetime_from_timestamp(raw) if raw is not None else None

    @classmethod
    def url_from_metadata(cls, metadata: Any, *, allow_bare_http: bool = True) -> str | None:
        """URL from a bare ``src[2]`` metadata list (legacy soft contract).

        Centralises the ``metadata[7][0]`` > ``metadata[5][0]`` >
        ``metadata[0]`` precedence for the legacy
        ``_types.sources._extract_source_url`` helper. This is DELIBERATELY a
        separate path from the strict :attr:`url` property: the legacy helper
        has a softer, looser contract that :attr:`url` intentionally tightened,
        and this method must reproduce the legacy behavior BYTE-FOR-BYTE
        because it backs re-exported public surface
        (``notebooklm.types._extract_source_url``). Specifically, unlike
        :attr:`url` it:

        * returns the RAW ``metadata[7][0]`` value with NO ``str()`` coercion
          and NO truthiness guard — a falsy or non-string canonical value
          (``""`` / ``0`` / ``42``) is returned verbatim and short-circuits
          (legacy assigns it to ``url`` then the ``if not url`` chain may fall
          through, so a falsy canonical value can still be overridden by the
          youtube / bare slots), whereas :attr:`url` coerces and falsy-guards.

        Every other branch (youtube ``[5][0]`` string-only, bare ``[0]``
        http-prefixed when ``allow_bare_http``) matches :attr:`url` exactly,
        so only the canonical slot needs a bespoke read.
        """
        if not isinstance(metadata, list):
            return None
        url: str | None = None
        if len(metadata) > cls._META_URL_POS:
            url_list = metadata[cls._META_URL_POS]
            if isinstance(url_list, list) and len(url_list) > 0:
                url = url_list[cls._LIST_FIRST_POS]
        if not url and len(metadata) > cls._META_YOUTUBE_POS:
            yt_data = metadata[cls._META_YOUTUBE_POS]
            if (
                isinstance(yt_data, list)
                and len(yt_data) > 0
                and isinstance(yt_data[cls._LIST_FIRST_POS], str)
            ):
                url = yt_data[cls._LIST_FIRST_POS]
        if not url and allow_bare_http and len(metadata) > cls._META_GOOGLE_DOCS_POS:
            candidate = metadata[cls._META_GOOGLE_DOCS_POS]
            if isinstance(candidate, str) and candidate.startswith("http"):
                url = candidate
        return url

    def _settings_block(self) -> list[Any] | None:
        """The ``Source.settings`` sub-list at ``self._raw[3]``, or ``None``.

        ``None`` when the slot is absent or not a list — several valid
        non-listing response shapes omit the block entirely, so both callers
        (:attr:`status` and :attr:`drive_status`) fail closed on it without
        warning.
        """
        if len(self._raw) <= self._STATUS_BLOCK_POS:
            return None
        block = self._raw[self._STATUS_BLOCK_POS]
        return block if isinstance(block, list) else None

    @property
    def status(self) -> SourceStatus:
        """Processing status from ``self._raw[3][1]``.

        Used by ``GET_NOTEBOOK`` source-list rows where every entry
        carries a status block. Returns :data:`SourceStatus.UNKNOWN` when:

        * position 3 is absent / non-list / too short, or
        * the status code is not one of the known enum values.

        Unknown numeric codes emit a warning — once per code — so backend enum
        drift is visible without repeating on every poll of the same source.
        Structurally malformed status blocks fail closed without warning because
        several valid non-listing response shapes omit the block entirely.
        """
        settings = self._settings_block()
        if settings is None or len(settings) <= self._STATUS_INNER_POS:
            return SourceStatus.UNKNOWN

        status_code = settings[self._STATUS_INNER_POS]
        if not isinstance(status_code, int) or isinstance(status_code, bool):
            return SourceStatus.UNKNOWN
        try:
            return SourceStatus(status_code)
        except ValueError:
            # Warn once per code, like the sibling unmapped-enum path
            # (``_types/sources.py::_warned_source_types``): ``SourceRow.status``
            # is re-decoded on every poll, so an unmapped code on a source being
            # waited on would otherwise repeat the same line ~17 times per wait.
            if status_code not in _warned_status_codes:
                _warned_status_codes.add(status_code)
                logger.warning(
                    "Unknown source status code %r from RPC %s; treating as UNKNOWN",
                    status_code,
                    self.method_id,
                )
            return SourceStatus.UNKNOWN

    @property
    def drive_status(self) -> DriveSourceStatus | None:
        """Drive-side health from ``self._raw[3][3]`` — ``None`` when absent.

        ``SourceSettings.userDriveSourceStatus`` (tag 4). Only Drive-backed
        rows carry it: in a 409-row live capture, 4 rows populated it (all
        Drive-backed, all ``ACTIVE``) and the other 405 left it absent — 402
        with ``settings=[null,2]`` and 3 with ``settings=[null,2,[...]]`` in
        the 2026-08-07 Web/Android wire audit.

        Return values:

        * ``None`` — the slot is absent, null, an explicit ``0``
          (``DRIVE_SOURCE_STATUS_UNSPECIFIED``, which proto3 normally omits
          anyway), or the settings block is short. All four mean the same
          thing — "this row makes no Drive-health claim" — so they share one
          representation. Absence is therefore NOT proof that a source is not
          Drive-backed (``SourceRow.drive_document_id`` answers that).
        * A :class:`~notebooklm.rpc.types.DriveSourceStatus` member for a
          mapped code.
        * :attr:`~notebooklm.rpc.types.DriveSourceStatus.UNKNOWN` when the slot
          IS populated but carries a code (or type) this client does not model
          — deliberately distinct from ``None`` so "backend said something we
          could not read" never masquerades as "backend said nothing".

        Unmapped values warn once per raw value, like :attr:`status`: a Drive
        source polled by ``wait_until_ready`` re-decodes this on every poll.
        """
        settings = self._settings_block()
        if settings is None or len(settings) <= self._DRIVE_STATUS_INNER_POS:
            return None
        code = settings[self._DRIVE_STATUS_INNER_POS]
        if code is None:
            return None
        # ``bool`` is an ``int`` subclass, and a JSON ``true``/``false`` here is
        # drift, not a status — so it must fall through to the warn path rather
        # than decode as 1 / 0.
        if isinstance(code, int) and not isinstance(code, bool):
            # An explicit ``UNSPECIFIED`` (0) collapses into the same ``None``
            # an absent slot yields: both say "no claim", and one state must
            # not have two representations.
            if code == _DRIVE_STATUS_UNSPECIFIED:
                return None
            # ``UNKNOWN`` is a client-side sentinel, not a wire value, so a
            # literal -1 is drift and must warn rather than round-trip.
            if code != DriveSourceStatus.UNKNOWN:
                try:
                    return DriveSourceStatus(code)
                except ValueError:
                    pass
        self._warn_unmapped_drive_status(code)
        return DriveSourceStatus.UNKNOWN

    def _warn_unmapped_drive_status(self, code: Any) -> None:
        """Warn once per ``(RPC, unmapped value)`` pair.

        Keyed like the sibling :data:`_warned_drive_id_slots` (which keys on
        ``(method_id, block_pos)``) so the same drifted code arriving from a
        second RPC is still reported once, naming that RPC.
        """
        rendered = repr(code)[:_MAX_DRIFT_REPR_LEN]
        key = (self.method_id, rendered)
        if key in _warned_drive_status_codes:
            return
        _warned_drive_status_codes.add(key)
        logger.warning(
            "Unknown Drive source status %s from RPC %s; treating as UNKNOWN. "
            "Drive-side health for this source cannot be reported",
            rendered,
            self.method_id,
        )


@dataclass(frozen=True)
class SourceGuideRow:
    """Typed view of a ``GET_SOURCE_GUIDE`` response payload.

    Shape: ``[[[ ..., summary_block, keyword_block, ... ]]]`` — the AI summary
    and keyword data live one wrapper deep at ``result[0][0]``. This adapter
    centralises the ``result[0]`` / ``[0][0]`` envelope unwrap and the
    ``inner[1]`` summary-block / ``inner[2]`` keyword-block reads that
    ``_source/content.get_guide`` previously open-coded.

    Every read is a **soft length-guarded degrade** preserving the legacy
    contract exactly: an absent / non-list envelope, summary block, or keyword
    block leaves the default (``""`` summary / ``[]`` keywords) rather than
    raising — the guide endpoint legitimately omits these for un-summarised
    sources.
    """

    _raw: Any = field(repr=False)

    _OUTER_POS: ClassVar[int] = 0
    _INNER_POS: ClassVar[int] = 0
    _SUMMARY_BLOCK_POS: ClassVar[int] = 1
    _KEYWORD_BLOCK_POS: ClassVar[int] = 2
    _LIST_FIRST_POS: ClassVar[int] = 0

    @property
    def _inner(self) -> list[Any] | None:
        """The ``result[0][0]`` record carrying summary/keyword blocks, or ``None``.

        Mirrors the legacy nested ``isinstance``/``len`` guards: a falsy or
        non-list ``result``, ``result[0]``, or ``result[0][0]`` all yield
        ``None`` (the "no guide content" default path).
        """
        result = self._raw
        if not (isinstance(result, list) and len(result) > self._OUTER_POS):
            return None
        outer = result[self._OUTER_POS]
        if not (isinstance(outer, list) and len(outer) > self._INNER_POS):
            return None
        inner = outer[self._INNER_POS]
        return inner if isinstance(inner, list) else None

    @property
    def summary(self) -> str:
        """AI summary at ``inner[1][0]`` — ``""`` when absent / non-string.

        Preserves the legacy ``get_guide`` contract: an absent / non-list
        summary block, or a present block whose first element is not a string,
        both yield the empty summary.
        """
        inner = self._inner
        if inner is None or len(inner) <= self._SUMMARY_BLOCK_POS:
            return ""
        block = inner[self._SUMMARY_BLOCK_POS]
        if not isinstance(block, list) or not block:
            return ""
        first = block[self._LIST_FIRST_POS]
        return first if isinstance(first, str) else ""

    @property
    def keywords(self) -> list[Any]:
        """Keyword list at ``inner[2][0]`` — ``[]`` when absent / non-list.

        Preserves the legacy ``get_guide`` contract: an absent / non-list
        keyword block, or a present block whose first element is not a list,
        both yield the empty keyword list.
        """
        inner = self._inner
        if inner is None or len(inner) <= self._KEYWORD_BLOCK_POS:
            return []
        block = inner[self._KEYWORD_BLOCK_POS]
        if not isinstance(block, list) or not block:
            return []
        first = block[self._LIST_FIRST_POS]
        return first if isinstance(first, list) else []


@dataclass(frozen=True)
class SourceFulltextRow:
    """Typed view of a ``GET_SOURCE`` response payload (fulltext fetch).

    Shape: ``[descriptor, ?, ?, text_block, html_block, ...]`` where the
    leading ``descriptor`` row carries ``[id_envelope, title, metadata, ...]``
    (the same normalized-entry layout :class:`SourceRow` wraps), the text
    content lives at ``result[3][0]`` and the HTML rendition at
    ``result[4][1]``. This adapter centralises the ``result[0]`` /
    ``descriptor[1]`` / ``descriptor[2]`` / ``result[3]`` / ``result[4]``
    envelope reads that ``_source/content.get_fulltext`` previously open-coded.

    Every read is a **soft length-guarded degrade** preserving the legacy
    contract exactly: missing slots yield empty defaults (``""`` title, ``None``
    metadata / html, ``None`` text-blocks) rather than raising — a partially
    populated source response is normal.
    """

    _raw: Any = field(repr=False)

    _DESCRIPTOR_POS: ClassVar[int] = 0
    _TITLE_POS: ClassVar[int] = 1
    _METADATA_POS: ClassVar[int] = 2
    _TEXT_BLOCK_POS: ClassVar[int] = 3
    _HTML_BLOCK_POS: ClassVar[int] = 4
    _HTML_CANDIDATE_POS: ClassVar[int] = 1
    _TEXT_CONTENT_POS: ClassVar[int] = 0
    _METADATA_TYPE_POS: ClassVar[int] = 4

    @property
    def descriptor(self) -> list[Any] | None:
        """The ``result[0]`` source-descriptor row, or ``None``.

        ``None`` when ``result`` is non-list or the descriptor slot is absent /
        non-list / too short to carry a title — mirrors the legacy
        ``isinstance(descriptor, list) and len(descriptor) > 1`` guard.
        """
        result = self._raw
        if not (isinstance(result, list) and len(result) > self._DESCRIPTOR_POS):
            return None
        descriptor = result[self._DESCRIPTOR_POS]
        if not isinstance(descriptor, list) or len(descriptor) <= self._TITLE_POS:
            return None
        return descriptor

    @property
    def title(self) -> str:
        """Source title at ``descriptor[1]`` — ``""`` when absent / non-string."""
        descriptor = self.descriptor
        if descriptor is None:
            return ""
        value = descriptor[self._TITLE_POS]
        return value if isinstance(value, str) else ""

    @property
    def metadata(self) -> list[Any] | None:
        """Metadata sub-list at ``descriptor[2]`` — ``None`` when absent / non-list."""
        descriptor = self.descriptor
        if descriptor is None or len(descriptor) <= self._METADATA_POS:
            return None
        value = descriptor[self._METADATA_POS]
        return value if isinstance(value, list) else None

    @property
    def source_row(self) -> SourceRow | None:
        """The descriptor wrapped as a :class:`SourceRow` (for ``type_code``).

        ``None`` when there is no descriptor; otherwise the descriptor carries
        the adapter's normalized-entry layout so ``SourceRow.type_code`` reads
        ``metadata[4]`` with the standard int-validating soft contract.
        """
        descriptor = self.descriptor
        if descriptor is None:
            return None
        return SourceRow.from_entry(descriptor, method_id=RPCMethod.GET_SOURCE.value)

    @property
    def raw_metadata_type_slot(self) -> Any:
        """Raw ``metadata[4]`` value (for the malformed-type-code WARNING).

        Returns ``None`` when metadata is absent or the type slot is missing.
        The consumer logs a diagnostic when this is present-but-non-int while
        :attr:`SourceRow.type_code` resolved to ``None`` (#1485 policy); the
        raw value is surfaced so the consumer can name its type in the log.
        """
        metadata = self.metadata
        if metadata is None or len(metadata) <= self._METADATA_TYPE_POS:
            return None
        return metadata[self._METADATA_TYPE_POS]

    @property
    def html_content(self) -> str | None:
        """HTML rendition at ``result[4][1]`` — ``None`` when absent / non-string.

        Mirrors the legacy markdown-path guard: an absent / non-list HTML block,
        a block too short to carry the candidate, or a non-string candidate all
        yield ``None`` ("no markdown rendition").
        """
        result = self._raw
        if not (isinstance(result, list) and len(result) > self._HTML_BLOCK_POS):
            return None
        block = result[self._HTML_BLOCK_POS]
        if not isinstance(block, list) or len(block) <= self._HTML_CANDIDATE_POS:
            return None
        candidate = block[self._HTML_CANDIDATE_POS]
        return candidate if isinstance(candidate, str) else None

    @property
    def text_content_blocks(self) -> list[Any] | None:
        """The document ``Body`` at ``result[3][0]`` — ``None`` when absent / non-list.

        ``result[3]`` is the source's ``tailwindDoc`` and ``[3][0]`` its
        ``Body``: a structured tree, not the pre-flattened string list the name
        suggests. ``_web/sources/content.py`` derives two things from it — the legacy
        newline-joined ``SourceFulltext.content`` and, since #2128, the parsed
        ``SourceFulltext.document`` (via
        :func:`notebooklm._web.rows.documents.build_document`, which owns
        every position below this one).

        Mirrors the legacy text-path guard: a falsy / non-list ``result[3]`` or
        a non-list ``result[3][0]`` yields ``None`` (empty content + warning).
        """
        result = self._raw
        if not (isinstance(result, list) and len(result) > self._TEXT_BLOCK_POS):
            return None
        block = result[self._TEXT_BLOCK_POS]
        if not isinstance(block, list) or not block:
            return None
        blocks = block[self._TEXT_CONTENT_POS]
        return blocks if isinstance(blocks, list) else None


def interpret_source_freshness(result: Any) -> bool:
    """Decode a ``CHECK_SOURCE_FRESHNESS`` payload into a freshness bool.

    Shapes by source type: ``[]`` or ``[[null, true, [id]]]`` = fresh
    (URL / Drive); bare ``True`` = fresh; bare ``False`` / ``[[null, false,
    ...]]`` = stale. A recognized nested shape carries a *boolean* flag at index
    ``[1]`` (``True`` = fresh, ``False`` = stale).

    Anything else is schema drift, not "stale": ``None``, a bare scalar, a list
    whose first element is a non-list scalar like ``["x"]``, a nested list too
    short to carry the flag, or a nested list whose flag is *non-boolean* (e.g.
    ``[[null, null, ...]]``). Raise ``DecodingError`` so callers can tell a miss
    from drift (#1344). The payload is passed via ``raw_response`` so the
    existing scrub/truncate preview applies instead of leaking it into the
    message.
    """
    if result is True:
        return True
    if result is False:
        return False
    if isinstance(result, list):
        if len(result) == 0:
            return True  # empty array = fresh
        first = result[0]
        if isinstance(first, list) and len(first) > 1:
            if first[1] is True:
                return True
            if first[1] is False:
                return False
    raise DecodingError(
        "Unrecognized CHECK_SOURCE_FRESHNESS payload shape",
        raw_response=repr(result),
        method_id=RPCMethod.CHECK_SOURCE_FRESHNESS.value,
    )


def first_added_source_id(payload: Any, *, method_id: str | None = None) -> str | None:
    """Extract the new source id from an ``AddSourcesAsync`` (``X1snv``) response.

    The response is ``[[[[id], "New Source", [.., type_code]], …], null,
    [[stub, ack], …]]`` — the created stub sources at index 0, each an id
    envelope ``[[id], title, meta]`` (live-captured #2292). Returns the first
    stub's id, decoded via :func:`safe_index` so a shape change RAISES
    ``UnknownRPCMethodError`` rather than silently yielding the wrong slot.
    Returns ``None`` only when the id slot is present but not a non-empty
    string.
    """
    id_value = safe_index(
        payload,
        0,
        0,
        0,
        0,
        method_id=method_id,
        source="_web.sources.play_books.add_play_book",
    )
    return id_value if isinstance(id_value, str) and id_value else None
