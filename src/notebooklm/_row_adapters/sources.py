"""Source row adapters for raw NotebookLM source response rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, ClassVar

from .._types.common import _datetime_from_timestamp
from ..exceptions import DecodingError
from ..rpc import RPCMethod, safe_index
from ..rpc.types import SourceStatus

__all__ = [
    "SourceFulltextRow",
    "SourceGuideRow",
    "SourceRow",
    "SourceRowShape",
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
    3      status block; ``[3][1]`` is the
           :class:`~notebooklm.rpc.SourceStatus` code (used by
           ``GET_NOTEBOOK`` source-list rows).
    =====  ============================================================

    **Metadata sub-list layout** (``self._raw[2]``):

    =====  ============================================================
    Index  Meaning
    =====  ============================================================
    0      Mixed — sometimes a bare ``http(s)://...`` URL (legacy
           shape, only honored when ``url_allow_bare_http=True``).
    2      timestamp block; ``[2][0]`` is the creation timestamp
           (seconds since epoch).
    4      type code (int — see
           :class:`notebooklm._types.sources.SourceType` mapping in
           ``_types/sources.py``).
    5      youtube/source-specific block; ``[5][0]`` is a YouTube URL.
    7      url block; ``[7][0]`` is the canonical source URL when
           present (takes precedence over ``metadata[5][0]`` and
           ``metadata[0]``).
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

    # Metadata sub-list positions.
    _META_BARE_URL_POS: ClassVar[int] = 0
    _META_TIMESTAMP_POS: ClassVar[int] = 2
    _META_TYPE_POS: ClassVar[int] = 4
    _META_YOUTUBE_POS: ClassVar[int] = 5
    _META_URL_POS: ClassVar[int] = 7

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
        e.g. :class:`notebooklm._source.listing.SourceLister` iterating
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

        Used by :class:`notebooklm._source.listing.SourceLister` to skip
        rows whose id envelopes legacy ``_extract_source_id`` would
        have rejected (returning ``None``) — including the rare
        ``[None, True, [None]]`` drive-payload-with-``None``-inner case
        that :attr:`id` decodes to ``""``.

        Equivalent to ``bool(self.id)``; exposed as a named predicate
        so consumer call sites read intent-first.
        """
        return bool(self.id)

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
        if not self.url_allow_bare_http or len(metadata) <= self._META_BARE_URL_POS:
            return None
        candidate = metadata[self._META_BARE_URL_POS]
        if isinstance(candidate, str) and candidate.startswith("http"):
            return candidate
        return None

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
        timestamp_block = metadata[self._META_TIMESTAMP_POS]
        if not isinstance(timestamp_block, list) or not timestamp_block:
            return None
        value = safe_index(
            timestamp_block,
            0,
            method_id=self.method_id,
            source="SourceRow.created_at_raw",
        )
        return value if isinstance(value, (int, float)) else None

    @property
    def created_at(self) -> datetime | None:
        """Creation timestamp as a :class:`~datetime.datetime`, or ``None``."""
        raw = self.created_at_raw
        if raw is None:
            return None
        return _datetime_from_timestamp(raw)

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

        Used only by :meth:`created_at_from_metadata` so the timestamp walk
        reuses the strict :attr:`created_at` property unchanged. ``_raw[0]``
        / ``_raw[1]`` are placeholders the timestamp path never reads.
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
        return cls._from_metadata(metadata).created_at

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
        if not url and allow_bare_http and len(metadata) > cls._META_BARE_URL_POS:
            candidate = metadata[cls._META_BARE_URL_POS]
            if isinstance(candidate, str) and candidate.startswith("http"):
                url = candidate
        return url

    @property
    def status(self) -> SourceStatus:
        """Processing status from ``self._raw[3][1]``.

        Used by ``GET_NOTEBOOK`` source-list rows where every entry
        carries a status block. Defaults to
        :data:`SourceStatus.READY` when:

        * position 3 is absent / non-list / too short, or
        * the status code is not one of the known enum values.

        This mirrors the legacy ``SourceLister._extract_status``
        contract — same fallback to :data:`SourceStatus.READY` on any
        unrecognised code. The membership check uses ``SourceStatus(...)``
        directly (catching :class:`ValueError`) rather than an explicit
        member tuple so the adapter automatically accepts any new values
        added to :class:`SourceStatus` without a parallel update here.
        """
        if (
            len(self._raw) <= self._STATUS_BLOCK_POS
            or not isinstance(self._raw[self._STATUS_BLOCK_POS], list)
            or len(self._raw[self._STATUS_BLOCK_POS]) <= self._STATUS_INNER_POS
        ):
            return SourceStatus.READY

        status_code = self._raw[self._STATUS_BLOCK_POS][self._STATUS_INNER_POS]
        try:
            return SourceStatus(status_code)
        except ValueError:
            return SourceStatus.READY


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
        """Text-content blocks at ``result[3][0]`` — ``None`` when absent / non-list.

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
