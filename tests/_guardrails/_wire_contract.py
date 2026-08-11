"""The declared mapping from this client's positional constants to real wire fields.

Background
----------
Every row adapter reads ``batchexecute`` responses by hardcoded array index. The
wire carries no field names, so nothing in the codebase says *what* index 3 is —
the constant's own name is the only claim, and a name cannot be wrong-detected.

A 2026-08 audit against the schema recovered from the official Android app found
that several of those names were simply wrong, including two that named fields
which have never existed (see ``ARTIFACT_ROW`` below). Those bugs were invisible
because the unit tests built fixtures matching the *code's belief* rather than
the wire, so they could only ever confirm the bug.

This registry makes the claim explicit and checkable. Each :class:`Mapping` says
"constant ``X`` on class ``Y`` reads protobuf field ``M.f``", and
``test_wire_contract.py`` asserts ``constant == tag - 1`` against
``docs/mobile/schema.proto``.

Adding a constant
-----------------
Every ``_*_POS`` constant in the scanned modules must appear in exactly one of
three places, in descending order of how much is actually known:

* :data:`MAPPINGS` — you can name the protobuf field it reads. Asserted against
  the schema (``constant == tag - 1``).
* :data:`PINNED` — the proto has no name for the slot (an ``addUnused()``
  reservation), but live evidence establishes the meaning. The value is frozen
  and the evidence recorded; a change-detector, not a validation.
* :data:`UNMAPPED` — you don't know. Nothing is asserted, but the reason is on
  the record.

The coverage test fails otherwise, so a new positional read cannot enter the
codebase without someone stating what it points at — or stating, on the record,
that they don't know.

An honest ``UNMAPPED`` entry is much better than a guessed ``MAPPINGS`` entry.
A wrong mapping here is worse than no mapping: it would lend false confidence to
exactly the class of defect this file exists to catch.
"""

from __future__ import annotations

from dataclasses import dataclass

#: Section hints. The proto declares several messages twice — once for the wire
#: and once for the app's local persistence schema, with *different* tags. These
#: select the wire copy.
WIRE = "orchestration.v1"
SOURCE_SETTINGS = "tailwind.v1/source_settings"
#: Placeholder ``cls`` for constants declared at module scope rather than on a class.
MODULE_LEVEL = "<module>"
# Must include the package prefix: `tailwind_doc.pb.dart` alone also matches the
# persistence copy of the same message, which carries different tags.
DOC = "orchestration.v1/tailwind_doc.pb.dart"


@dataclass(frozen=True)
class Mapping:
    """One positional constant, and the wire field it claims to read.

    Normally identified by ``field`` name. When the schema extractor could not
    recover a name (it emits the placeholder ``fieldType``), set ``tag``
    instead and record in ``note`` how the tag was established — otherwise the
    assertion degenerates into checking a number against itself.
    """

    module: str
    cls: str
    const: str
    message: str
    field: str | None = None
    #: Explicit protobuf tag, for fields whose name was not recovered.
    tag: int | None = None
    section: str = WIRE
    #: Set to a GitHub issue reference when the mapping is known-wrong today.
    #: The test xfails these; the fixing PR must remove the marker.
    known_bad: str | None = None
    note: str = ""

    def __post_init__(self) -> None:
        if (self.field is None) == (self.tag is None):
            raise ValueError(
                f"{self.module}.{self.cls}.{self.const}: set exactly one of field= or tag="
            )


@dataclass(frozen=True)
class Unmapped:
    """A constant deliberately not checked, with the reason on the record."""

    module: str
    cls: str
    const: str
    reason: str


@dataclass(frozen=True)
class Pinned:
    """A constant whose meaning is known from LIVE evidence but not from the proto.

    These read ``addUnused()`` slots: the mobile ``BuilderInfo`` reserves the field
    but records no name, so the tag is absent from ``mobile/schema.proto`` and
    cannot be checked against it. What we *can* do is record what the slot means,
    cite the observation that established it, and freeze the value so an
    accidental change is caught.

    This is a **regression pin plus documentation**, not a schema validation — it
    is strictly weaker than a :class:`Mapping` and should be upgraded to one if a
    future schema extraction ever recovers the name.
    """

    module: str
    cls: str
    const: str
    value: int
    means: str
    evidence: str


MAPPINGS: tuple[Mapping, ...] = (
    # ---- Source row: Source ------------------------------------------------
    Mapping("sources", "SourceRow", "_ID_POS", "Source", "sourceId"),
    Mapping("sources", "SourceRow", "_TITLE_POS", "Source", "title"),
    Mapping("sources", "SourceRow", "_METADATA_POS", "Source", "metadata"),
    Mapping(
        "sources",
        "SourceRow",
        "_STATUS_BLOCK_POS",
        "Source",
        "settings",
        note="live-verified: settings=[null,2] decodes as status COMPLETE",
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_STATUS_INNER_POS",
        "SourceSettings",
        "status",
        section=SOURCE_SETTINGS,
        note="the documented source[3][1] descent",
    ),
    # ---- Source row: SourceMetadata ---------------------------------------
    Mapping(
        "sources", "SourceRow", "_META_TYPE_POS", "SourceMetadata", "originalSourceContentType"
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_META_BARE_URL_POS",
        "SourceMetadata",
        "googleDocsMetadata",
        note=(
            "MISLEADING NAME: index 0 is googleDocsMetadata, not a bare URL. The "
            "url_allow_bare_http branch that reads it as a str is dead — real "
            "metadata[0] is null 3076/3116 and a list 40/3116, never a str."
        ),
    ),
    Mapping("sources", "SourceRow", "_META_URL_POS", "SourceMetadata", "webpageMetadata"),
    Mapping(
        "sources",
        "SourceRow",
        "_META_DRIVE_DESCRIPTOR_POS",
        "SourceMetadata",
        "googleDriveSourceMetadata",
    ),
    # ---- Artifact row: Artifact -------------------------------------------
    Mapping("artifacts", "ArtifactRow", "_ID_POS", "Artifact", "artifactId"),
    Mapping("artifacts", "ArtifactRow", "_TITLE_POS", "Artifact", "title"),
    Mapping("artifacts", "ArtifactRow", "_TYPE_POS", "Artifact", "type"),
    Mapping("artifacts", "ArtifactRow", "_STATUS_POS", "Artifact", "status"),
    Mapping("artifacts", "ArtifactRow", "_AUDIO_METADATA_POS", "Artifact", "audioOverview"),
    Mapping("artifacts", "ArtifactRow", "_REPORT_MARKDOWN_POS", "Artifact", "tailoredReport"),
    Mapping("artifacts", "ArtifactRow", "_VIDEO_METADATA_POS", "Artifact", "explainerVideo"),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_OPTIONS_POS",
        "Artifact",
        "app",
        note="quiz/flashcards are AppArtifacts; generationOptions nests under this",
    ),
    Mapping("artifacts", "ArtifactRow", "_INFOGRAPHIC_METADATA_POS", "Artifact", "infographic"),
    Mapping("artifacts", "ArtifactRow", "_SLIDE_DECK_METADATA_POS", "Artifact", "slides"),
    # Known-wrong: these two name fields that do not exist. Index 3 is
    # `sources` (repeated message) and index 5 is `isPubliclyReadable` (bool).
    # `failed_error_text` is consequently dead code that always returns None.
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_ERROR_TEXT_POS",
        "Artifact",
        "sources",
        known_bad="#2134",
        note="constant claims 'failed-artifact error text'; the field is `sources`",
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_ERROR_PAYLOAD_POS",
        "Artifact",
        "isPubliclyReadable",
        known_bad="#2134",
        note="constant claims 'nested error payload'; the field is `isPubliclyReadable`",
    ),
    # ---- Answer row: AnswerResponse / TailwindDoc -------------------------
    Mapping("chat", "AnswerRow", "_TEXT_POS", "AnswerResponse", "response"),
    Mapping("chat", "AnswerRow", "_CONV_BLOCK_POS", "AnswerResponse", "conversationTurnKey"),
    Mapping("chat", "AnswerRow", "_TYPE_BLOCK_POS", "AnswerResponse", "responseDoc"),
    Mapping(
        "chat",
        "AnswerRow",
        "_CITATIONS_POS",
        "TailwindDoc",
        "objects",
        section=DOC,
        note="nested inside responseDoc, not a top-level AnswerResponse index",
    ),
    # ---- Notes: ProjectNote ------------------------------------------------
    Mapping("notes", "NoteRow", "_ID_POS", "ProjectNote", "id"),
    Mapping("notes", "NoteRow", "_CONTENT_POS", "ProjectNote", "content"),
    Mapping("notes", "NoteRow", "_INNER_CONTENT_POS", "ProjectNote", "content"),
    Mapping("notes", "NoteRow", "_INNER_TITLE_POS", "ProjectNote", "name"),
    # ---- Research: DiscoveredSource ---------------------------------------
    Mapping("research", "ResearchResultRow", "_URL_POS", "DiscoveredSource", "sourceUrl"),
    Mapping("research", "ResearchResultRow", "_TITLE_POS", "DiscoveredSource", "title"),
    Mapping("research", "ResearchResultRow", "_RESULT_TYPE_POS", "DiscoveredSource", "corpusType"),
    # ---- Notebooks: PromptSuggestion --------------------------------------
    Mapping("notebooks", "PromptSuggestionRow", "_TITLE_POS", "PromptSuggestion", "title"),
    Mapping("notebooks", "PromptSuggestionRow", "_PROMPT_POS", "PromptSuggestion", "prompt"),
    # ---- Account limits: TierLimits ---------------------------------------
    # The extractor could not recover these three field names (it emitted the
    # `fieldType` placeholder), so they are pinned by tag. The tags come from the
    # Dart BuilderInfo disassembly and were confirmed live on BOTH transports
    # against two account tiers: web get_account_limits() and mobile gRPC
    # GetOrCreateAccount returned field-for-field identical values
    # (free: [_, 100, 50, 500000, 1]; Pro cassettes: [_, 500, 300, 500000, 2]),
    # matching Google's published per-tier limits.
    Mapping(
        "_settings",
        MODULE_LEVEL,
        "_NOTEBOOK_LIMIT_INDEX",
        "TierLimits",
        tag=2,
        note=(
            "maxProjects — name unrecovered; tag from BuilderInfo, verified live on both transports"
        ),
    ),
    Mapping(
        "_settings",
        MODULE_LEVEL,
        "_SOURCE_LIMIT_INDEX",
        "TierLimits",
        tag=3,
        note="maxSourcesPerProject — name unrecovered; verified live on both transports",
    ),
)


_UNRECOVERED = (
    "index maps to a tag the schema extractor could not name (the mobile "
    "BuilderInfo marks it addUnused); populated on the wire but unnamed"
)
_NESTED_LOCAL = "index into a nested sub-message, not a tag of a recovered top-level message"
_SHAPE_UNKNOWN = "the enclosing message for this row shape has not been identified"
#: Strongest smell: we decode an index the message has NO field for. Usually a
#: shape borrowed from a different RPC. Distinct from "unmapped" — this is not
#: missing knowledge, it is a positive contradiction with the schema.
_NO_SUCH_FIELD = "READS A NONEXISTENT FIELD:"
_ENVELOPE = (
    "indexes the batchexecute envelope the transport adds, not a field of a "
    "Tailwind protobuf message"
)

UNMAPPED: tuple[Unmapped, ...] = (
    # sources.py
    # NOTE: _META_BARE_URL_POS is now a real mapping (see MAPPINGS) — index 0 is
    # SourceMetadata.googleDocsMetadata, not a bare URL.
    Unmapped("sources", "SourceRow", "_META_TIMESTAMP_POS", _UNRECOVERED),
    Unmapped("sources", "SourceRow", "_META_YOUTUBE_POS", _UNRECOVERED),
    Unmapped("sources", "SourceRow", "_META_MIME_POS", _UNRECOVERED),
    Unmapped("sources", "SourceRow", "_DRIVE_DESCRIPTOR_MIME_POS", _NESTED_LOCAL),
    Unmapped(
        "sources",
        "SourceRow",
        "_ID_ENVELOPE_PLAIN_POS",
        "SourceId.id (tag 1) — the only field the message has",
    ),
    Unmapped(
        "sources",
        "SourceRow",
        "_ID_ENVELOPE_DRIVE_PAYLOAD_POS",
        f"{_NO_SUCH_FIELD} `SourceId` declares exactly one field (`id` = tag 1), so "
        "index 2 (tag 3) cannot exist on it. The `[null, true, [id]]` shape this "
        "branch decodes is the CHECK_SOURCE_FRESHNESS (`yR9Yof`) *response*, "
        "transplanted onto the source-list entry. Real rows are `['uuid']` in "
        "3116/3116 entries — the branch is dead.",
    ),
    Unmapped(
        "sources",
        "SourceRow",
        "_ID_ENVELOPE_DRIVE_INNER_POS",
        f"{_NO_SUCH_FIELD} inner index of the dead CHECK_SOURCE_FRESHNESS-shaped branch above.",
    ),
    Unmapped("sources", "SourceRow", "_LIST_FIRST_POS", _NESTED_LOCAL),
    Unmapped("sources", "SourceGuideRow", "_OUTER_POS", _SHAPE_UNKNOWN),
    Unmapped("sources", "SourceGuideRow", "_INNER_POS", _SHAPE_UNKNOWN),
    Unmapped("sources", "SourceGuideRow", "_SUMMARY_BLOCK_POS", _SHAPE_UNKNOWN),
    Unmapped("sources", "SourceGuideRow", "_KEYWORD_BLOCK_POS", _SHAPE_UNKNOWN),
    Unmapped("sources", "SourceGuideRow", "_LIST_FIRST_POS", _NESTED_LOCAL),
    Unmapped(
        "sources",
        "SourceFulltextRow",
        "_DESCRIPTOR_POS",
        "ambiguous: may read LoadSourceResponse.source or the inner Source; "
        "resolving needs a live gRPC LoadSource, currently blocked",
    ),
    Unmapped("sources", "SourceFulltextRow", "_TITLE_POS", _SHAPE_UNKNOWN),
    Unmapped("sources", "SourceFulltextRow", "_METADATA_POS", _SHAPE_UNKNOWN),
    Unmapped("sources", "SourceFulltextRow", "_TEXT_BLOCK_POS", _SHAPE_UNKNOWN),
    Unmapped(
        "sources",
        "SourceFulltextRow",
        "_HTML_BLOCK_POS",
        "live-verified correct (HTML at result[4][1]) but the enclosing message is unidentified",
    ),
    Unmapped("sources", "SourceFulltextRow", "_HTML_CANDIDATE_POS", _NESTED_LOCAL),
    Unmapped("sources", "SourceFulltextRow", "_TEXT_CONTENT_POS", _NESTED_LOCAL),
    Unmapped("sources", "SourceFulltextRow", "_METADATA_TYPE_POS", _NESTED_LOCAL),
    # artifacts.py
    Unmapped("artifacts", "ArtifactRow", "_AUDIO_MEDIA_LIST_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_MEDIA_URL_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_MEDIA_KIND_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_MEDIA_MIME_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_INFOGRAPHIC_CONTENT_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_INFOGRAPHIC_FIRST_CONTENT_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_INFOGRAPHIC_IMAGE_DATA_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_SLIDE_DECK_PDF_URL_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_SLIDE_DECK_PPTX_URL_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ReportSuggestionRow", "_TITLE_POS", _SHAPE_UNKNOWN),
    Unmapped("artifacts", "ReportSuggestionRow", "_DESCRIPTION_POS", _SHAPE_UNKNOWN),
    Unmapped("artifacts", "ReportSuggestionRow", "_PROMPT_POS", _SHAPE_UNKNOWN),
    Unmapped("artifacts", "ReportSuggestionRow", "_AUDIENCE_LEVEL_POS", _SHAPE_UNKNOWN),
    # chat.py
    Unmapped(
        "chat",
        "AnswerRow",
        "_ANSWER_MARKER_POS",
        "negative index (-1) into a positional message; only lands on "
        "TailwindDoc.type because tag 5 is currently last — see #2121",
    ),
    Unmapped("chat", "SavedChatNoteRow", "_OUTER_NOTE_POS", _NESTED_LOCAL),
    Unmapped("chat", "SavedChatNoteRow", "_ID_POS", _SHAPE_UNKNOWN),
    Unmapped("chat", "SavedChatNoteRow", "_SERVER_TITLE_POS", _SHAPE_UNKNOWN),
    Unmapped("chat", "ConversationTurnRow", "_QUESTION_TEXT_POS", _SHAPE_UNKNOWN),
    Unmapped("chat", "ConversationTurnRow", "_ANSWER_CONTENT_POS", _SHAPE_UNKNOWN),
    Unmapped(
        "chat",
        "StreamFrameRow",
        "_TAG_POS",
        "streamed GenerateFreeFormStreamed frame, not a batchexecute array",
    ),
    Unmapped("chat", "StreamFrameRow", "_INNER_JSON_POS", "streamed frame envelope"),
    Unmapped("chat", "StreamFrameRow", "_ERROR_CODE_POS", "streamed frame envelope"),
    Unmapped("chat", "StreamFrameRow", "_ERROR_PAYLOAD_POS", "streamed frame envelope"),
    Unmapped(
        "chat",
        "ErrorPayloadRow",
        "_STATUS_POS",
        "google.rpc.Status envelope, not a Tailwind message",
    ),
    Unmapped("chat", "ErrorPayloadRow", "_ENTRIES_POS", "google.rpc.Status envelope"),
    Unmapped("chat", "TextLeafRow", "_TEXT_POS", _NESTED_LOCAL),
    Unmapped("chat", "CitationRow", "_CHUNK_BLOCK_POS", _NESTED_LOCAL),
    Unmapped("chat", "CitationRow", "_DETAIL_POS", _NESTED_LOCAL),
    Unmapped("chat", "CitationDetail", "_SCORE_POS", _NESTED_LOCAL),
    Unmapped(
        "chat",
        "CitationDetail",
        "_ANSWER_RANGE_POS",
        "reads Citation tag 4; live-confirmed to carry the fragment's SOURCE-side "
        "union range, not an answer-text range as the field name claims — see #2120",
    ),
    Unmapped(
        "chat",
        "CitationDetail",
        "_PASSAGES_POS",
        "reads the fragment message (a 1-element list), so the passage loop can "
        "never iterate — see #2120",
    ),
    Unmapped("chat", "CitationDetail", "_SOURCE_ID_POS", _NESTED_LOCAL),
    Unmapped("chat", "CitationDetail", "_ANSWER_RANGE_START_POS", _NESTED_LOCAL),
    Unmapped("chat", "CitationDetail", "_ANSWER_RANGE_END_POS", _NESTED_LOCAL),
    Unmapped("chat", "PassageRow", "_PASSAGE_DATA_POS", _NESTED_LOCAL),
    Unmapped("chat", "PassageRow", "_START_POS", _NESTED_LOCAL),
    Unmapped("chat", "PassageRow", "_END_POS", _NESTED_LOCAL),
    Unmapped("chat", "PassageRow", "_TEXT_PAYLOAD_POS", _NESTED_LOCAL),
    # notes.py
    Unmapped("notes", "NoteRow", "_STATUS_POS", "reads the NoteOrStatus wrapper, not ProjectNote"),
    Unmapped("notes", "NoteRow", "_INNER_META_POS", _NESTED_LOCAL),
    Unmapped("notes", "NoteRow", "_META_TIMESTAMP_POS", _NESTED_LOCAL),
    Unmapped("notes", "NoteRow", "_TS_SECONDS_POS", _NESTED_LOCAL),
    # research.py
    Unmapped("research", "ResearchTaskRow", "_ID_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskRow", "_INFO_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskInfoRow", "_QUERY_TEXT_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskInfoRow", "_QUERY_SOURCE_TYPE_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskInfoRow", "_SOURCES_POS", _NESTED_LOCAL),
    Unmapped("research", "ResearchTaskInfoRow", "_SUMMARY_POS", _NESTED_LOCAL),
    Unmapped(
        "research",
        "ResearchResultRow",
        "_LEGACY_CHUNKS_POS",
        "MAPPED AND MIS-MODELLED, not merely unverified. src[6] is a structured "
        "content block with a FIXED schema shared by both row types:\n"
        "    [ text|None, kind:int, text|None, None, int|None, structured_doc|None ]\n"
        "  report row: ['# ...', 3, None, None, None, [doc tree]]   (len 6)\n"
        "  web row:    [None,     1, 'snippet...', None, 13]        (len 5)\n"
        "src[6][1] is the discriminator: 3 = report, 1/2 = web snippet (live "
        "distribution over 63 rows: {1:41, 2:21, 3:1}). Stable across two captures "
        "14 months apart. But `extract_legacy_report_chunks` never reads chunks — it "
        "harvests EVERY string in the block and joins them, working only because each "
        "row type happens to hold exactly one (report at [0], snippet at [2]). "
        "Correct today only because the report row arrives at index 0 and shields the "
        "rest; 62/62 web rows carry a populated src[6]. Reordering the live payload "
        "drops a 35,773-char report for a 4,020-char tardigrade blurb, silently. "
        "FIX: gate on src[6][1] == 3 and read src[6][0].",
    ),
    Unmapped("research", "ResearchResultRow", "_PAYLOAD_TITLE_POS", _NESTED_LOCAL),
    Unmapped("research", "ResearchResultRow", "_PAYLOAD_REPORT_POS", _NESTED_LOCAL),
    Unmapped("research", "ResearchStartRow", "_TASK_ID_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchStartRow", "_REPORT_ID_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ImportedSourceRow", "_ID_POS", _NESTED_LOCAL),
    # ---- module-scope envelope constants ----------------------------------
    # These index batchexecute *envelopes* (the outer wrapper the transport adds),
    # not fields of a Tailwind message, so there is no tag to check them against.
    Unmapped("chat", MODULE_LEVEL, "_LAST_CONVERSATION_ID_POS", _ENVELOPE),
    Unmapped("chat", MODULE_LEVEL, "_TURNS_CONTAINER_POS", _ENVELOPE),
    Unmapped("notebooks", MODULE_LEVEL, "_SUGGEST_PROMPTS_CONTAINER_POS", _ENVELOPE),
    Unmapped("research", MODULE_LEVEL, "_ENVELOPE_OUTER_POS", _ENVELOPE),
    Unmapped("research", MODULE_LEVEL, "_ENVELOPE_PROBE_POS", _ENVELOPE),
    Unmapped("research", MODULE_LEVEL, "_IMPORT_ENVELOPE_OUTER_POS", _ENVELOPE),
    Unmapped("research", MODULE_LEVEL, "_IMPORT_ENVELOPE_PROBE_POS", _ENVELOPE),
    Unmapped("research", MODULE_LEVEL, "_IMPORT_ROW_ID_ENVELOPE_POS", _ENVELOPE),
    Unmapped("research", MODULE_LEVEL, "_IMPORT_ROW_TITLE_POS", _NESTED_LOCAL),
    Unmapped("_mind_maps_api", MODULE_LEVEL, "_CREATE_ARTIFACT_ENVELOPE_POS", _ENVELOPE),
    Unmapped("_mind_maps_api", MODULE_LEVEL, "_CREATE_ARTIFACT_ID_POS", _NESTED_LOCAL),
    Unmapped("_mind_maps_api", MODULE_LEVEL, "_INTERACTIVE_TREE_LEAF_POS", _NESTED_LOCAL),
    Unmapped(
        "chat",
        MODULE_LEVEL,
        "_CHAT_SETTINGS_POS",
        "reads Project tag 8 (the chat persona/config — see #2123), which the "
        f"extractor did not recover a name for. {_UNRECOVERED}",
    ),
    Unmapped(
        "_settings",
        MODULE_LEVEL,
        "_TIER_INDEX",
        "reads TierLimits tag 5 (subscription tier). The mobile BuilderInfo "
        "registers only tags 2-4, so tag 5 is addUnused and cannot be pinned "
        "against the proto — but the wire carries 5 elements and the live value "
        "(1=free / 2=Pro) matches _types/common.py on both transports.",
    ),
)


PINNED: tuple[Pinned, ...] = (
    Pinned(
        "artifacts",
        "ArtifactRow",
        "_TIMESTAMP_POS",
        15,
        "Artifact tag 16 — creation time",
        "33/33 artifacts strictly older than tag 11 lastModifiedTimestamp — never "
        "equal, never greater — across audio/video/report/app/infographic/slides/table",
    ),
    Pinned(
        "artifacts",
        "ArtifactRow",
        "_DATA_TABLE_PAYLOAD_POS",
        18,
        "Artifact tag 19 — per-type content sub-message for ARTIFACT_TYPE_TABLE "
        "(sibling of audioOverview/tailoredReport/infographic/slides)",
        "WEAK: exactly one observation — the single type-9 artifact in a 33-artifact "
        "sweep, carrying its full table doc. Corroborate before relying on it.",
    ),
    Pinned(
        "chat",
        "ConversationTurnRow",
        "_ROLE_POS",
        2,
        "ChatHistoryMessage tag 3 — role, values {1, 2}",
        "56/56 turns populated; role 1 rows carry userQueryText (tag 4) and role 2 "
        "rows carry actOnSourcesResponse (tag 5), 28/28 each",
    ),
)


#: Client enums checked against the recovered backend enums, as
#: ``{our qualified name: (backend enum name, {our_value: BACKEND_MEMBER})}``.
#: Only members we actually claim are listed; the test reports backend members we
#: do not model as informational, not as failures.
ENUM_BINDINGS: dict[str, tuple[str, dict[int, str]]] = {
    "SourceStatus": (
        "SourceStatus",
        {
            1: "SOURCE_STATUS_PENDING",
            2: "SOURCE_STATUS_COMPLETE",
            3: "SOURCE_STATUS_ERROR",
            5: "SOURCE_STATUS_TENTATIVE",
        },
    ),
    "ArtifactStatus": (
        "ArtifactStatus",
        {
            1: "ARTIFACT_STATUS_INITIALIZED",
            2: "ARTIFACT_STATUS_PROCESSING",
            3: "ARTIFACT_STATUS_READY",
            4: "ARTIFACT_STATUS_FAILED",
        },
    ),
    # _types/sources.py::_SOURCE_TYPE_CODE_MAP
    "SourceType": (
        "OriginalSourceContentType",
        {
            1: "SOURCE_CONTENT_TYPE_GOOGLE_DOC",
            2: "SOURCE_CONTENT_TYPE_GOOGLE_SLIDES",
            3: "SOURCE_CONTENT_TYPE_PDF",
            4: "SOURCE_CONTENT_TYPE_TEXT",
            5: "SOURCE_CONTENT_TYPE_URL",
            8: "SOURCE_CONTENT_TYPE_MARKDOWN",
            9: "SOURCE_CONTENT_TYPE_YOUTUBE_VIDEO",
            10: "SOURCE_CONTENT_TYPE_AUDIO",
            11: "SOURCE_CONTENT_TYPE_WORD",
            13: "SOURCE_CONTENT_TYPE_IMAGE",
            14: "SOURCE_CONTENT_TYPE_DRIVE",
            16: "SOURCE_CONTENT_TYPE_CSV",
            17: "SOURCE_CONTENT_TYPE_EPUB",
        },
    ),
}

#: Enum members our client cannot express today. Each entry is a real backend
#: value that maps to "unknown" (or worse) in this client.
ENUM_GAPS: dict[str, tuple[tuple[int, str, str], ...]] = {
    "SourceStatus": (
        (0, "SOURCE_STATUS_UNSPECIFIED", "#2124 — falls back to READY, asserting health"),
        (4, "SOURCE_STATUS_PENDING_DELETION", "#2124 — falls back to READY, asserting health"),
    ),
    "ArtifactStatus": (
        (0, "ARTIFACT_STATUS_UNKNOWN", "#2127"),
        (5, "ARTIFACT_STATUS_SUGGESTED", "#2127"),
        (6, "ARTIFACT_PENDING_REVIEW", "#2127 — state discovered only in the object store"),
    ),
    "SourceType": (
        (
            0,
            "SOURCE_CONTENT_TYPE_UNKNOWN",
            "occurs live (rejected upload, failed processing) and triggers a "
            "misleading 'Consider updating notebooklm-py' prompt no upgrade fixes",
        ),
        (
            6,
            "SOURCE_CONTENT_TYPE_POWERPOINT",
            "LIVE BUG: a valid .pptx uploads, reaches READY, decodes to code 6, and "
            "falls through to SourceType.UNKNOWN with the spurious upgrade warning",
        ),
        (
            7,
            "SOURCE_CONTENT_TYPE_GOOGLE_SHEET",
            "declared but NOT emitted on the web transport — a native Google Sheet "
            "comes back as 14 (DRIVE). Do NOT add a 7 mapping on the enum dump alone.",
        ),
        (
            12,
            "SOURCE_CONTENT_TYPE_EXCEL",
            "unreachable: the upload endpoint rejects .xlsx (HTTP 400)",
        ),
        (15, "SOURCE_CONTENT_TYPE_GMAIL", "no entry point on the web batchexecute surface"),
        (18, "SOURCE_CONTENT_TYPE_GEMINI_CHAT", "originates in the Gemini app"),
        (19, "SOURCE_CONTENT_TYPE_AI_MODE_CHAT", "originates in Search AI Mode"),
        (20, "SOURCE_CONTENT_TYPE_EXPERT_INTELLIGENCE", "no entry point on this tier/surface"),
    ),
}
