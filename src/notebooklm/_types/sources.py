"""Private source type implementations."""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

from .._url_utils import pdf_url_display_title
from .common import (
    UnknownTypeWarning,
)
from .documents import StructuredDocument
from .enums import DriveSourceStatus, SourceStatus

if TYPE_CHECKING:
    from .._web.rows.sources import SourceRow


class SourceType(str, Enum):
    """User-facing source types.

    This is a str enum, so comparisons work with both enum members and strings:
        source.kind == SourceType.WEB_PAGE  # True
        source.kind == "web_page"           # Also True
    """

    GOOGLE_DOCS = "google_docs"
    GOOGLE_SLIDES = "google_slides"
    GOOGLE_SPREADSHEET = "google_spreadsheet"
    GOOGLE_DRIVE = "google_drive"
    PDF = "pdf"
    PASTED_TEXT = "pasted_text"
    WEB_PAGE = "web_page"
    GOOGLE_DRIVE_AUDIO = "google_drive_audio"
    GOOGLE_DRIVE_VIDEO = "google_drive_video"
    YOUTUBE = "youtube"
    MARKDOWN = "markdown"
    DOCX = "docx"
    POWERPOINT = "powerpoint"
    CSV = "csv"
    EPUB = "epub"
    EXCEL = "excel"
    GEMINI_CHAT = "gemini_chat"
    GMAIL = "gmail"
    AI_MODE_CHAT = "ai_mode_chat"
    EXPERT_INTELLIGENCE = "expert_intelligence"
    IMAGE = "image"
    MEDIA = "media"
    UNKNOWN = "unknown"


class PlayBookExportReason(str, Enum):
    """Why a Google Play Books title cannot be added as a source.

    Decoded from the ``reason`` field of a ``ListExpertIntelligenceContent``
    row (backend enum ``Y6a`` in the web bundle). ``None`` — not a member here
    — means the title is exportable. Only :attr:`OPTED_OUT` has been observed
    live; the other three are read off the backend enum and mapped by code.
    """

    OPTED_OUT = "opted_out"  # 1: "Publisher has opted out of content export"
    UNSUPPORTED_CONTENT = "unsupported_content"  # 2: "Unsupported content type"
    NOT_OWNED = "not_owned"  # 3: "Book is not owned"
    LICENSE_RESTRICTION = "license_restriction"  # 4: "License restriction"


#: ``ListExpertIntelligenceContent`` reason code -> :class:`PlayBookExportReason`.
#: Codes are 1-based; ``0``/absent means the title is exportable (``None``).
_PLAY_BOOK_EXPORT_REASON_MAP: dict[int, PlayBookExportReason] = {
    1: PlayBookExportReason.OPTED_OUT,
    2: PlayBookExportReason.UNSUPPORTED_CONTENT,
    3: PlayBookExportReason.NOT_OWNED,
    4: PlayBookExportReason.LICENSE_RESTRICTION,
}


_warned_source_types: set[int] = set()


#: Backend type code -> public :class:`SourceType`.
#:
#: Aligned against the ``OriginalSourceContentType`` enum recovered from the
#: Android binary (``docs/android/enums.txt``, and the checked-in
#: ``read.proto``), which agrees with this map on every code. Two evidence
#: levels, marked inline on the entries themselves:
#:
#: * **live-observed** — a source of that type was created and read back. This
#:   is every code except the three below, and includes ``18``: an Android
#:   ``AddSources`` carrying ``CONTENT_TYPE_GEMINI_CHAT`` reproducibly returns a
#:   source with it, and ``20``: adding a Google Play Book via
#:   ``sources.add_play_book`` reads back as ``EXPERT_INTELLIGENCE`` (#2292).
#: * **schema-only** — ``12``, ``15`` and ``19``. Each is defined by the
#:   recovered enum, and no route this client can reach produces one: ``.xlsx``
#:   is refused at the Web upload ``start`` with HTTP 400, a spreadsheet
#:   imported from Drive comes back as ``14``, and Gmail and AI Mode chat
#:   imports are not exposed on either front door.
#:
#: The schema-only three are mapped anyway so a server that does emit one reads
#: as itself instead of ``UNKNOWN``: a label on a code nothing sends costs
#: nothing, while leaving one unmapped raises ``UnknownTypeWarning`` and sends
#: the next reader hunting — which is exactly how ``18`` was found. Promote an
#: entry's marker once one is observed.
_SOURCE_TYPE_CODE_MAP: dict[int, SourceType] = {
    0: SourceType.UNKNOWN,
    1: SourceType.GOOGLE_DOCS,
    2: SourceType.GOOGLE_SLIDES,  # Was GOOGLE_OTHER, now more specific
    3: SourceType.PDF,
    4: SourceType.PASTED_TEXT,
    5: SourceType.WEB_PAGE,
    6: SourceType.POWERPOINT,
    7: SourceType.GOOGLE_SPREADSHEET,
    8: SourceType.MARKDOWN,
    9: SourceType.YOUTUBE,
    10: SourceType.MEDIA,
    11: SourceType.DOCX,
    12: SourceType.EXCEL,  # schema-only; no reachable producer
    13: SourceType.IMAGE,
    14: SourceType.GOOGLE_DRIVE,
    15: SourceType.GMAIL,  # schema-only; no reachable producer
    16: SourceType.CSV,
    17: SourceType.EPUB,
    18: SourceType.GEMINI_CHAT,  # live: Android AddSources CONTENT_TYPE_GEMINI_CHAT
    19: SourceType.AI_MODE_CHAT,  # schema-only; no reachable producer
    20: SourceType.EXPERT_INTELLIGENCE,  # live: Play Books add (#2292)
}

#: Backend type code for a Play Books (Expert Intelligence) source, derived from
#: :data:`_SOURCE_TYPE_CODE_MAP` so the ``add_play_book`` PROCESSING stub cannot
#: drift from the decode map if the code is ever renumbered (#2292).
_EXPERT_INTELLIGENCE_TYPE_CODE: int = next(
    code
    for code, source_type in _SOURCE_TYPE_CODE_MAP.items()
    if source_type is SourceType.EXPERT_INTELLIGENCE
)


#: Local-file extensions NotebookLM's resumable upload accepts, spelled with the
#: leading dot and lowercased (the form :attr:`pathlib.PurePath.suffix` returns).
#:
#: The **input-side twin** of :data:`_SOURCE_TYPE_CODE_MAP`: that map says how the
#: backend labels a source on the way out, this set says how a user may spell one
#: on the way in. They are deliberately co-located so a newly supported file type
#: cannot gain a decode entry without a spelling — PowerPoint is the case that
#: proved the split (#2137/#2191 added ``6: SourceType.POWERPOINT`` while every
#: consumer of this set still had PowerPoint missing from its own copy, #2202).
#:
#: A *mapping* to :class:`SourceType` is deliberately NOT asserted here: only some
#: of these extensions have a live-captured decode code (pdf→3, md→8, docx→11,
#: pptx→6, csv→16, epub→17), and inventing one for ``.txt`` would put an
#: unverified wire fact in a constant.
#:
#: Every member has been live-probed to READY on the Web upload endpoint. An
#: extension only earns a place here with such a probe: see
#: :data:`_FILE_SHAPED_ONLY_EXTENSIONS` for the four that were removed on
#: 2026-08-31 after the endpoint was found to reject them outright.
_UPLOAD_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".csv",
        ".docx",
        ".epub",
        ".md",
        ".markdown",
        ".pdf",
        ".pptx",
        ".txt",
    }
)

#: File-shaped extensions that are NOT asserted to be upload-accepted.
#:
#: The escape hatch that keeps :data:`_UPLOAD_FILE_EXTENSIONS` honest. The two
#: sets feed consumers with very different failure modes, so an extension has to
#: earn the first one:
#:
#: * the path heuristic only decides whether a *non-existent* argument gets a
#:   warning — a wrong entry costs nothing;
#: * the Drive router (``_web.sources.drive_import``) uses the upload set as a
#:   **network** gate — a wrong entry turns a fast, clear client-side refusal
#:   into a full file download followed by a murky server-side failure.
#:
#: ``.ppt`` sits here because the evidence covers ``.pptx`` only: a real ``.pptx``
#: upload was live-probed to READY decoding as ``POWERPOINT`` (#2202), while
#: legacy ``.ppt`` — a different container (OLE compound file, not OOXML) — has
#: never been put on the wire. Renaming a ``.pptx`` to ``.ppt`` would prove
#: nothing. Move it up once a real legacy ``.ppt`` is probed; until then a
#: mistyped ``deck.ppt`` still earns its warning and the Drive router still
#: refuses it up front, which is the behavior with evidence behind it.
#:
#: ``.doc``, ``.odt``, ``.rtf`` and ``.tsv`` were moved down out of the upload
#: set on 2026-08-31. Each had been asserted as uploadable without ever being
#: put on the wire; a real file of each type is refused at the Web resumable
#: ``start`` with **HTTP 400**, and identically so when the content type is
#: forced to ``text/plain`` — so the endpoint refuses them by extension, not by
#: MIME. (Android's upload frontend errors on all four as well.) Leaving them in
#: the upload set was the exact failure its own docstring warns about: the Drive
#: router treated them as a green light and downloaded whole files before the
#: server rejected them. Move one back up only with a live READY probe.
_FILE_SHAPED_ONLY_EXTENSIONS: frozenset[str] = frozenset({".doc", ".odt", ".ppt", ".rtf", ".tsv"})

#: HTML-family extensions NotebookLM's upload endpoint **rejects**. Tracked next to
#: the accepted set (rather than folded into it) because the two callers want them
#: on opposite sides: the upload/Drive gates reject them with convert-first
#: guidance, while the path heuristic must still recognise ``page.html`` as
#: file-shaped so a mistyped one is flagged rather than silently pasted in as
#: text content.
_HTML_FILE_EXTENSIONS: frozenset[str] = frozenset({".html", ".htm", ".xhtml", ".xht"})

#: Extensions that make an argument *look like a local file path*.
#:
#: Scope, precisely: ``source add``'s auto-detect checks ``Path(content).exists()``
#: BEFORE consulting this set, so an existing file is uploaded on its own merits
#: whatever its extension. This set decides what happens to a file-shaped argument
#: that does **not** exist — whether the user gets the "looks like a path but does
#: not exist" warning or has the string silently ingested as pasted text. A missing
#: extension therefore costs a typo'd filename its warning, which is exactly the
#: footgun the warning exists to catch (#2202).
#:
#: Derived — never hand-maintained — so a type gains its spelling here the moment
#: it becomes uploadable.
_PATH_SHAPED_FILE_EXTENSIONS: frozenset[str] = (
    _UPLOAD_FILE_EXTENSIONS | _HTML_FILE_EXTENSIONS | _FILE_SHAPED_ONLY_EXTENSIONS
)


_SOURCE_TYPE_COMPAT_MAP: dict[SourceType, str] = {
    SourceType.GOOGLE_DOCS: "text",
    SourceType.GOOGLE_SLIDES: "text",
    SourceType.GOOGLE_SPREADSHEET: "text",
    SourceType.GOOGLE_DRIVE: "text",
    SourceType.PDF: "text_file",
    SourceType.PASTED_TEXT: "text",
    SourceType.WEB_PAGE: "url",
    SourceType.YOUTUBE: "youtube",
    SourceType.MARKDOWN: "text_file",
    SourceType.DOCX: "text_file",
    SourceType.POWERPOINT: "text_file",
    SourceType.CSV: "text",
    SourceType.EPUB: "text_file",
    SourceType.EXCEL: "text_file",
    SourceType.GEMINI_CHAT: "text",
    SourceType.GMAIL: "text",
    SourceType.AI_MODE_CHAT: "text",
    SourceType.EXPERT_INTELLIGENCE: "text",
    SourceType.IMAGE: "text",
    SourceType.MEDIA: "text",
    SourceType.UNKNOWN: "text",
}


# Code 14 is the backend's catch-all for a Drive-hosted file it does not give a
# format-specific code (#1828/#1832). Measured on the Web backend by importing
# one Drive file of each class and reading back ``Source._type_code``:
#
#     Google Doc                       -> 1   (GOOGLE_DOCS)
#     Drive-hosted PDF                 -> 3   (PDF)
#     Google Sheet                     -> 14
#     Drive-hosted .txt / .csv         -> 14
#     Drive-hosted .docx / .pptx       -> 14
#
# Only Docs and PDFs get their own code; everything else collapses to 14, which
# is why the recovered mobile enum names it ``SOURCE_CONTENT_TYPE_DRIVE`` and not
# a spreadsheet. That enum agrees with this map on every other code it defines
# (0-6, 8-11, 13, 16, 17), so the two are the same numbering and 14 was simply
# mislabelled here.
#
# Live capture showed Drive sources carry no URL (metadata[5]/[7] are null and
# metadata[0] holds the Drive metadata block — see ``SourceRow.drive_document_id``
# — rather than a URL), so disambiguation uses the original-content MIME at
# Source tag 8 first (#2112), then the Drive-only MIME at metadata[19] /
# metadata[9][2].
#
# A native Sheet carries "application/vnd.google-apps.spreadsheet" and is
# remapped to 7, the code the recovered enum reserves for GOOGLE_SHEET, which
# preserves the one labelling that was already correct under the old map. A
# Drive PDF carries "application/pdf" (→ 3). Only MIMEs proven by live capture
# are mapped; anything else under 14 stays GOOGLE_DRIVE — honest for a file the
# backend itself declined to classify. Extend as more collisions are captured.
_TYPE_CODE_14_MIME_OVERRIDE: dict[str, int] = {
    "application/pdf": 3,  # Drive-hosted PDF → PDF
    "application/vnd.google-apps.spreadsheet": 7,  # native Sheet → GOOGLE_SPREADSHEET
}


def _disambiguate_type_code(type_code: int | None, mime: str | None) -> int | None:
    """Correct the ambiguous ``type_code == 14`` using the row MIME (#1832).

    Returns the effective type code: a Drive file whose MIME maps in
    :data:`_TYPE_CODE_14_MIME_OVERRIDE` is remapped (PDF → 3, native Sheet → 7);
    with no MIME or an unrecognized one the code is returned unchanged and
    decodes as ``GOOGLE_DRIVE``, which is what the backend actually said.
    """
    if type_code == 14 and mime is not None:
        return _TYPE_CODE_14_MIME_OVERRIDE.get(mime, type_code)
    return type_code


def _safe_source_type(type_code: int | None) -> SourceType:
    """Convert internal type code to user-facing SourceType enum."""
    if type_code is None:
        return SourceType.UNKNOWN

    result = _SOURCE_TYPE_CODE_MAP.get(type_code)
    if result is None:
        if type_code not in _warned_source_types:
            _warned_source_types.add(type_code)
            warnings.warn(
                f"Unknown source type code {type_code}. "
                "Consider updating notebooklm-py to the latest version.",
                UnknownTypeWarning,
                stacklevel=3,
            )
        return SourceType.UNKNOWN
    return result


def _extract_source_url(metadata: Any, *, allow_bare_http: bool = True) -> str | None:
    """Extract a source URL from a ``src[2]`` metadata array.

    Thin compatibility shim over
    :meth:`notebooklm._web.rows.sources.SourceRow.url_from_metadata`,
    which centralises the ``metadata[7]`` > ``metadata[5]`` > ``metadata[0]``
    positional precedence in the sanctioned row-adapter layer. The adapter
    method reproduces this helper's exact (soft, un-coerced) semantics, so this
    re-exported public helper is behavior-preserved while its position
    knowledge no longer lives here.
    """
    from .._web.rows.sources import SourceRow

    return SourceRow.url_from_metadata(metadata, allow_bare_http=allow_bare_http)


def _extract_source_created_at(metadata: Any) -> datetime | None:
    """Extract a source creation timestamp from a ``src[2]`` metadata array.

    Thin compatibility shim over
    :meth:`notebooklm._web.rows.sources.SourceRow.created_at_from_metadata`,
    which owns the ``metadata[2][0]`` timestamp position. Behavior-identical to
    the original inline walk (both funnel the inner value through
    :func:`_datetime_from_timestamp`).
    """
    from .._web.rows.sources import SourceRow

    return SourceRow.created_at_from_metadata(metadata)


#: Drive states that mean "the notebook's copy is not a faithful view of a
#: readable live Drive file" — what :attr:`Source.is_drive_degraded` reports.
#: An explicit allowlist, not ``!= ACTIVE``: the client-side ``UNKNOWN``
#: sentinel says nothing about health, and a future backend member must be
#: classified deliberately rather than inherit "degraded" by default.
_DEGRADED_DRIVE_STATUSES: frozenset[DriveSourceStatus] = frozenset(
    {
        DriveSourceStatus.INACCESSIBLE,
        DriveSourceStatus.SYNCING,
        DriveSourceStatus.DELETED,
        DriveSourceStatus.GEN_AI_ACCESS_DENIED,
    }
)


def _pdf_url_title_fallback(
    title: str | None, url: str | None, type_code: int | None
) -> str | None:
    """Derive a display title for a direct-PDF-URL source, or return ``title``.

    Direct-PDF URLs arrive with the raw request URL in the title slot (the
    server extracts ``<title>`` for HTML pages but not for a link that points
    straight at a ``.pdf``), so this falls back to the URL path basename —
    e.g. ``https://host/papers/SomePaper.pdf`` → ``SomePaper`` (#1850).

    Fires only when the title is *exactly* the source ``url`` (the server
    degradation — never a user-set title that merely resembles a URL, e.g. a
    PDF renamed to a URL string) and the source is a PDF. Uses a plain
    :data:`_SOURCE_TYPE_CODE_MAP` lookup rather than :func:`_safe_source_type`
    so parsing an unknown-typed source never emits ``UnknownTypeWarning`` at
    construction time (the warning stays at ``.kind`` access).

    Shared by :meth:`Source.from_row` (add + list paths) and the
    ``source fulltext`` / ``GET_SOURCE`` read (``SourceContentRenderer``), so
    every user-visible read of a degraded PDF title is corrected consistently.
    """
    if (
        title is not None
        and title == url
        and type_code is not None
        and _SOURCE_TYPE_CODE_MAP.get(type_code) is SourceType.PDF
    ):
        return pdf_url_display_title(title) or title
    return title


@dataclass(frozen=True)
class PlayBook:
    """One title from a Google Play Books library.

    A row of :meth:`~notebooklm.NotebookLMClient.sources.list_play_books`,
    decoded from ``ListExpertIntelligenceContent`` ("Expert Intelligence" —
    the Discover-page offer to add purchased ebooks; US only, 18+). The listing
    returns **every** library title, not only the addable ones — inspect
    :attr:`export_disabled` before adding.

    A title with :attr:`export_disabled` cannot be added; :attr:`reason` says
    why (``None`` when exportable). :attr:`field_type` is an opaque backend
    double (not a price) that the picker sorts on; it is passed back verbatim
    when the book is added.
    """

    content_id: str
    title: str | None
    authors: tuple[str, ...]
    description_html: str | None
    cover_url: str | None
    export_disabled: bool
    reason: PlayBookExportReason | None
    field_type: float | None
    updated_at: datetime | None

    @property
    def store_url(self) -> str:
        """Play Store detail page for this title."""
        return f"https://play.google.com/store/books/details?id={self.content_id}&pcampaignid=nblm"


@dataclass(frozen=True)
class ExpertIntelligenceSourceMetadata:
    """Play Books provenance carried on an ingested Expert-Intelligence source.

    Decoded from ``SourceMetadata`` field 19
    (``expertIntelligenceSourceMetadata``) and exposed on
    :attr:`Source.expert_intelligence`. Present only on sources of kind
    :attr:`SourceType.EXPERT_INTELLIGENCE`; ``None`` on every other source.
    """

    content_id: str | None
    provider: int | None
    title: str | None
    authors: tuple[str, ...]
    thumbnail_image_url: str | None
    description: str | None
    field_type: float | None


@dataclass(frozen=True)
class RelevantChunk:
    """A source passage returned by :meth:`SourcesAPI.search`.

    ``rank`` is global across the searched sources: lower values are more
    relevant, while ``0`` means the backend omitted a rank. ``start`` and
    ``end`` are source-relative character offsets when the backend supplies a
    span; both are ``None`` when it does not.
    """

    source_id: str
    text: str
    rank: int
    start: int | None = None
    end: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.source_id, str) or not self.source_id:
            raise ValueError("source_id must be a non-empty string")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("text must be a non-empty string")
        if not isinstance(self.rank, int) or isinstance(self.rank, bool) or self.rank < 0:
            raise ValueError("rank must be a non-negative integer")
        for name, value in (("start", self.start), ("end", self.end)):
            if value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or None")
        if (self.start is None) != (self.end is None):
            raise ValueError("start and end must either both be set or both be None")
        if self.start is not None and self.end is not None and self.start > self.end:
            raise ValueError("start must be less than or equal to end")


@dataclass
class Source:
    """Represents a NotebookLM source."""

    id: str
    title: str | None = None
    url: str | None = None
    _type_code: int | None = field(default=None, repr=False)
    created_at: datetime | None = None
    # ``status`` holds a :class:`~notebooklm.rpc.SourceStatus` member (an
    # ``int`` enum) decoded from the GET_NOTEBOOK source-list status block.
    # The annotation was previously ``int`` even though every construction
    # path (the listing service and :meth:`from_api_response`) populates it
    # with a ``SourceStatus``; ``SourceStatus`` is the accurate declared type
    # and remains ``int``-compatible at runtime and for equality.
    status: SourceStatus = SourceStatus.READY
    #: Google Drive file id for Drive-backed sources; ``None`` for every other
    #: kind. Drive sources carry no :attr:`url` (the backend leaves the URL
    #: slots empty), so this is the only field tying such a source back to the
    #: ``file_id`` it was created from — ``sources.add_drive`` matches on it to
    #: stay idempotent when a create has to be retried (#2113).
    drive_document_id: str | None = None
    #: Drive-side health of a Drive-backed source
    #: (``SourceSettings.userDriveSourceStatus``), or ``None`` when the row
    #: makes no Drive-health claim — every non-Drive source, and a Drive source
    #: whose status is the proto3 default. Orthogonal to :attr:`status`, which
    #: reports NotebookLM's own ingestion pipeline: a source whose Drive file
    #: was deleted or unshared keeps reporting ``READY`` because ingestion did
    #: complete (#2111). See :attr:`is_drive_degraded`.
    drive_status: DriveSourceStatus | None = None
    #: Direct URL for downloading the original uploaded file. Populated only
    #: when the backend retains a downloadable blob for the source (#2112).
    download_url: str | None = field(default=None, repr=False)
    #: Human-clickable Drive viewer URL for the original uploaded file, when
    #: the backend supplies one (#2112).
    viewer_url: str | None = field(default=None, repr=False)
    #: True MIME type from the source's content blob descriptor. Unlike the
    #: internal Drive MIME used for type disambiguation, this also covers
    #: ordinary uploaded files (#2112).
    content_mime: str | None = None
    #: Inferred source word count. The wire slot is confirmed live but unnamed
    #: in the recovered schema, so the semantic label remains provisional.
    word_count: int | None = None
    #: Opaque revision identifier from the source metadata revision handle.
    revision_id: str | None = None
    #: Timestamp paired with :attr:`revision_id`, decoded as timezone-aware UTC.
    revision_timestamp: datetime | None = None
    #: Last time the backend reports the source content was modified/refreshed,
    #: decoded as timezone-aware UTC.
    last_modified_at: datetime | None = None
    #: Play Books provenance for an Expert-Intelligence source (kind
    #: :attr:`SourceType.EXPERT_INTELLIGENCE`), decoded from ``SourceMetadata``
    #: field 19; ``None`` for every other source (#2292).
    expert_intelligence: ExpertIntelligenceSourceMetadata | None = None

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""
        return _safe_source_type(self._type_code)

    @property
    def is_ready(self) -> bool:
        """Check if NotebookLM finished ingesting this source (status=READY).

        .. note::
           This reports **NotebookLM's ingestion pipeline only**
           (``SourceSettings.status``). For a Drive-backed source it does not
           track the Drive file: ingestion completes once, and stays complete,
           even after the file is deleted, unshared, or is mid-resync — so
           ``is_ready`` can be ``True`` while chat is grounded on a stale
           snapshot. Drive-side health is reported separately by
           :attr:`drive_status` / :attr:`is_drive_degraded` (#2111); this
           property deliberately does not fold them in, because
           ``wait_until_ready`` would then poll forever on a Drive file that
           can never come back.
        """
        return self.status == SourceStatus.READY

    @property
    def is_drive_degraded(self) -> bool:
        """Whether the backend reports a *non-healthy* Drive state for this source.

        ``True`` only for the four explicitly degraded members —
        ``INACCESSIBLE``, ``SYNCING``, ``DELETED``, ``GEN_AI_ACCESS_DENIED``.
        Everything else is ``False``:

        * ``drive_status is None`` — no Drive-health claim on the row (every
          non-Drive source, and the proto3-default case).
        * ``ACTIVE`` — nothing wrong reported.
        * ``UNKNOWN`` — the slot carried a code this client does not model. A
          state we cannot name is not evidence of degradation; callers who
          prefer to fail closed should read :attr:`drive_status` directly and
          decide for themselves.

        ``False`` therefore means "the backend reported no degradation", NOT
        "the Drive file is confirmed present and readable" — the three cases
        above all report ``False`` without any such confirmation.

        Note that ``SYNCING`` is transient and self-healing: it means the copy
        is mid-update, not broken. Callers wiring this to an alert should
        exclude it (``src.is_drive_degraded and src.drive_status is not
        DriveSourceStatus.SYNCING`` recovers the sticky-fault set).

        The degraded set is an explicit allowlist rather than
        ``!= ACTIVE`` so a future backend member cannot silently start
        reporting every Drive source as broken.

        .. warning::
           ``ACTIVE`` is the only value this project has observed on the wire;
           the degraded members are read off the backend enum. See
           :class:`~notebooklm.rpc.types.DriveSourceStatus`.
        """
        return self.drive_status in _DEGRADED_DRIVE_STATUSES

    @property
    def is_processing(self) -> bool:
        """Check if source is still being processed (status=PROCESSING)."""
        return self.status == SourceStatus.PROCESSING

    @property
    def is_error(self) -> bool:
        """Check if source processing failed (status=ERROR)."""
        return self.status == SourceStatus.ERROR

    @classmethod
    def from_row(cls, row: SourceRow) -> Source:
        """Build a :class:`Source` from a normalized :class:`SourceRow`.

        This is the **single** construction site for a :class:`Source`
        from a parsed source row. Both :meth:`from_api_response` (the
        public classmethod used by ``ADD_SOURCE`` / rename paths) and
        :meth:`notebooklm._web.sources.listing.SourceLister._parse_source`
        (the ``GET_NOTEBOOK`` list/get/poll path) funnel through here so
        every code path produces identical :class:`Source` instances —
        including the decoded :attr:`status`.

        Minimal flat rows historically yield ``_type_code=None`` and skip
        metadata-derived fields. That invariant is now handled by SourceRow
        when no metadata list is present at ``_raw[2]``, so
        :attr:`SourceRow.metadata` returns ``None`` and
        :attr:`~SourceRow.type_code` / :attr:`~SourceRow.url` /
        :attr:`~SourceRow.created_at` and all optional enrichment properties
        resolve to ``None`` while
        :attr:`~SourceRow.status` resolves to ``SourceStatus.UNKNOWN``. The
        single field mapping below therefore covers all three wire shapes
        identically.
        """
        # Correct the type_code==14 native-Sheet/Drive-PDF overload before it
        # reaches ``kind`` (#1832). Prefer the original-content MIME, then fall
        # back to the Drive-only MIME if the first value is not a known
        # override. Both passes are no-ops for every other code and real Sheets.
        type_code = _disambiguate_type_code(row.type_code, row.content_mime)
        type_code = _disambiguate_type_code(type_code, row.mime)
        return cls(
            id=row.id,
            # #1850: a direct-PDF URL arrives with the raw URL in the title slot
            # (the server extracts <title> for HTML pages but not for a link
            # that points straight at a .pdf). Fall back to the URL path
            # basename. This single funnel covers the add and list paths.
            title=_pdf_url_title_fallback(row.title, row.url, type_code),
            url=row.url,
            _type_code=type_code,
            created_at=row.created_at,
            status=row.status,
            drive_document_id=row.drive_document_id,
            drive_status=row.drive_status,
            download_url=row.download_url,
            viewer_url=row.viewer_url,
            content_mime=row.content_mime,
            word_count=row.word_count,
            revision_id=row.revision_id,
            revision_timestamp=row.revision_timestamp,
            last_modified_at=row.last_modified_at,
            expert_intelligence=row.expert_intelligence,
        )

    @classmethod
    def from_api_response(
        cls,
        data: list[Any],
        notebook_id: str | None = None,
        *,
        method_id: str | None = None,
    ) -> Source:
        """Parse source data from various API response formats.

        Multi-shape dispatch (the three wire shapes — deeply nested,
        medium nested, flat) is centralised in
        :meth:`notebooklm._web.rows.sources.SourceRow.from_unknown_shape`;
        position knowledge for the entry layout lives on
        :class:`SourceRow` itself. This method only normalizes the wire
        shape into a :class:`SourceRow` and defers to :meth:`from_row` —
        the single construction site shared with the
        ``GET_NOTEBOOK`` list/get/poll path
        (:meth:`notebooklm._web.sources.listing.SourceLister._parse_source`) —
        so all paths produce identical :class:`Source` instances,
        including the decoded :attr:`status`. ``status`` earlier silently
        fell back to the ``SourceStatus.READY`` default here while the
        listing path read it from the row.

        Args:
            data: Raw decoded source payload (one of the three wire
                shapes handled by
                :meth:`~notebooklm._web.rows.sources.SourceRow.from_unknown_shape`).
            notebook_id: Accepted for call-site symmetry and forward
                compatibility but currently unused — the parsed source
                wire shape carries no notebook reference, so this value
                does not influence the returned :class:`Source`. It is
                retained (rather than dropped) because
                ``Source.from_api_response`` is tracked public surface;
                removing the parameter would be a backward-incompatible
                signature change flagged by
                ``scripts/audit_public_api_compat.py``.
            method_id: Originating RPC method id (for example, the add-source
                or update-source method identifier) used only to tag checked
                row-decode drift diagnostics with the real method.
                Defaults to ``None``, which lets
                :meth:`~notebooklm._web.rows.sources.SourceRow.from_unknown_shape`
                fall back to its ``GET_NOTEBOOK`` default — preserving
                the historical behavior for callers that do not pass it.
        """
        # Keep the row-adapter dependency local so importing the source
        # dataclass package does not pull source-row parsing helpers into
        # the top-level public type facade.
        from .._web.rows.sources import SourceRow

        return cls.from_row(SourceRow.from_unknown_shape(data, method_id=method_id))


@dataclass
class SourceFulltext:
    """Full text content of a source as indexed by NotebookLM.

    Attributes:
        source_id: The source UUID.
        title: The source title.
        content: The legacy flat rendering — every text run the document tree
            contains, joined with ``"\\n"`` in traversal order. Kept
            byte-identical to its pre-#2128 output so existing callers are
            unaffected, which also means **its offsets are not the backend's**:
            the joins insert separators the wire's character ranges never
            accounted for. Use :attr:`document` when offsets matter, and
            :attr:`rendered_content` when readability does — joining *runs*
            rather than *blocks* splits paragraphs mid-sentence (#2211).
        url: The source URL, when it has one.
        char_count: ``len(content)`` — Python characters over the legacy flat
            rendering, **including the** ``"\\n"`` **separators that rendering
            inserts**. It is a size, not a coordinate: it is neither the
            document's extent nor comparable to any citation or annotation
            offset (those are UTF-16 code units over :attr:`document`, which
            has no separators). On the module's own test source the two read
            548 and 532. Use :attr:`document` for anything positional.
        document: The parsed document tree (#2128) — headings, list structure,
            per-run styling, and the character ranges everything indexes. This
            is the surface citation alignment is built on: resolve a
            ``ChatReference``'s ``start_char`` / ``end_char`` with
            ``document.slice(start_char, end_char)``. Empty (not ``None``) when
            the response carried no decodable document, so consumers never have
            to branch.

            Not emitted by the CLI ``--json`` / MCP / REST fulltext payloads,
            which stay pinned to their existing key sets.

    See also :attr:`rendered_content` — the readable rendering derived from
    :attr:`document`, and (like :attr:`document`) a Python-API surface only.
    """

    source_id: str
    title: str
    content: str
    _type_code: int | None = field(default=None, repr=False)
    url: str | None = None
    char_count: int = 0
    document: StructuredDocument = field(default_factory=StructuredDocument, repr=False)

    @property
    def kind(self) -> SourceType:
        """Get source type as SourceType enum."""
        return _safe_source_type(self._type_code)

    @property
    def rendered_content(self) -> str:
        """The source's text rendered for reading — one line per text-bearing block.

        :attr:`content` joins every text **run** with ``"\\n"``, and a run is a
        sub-paragraph fragment, so a paragraph the backend split into three runs
        becomes three lines. That is a consequence of flattening a tree nobody
        had parsed, not a rendering anybody chose. Since #2128 the tree *is*
        parsed, so this property renders from it instead — runs joined
        **within** a block, blocks separated, blocks with nothing to read
        omitted. A table is the one exception to "one line per block": it reads
        as one line per **row**, cells tab-separated, from the cell boundaries
        the parse carries alongside its flattened runs (#2230).
        :meth:`~notebooklm.types.StructuredDocument.render` carries the full
        account, including the measurement on this repo's own capture.

        It is derived, additive and free — the document is parsed on every
        ``get_fulltext`` regardless of ``output_format``, so nothing extra is
        fetched, and :attr:`content` is untouched for callers that depend on its
        exact bytes. Derived on *each* access rather than cached, so that it
        cannot go stale against a reassigned :attr:`document`; bind it to a
        local if you read it more than once.

        Like :attr:`content` and unlike
        :attr:`~notebooklm.types.StructuredDocument.text`, this rendering is
        **not** offset-addressable: the separators it inserts are its own.
        Resolve a citation's ``start_char`` / ``end_char`` with
        ``document.slice(...)``, or get a readable window around one from
        ``resolve_chat_reference_passage``.

        **With** ``output_format="markdown"`` **this is not "content, made
        readable".** That format fills :attr:`content` from the response's HTML
        rendition while :attr:`document` is parsed from its text blocks either
        way, so the two are then different renderings of different payload
        slots: markdown here, plain text there. Only in the default ``"text"``
        mode are they the same material laid out two ways.

        ``""`` when the response carried no decodable document (a source whose
        text arrived as bare strings), where :attr:`content` may still have
        text — this is a strictly structural rendering, never a fallback
        flattening. ``document.blocks`` tells the two apart: empty means
        nothing decoded, rather than a source with nothing in it.
        """
        return self.document.render()

    def find_citation_context(
        self,
        cited_text: str,
        context_chars: int = 200,
    ) -> list[tuple[str, int]]:
        """Search for citation text in :attr:`content` and return matching contexts.

        The search key is a prefix of ``cited_text``, capped at 40 characters
        **and at the first block boundary the citation crosses**. Both caps
        matter, and the second one is not an optimisation:

        :attr:`content` joins the document's text runs with ``"\n"`` —
        characters the backend never counted (#2128) — while ``cited_text``
        concatenates the cited blocks with no separator at all (#2120). A key
        that spans a block boundary therefore contains a join that ``content``
        renders differently, and **cannot match anywhere**. That is not
        hypothetical: a source whose first block is a short heading (14
        characters is typical) produces a 40-character key crossing the very
        first boundary, so every multi-block citation into it would fail to
        resolve.

        Before #2120 ``cited_text`` was truncated to the fragment's first block,
        so the key could not straddle a boundary and this never arose. Capping
        here restores that property for the *key* while leaving ``cited_text``
        the complete, offset-accurate value.

        This remains a value-based search against a string the citation offsets
        do not describe, and is now the **fallback** rather than the primary
        path: ``resolve_chat_reference_passage`` resolves by offset against
        :attr:`document` — exact, and needing no search — and only comes here
        for a reference or a source without usable offsets (#2211). Prefer
        ``document.slice()`` directly when you have a range.
        """
        if not cited_text or not self.content:
            return []

        search_text = cited_text[: min(40, self._first_block_boundary(cited_text))]

        matches = []
        pos = 0
        while (idx := self.content.find(search_text, pos)) != -1:
            start = max(0, idx - context_chars)
            end = min(len(self.content), idx + len(search_text) + context_chars)
            matches.append((self.content[start:end], idx))
            pos = idx + len(search_text)

        return matches

    def _first_block_boundary(self, cited_text: str) -> int:
        """Length of ``cited_text`` up to the first document-block boundary.

        Returns ``len(cited_text)`` when no boundary applies — an undecoded
        document, or a citation this source's blocks do not open — so the
        behaviour is unchanged for every case that was already working.
        """
        for block in self.document.blocks:
            if block.text and cited_text.startswith(block.text):
                return len(block.text)
        return len(cited_text)


@dataclass(frozen=True)
class CopiedSource:
    """One ``CopySourcesAsync`` outcome: an original source id and its new copy.

    Returned by :meth:`SourcesAPI.copy`. The backend answers with a mapping
    from each requested source id to the freshly created :class:`Source` row in
    the target notebook, so callers can correlate copies with their originals
    without a follow-up listing.

    Attributes:
        original_id: The id of the source that was copied.
        source: The new source row in the target notebook.
    """

    original_id: str
    source: Source

    def __post_init__(self) -> None:
        if not self.original_id:
            raise ValueError("CopiedSource.original_id must not be empty")
        if not self.source.id:
            raise ValueError("CopiedSource.source must carry the new source id")
