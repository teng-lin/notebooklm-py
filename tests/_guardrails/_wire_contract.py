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
``docs/android/schema.proto``.

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
#: ``GET_SHARE_STATUS`` decodes the sharing service's ``GetProjectDetailsResponse``,
#: which lives in its own package rather than the ``orchestration.v1`` wire section.
SHARING = "labs_tailwind_sharing_service"
#: ``ProjectPublicSettings`` (the nested ``publicSettings`` block) is declared in
#: the shared projects package, not alongside the sharing service RPC.
SHARING_COMMON = "common.protos/projects.pb.dart"
#: Placeholder ``cls`` for constants declared at module scope rather than on a class.
MODULE_LEVEL = "<module>"
# Must include the package prefix: `tailwind_doc.pb.dart` alone also matches the
# persistence copy of the same message, which carries different tags.
DOC = "orchestration.v1/tailwind_doc.pb.dart"
# ``TextStyle`` / ``ParagraphStyle`` / ``BulletInfo`` live in the sibling
# ``_common`` file. The distinction is load-bearing, not cosmetic: the
# persistence copy of ``BulletInfo`` numbers ``nestingLevel`` 1 where the wire
# copy numbers it 3, so a missing section hint would assert against the wrong
# tags and pass for the wrong reason.
DOC_COMMON = "orchestration.v1/tailwind_doc_common.pb.dart"


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
    but records no name, so the tag is absent from ``android/schema.proto`` and
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
    Mapping(
        "sources",
        "SourceRow",
        "_DRIVE_STATUS_INNER_POS",
        "SourceSettings",
        "userDriveSourceStatus",
        section=SOURCE_SETTINGS,
        note=(
            "#2111 — the sibling of status in the same settings block. "
            "Live: 4/409 source rows carry settings=[null,2,null,3] (all "
            "Drive-backed, all DRIVE_SOURCE_STATUS_ACTIVE); the other 405 "
            "omit the slot. Read by SourceRow.drive_status."
        ),
    ),
    # ---- Source row: SourceMetadata ---------------------------------------
    Mapping(
        "sources", "SourceRow", "_META_TYPE_POS", "SourceMetadata", "originalSourceContentType"
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_META_GOOGLE_DOCS_POS",
        "SourceMetadata",
        "googleDocsMetadata",
        note=(
            "renamed from _META_BARE_URL_POS in #2113 — index 0 is "
            "googleDocsMetadata, not a bare URL. The url_allow_bare_http branch "
            "that reads it as a str is dead: real metadata[0] is null 3076/3116 "
            "and a list 40/3116, never a str. The list case is the Drive "
            "documentId block read by SourceRow.drive_document_id."
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
    # ---- Source row: the Drive documentId slot (#2113) ---------------------
    Mapping(
        "sources",
        "SourceRow",
        "_DRIVE_DOCUMENT_ID_POS",
        "GoogleDriveSourceMetadata",
        "documentId",
        note=(
            "one constant indexes BOTH Drive blocks: GoogleDocsSourceMetadata "
            "(metadata[0]) and GoogleDriveSourceMetadata (metadata[9]) each "
            "declare documentId as tag 1, so both read index 0. Asserted here "
            "against the Drive copy; the Docs copy is checked by "
            "test_google_docs_document_id_shares_the_drive_tag."
        ),
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_DRIVE_DESCRIPTOR_MIME_POS",
        "GoogleDriveSourceMetadata",
        "mimeType",
    ),
    # ---- Source row: Expert Intelligence / Play Books provenance (#2292) ---
    Mapping(
        "sources",
        "SourceRow",
        "_META_EXPERT_INTELLIGENCE_POS",
        "SourceMetadata",
        "expertIntelligenceSourceMetadata",
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_EI_CONTENT_ID_POS",
        "ExpertIntelligenceSourceMetadata",
        "contentId",
    ),
    Mapping("sources", "SourceRow", "_EI_TITLE_POS", "ExpertIntelligenceSourceMetadata", "title"),
    Mapping(
        "sources", "SourceRow", "_EI_AUTHORS_POS", "ExpertIntelligenceSourceMetadata", "authors"
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_EI_THUMBNAIL_POS",
        "ExpertIntelligenceSourceMetadata",
        "thumbnailImageUrl",
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_EI_DESCRIPTION_POS",
        "ExpertIntelligenceSourceMetadata",
        "description",
    ),
    Mapping(
        "sources",
        "SourceRow",
        "_EI_FIELD_TYPE_POS",
        "ExpertIntelligenceSourceMetadata",
        tag=7,
        note=(
            "the mobile BuilderInfo could not recover this field's name (it emits "
            "the placeholder 'fieldType'), so the tag is pinned rather than the "
            "name. Web index 6 == proto tag 7; live web capture puts the opaque "
            "double here (#2292). Read by SourceRow.expert_intelligence.field_type."
        ),
    ),
    # ---- Artifact row: Artifact -------------------------------------------
    Mapping("artifacts", "ArtifactRow", "_ID_POS", "Artifact", "artifactId"),
    Mapping("artifacts", "ArtifactRow", "_TITLE_POS", "Artifact", "title"),
    Mapping("artifacts", "ArtifactRow", "_TYPE_POS", "Artifact", "type"),
    Mapping("artifacts", "ArtifactRow", "_SOURCES_POS", "Artifact", "sources"),
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
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_LAST_MODIFIED_TIMESTAMP_POS",
        "Artifact",
        "lastModifiedTimestamp",
    ),
    Mapping("artifacts", "ArtifactRow", "_INFOGRAPHIC_METADATA_POS", "Artifact", "infographic"),
    Mapping("artifacts", "ArtifactRow", "_SLIDE_DECK_METADATA_POS", "Artifact", "slides"),
    Mapping(
        "artifacts", "ArtifactRow", "_ARTIFACT_USER_STATE_POS", "Artifact", "artifactUserState"
    ),
    Mapping("artifacts", "ArtifactRow", "_ETAG_POS", "Artifact", "etag"),
    # ---- Artifact content payloads (#2135/#2136) --------------------------
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_AUDIO_MEDIA_LIST_POS",
        "AudioOverviewArtifact",
        "mediaUrls",
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_AUDIO_DURATION_POS",
        "AudioOverviewArtifact",
        "duration",
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_VIDEO_MEDIA_LIST_POS",
        "ExplainerVideoArtifact",
        "mediaUrls",
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_VIDEO_DURATION_POS",
        "ExplainerVideoArtifact",
        "duration",
    ),
    Mapping("artifacts", "ArtifactRow", "_MEDIA_URL_POS", "MediaStreamingUrl", "url"),
    Mapping("artifacts", "ArtifactRow", "_MEDIA_KIND_POS", "MediaStreamingUrl", "type"),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_REPORT_GENERATION_OPTIONS_POS",
        "TailoredReportArtifact",
        "generationOptions",
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_REPORT_KIND_POS",
        "TailoredReportArtifactGenerationOptions",
        "type",
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_INFOGRAPHIC_ITEMS_POS",
        "InfographicArtifact",
        "infographics",
    ),
    Mapping("artifacts", "ArtifactRow", "_INFOGRAPHIC_TITLE_POS", "Infographic", "title"),
    Mapping("artifacts", "ArtifactRow", "_INFOGRAPHIC_IMAGE_POS", "Infographic", "image"),
    Mapping("artifacts", "ArtifactRow", "_SLIDE_ITEMS_POS", "SlidesArtifact", "slides"),
    Mapping("artifacts", "ArtifactRow", "_SLIDE_IMAGE_POS", "Slide", "image"),
    Mapping("artifacts", "ArtifactRow", "_IMAGE_URL_POS", "ServedImage", "url"),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_AUDIO_USER_STATE_POS",
        "ArtifactState",
        "audioOverviewState",
    ),
    Mapping("artifacts", "ArtifactRow", "_APP_USER_STATE_POS", "ArtifactState", "appArtifactState"),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_PLAYBACK_POSITION_POS",
        "AudioOverviewState",
        "playbackPosition",
    ),
    Mapping("artifacts", "ArtifactRow", "_APP_STATE_POS", "AppArtifactState", "appState"),
    # ---- Inside the AppArtifact options block (#2195) ----------------------
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_GENERATION_OPTIONS_POS",
        "AppArtifact",
        "generationOptions",
        note="the data[9][1] descent shared by variant, generation_prompt and the option pairs",
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_APP_TYPE_POS",
        "AppArtifactGenerationOptions",
        "appType",
        note=(
            "ArtifactRow.variant. AppType: 1=FLASHCARDS, 2=QUIZ, 4=MINDMAP "
            "(docs/android/enums.txt), matching the variant codes the payload "
            "builders send."
        ),
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_FLASHCARDS_OPTIONS_POS",
        "AppArtifactGenerationOptions",
        "flashcardsGenerationOptions",
        note=(
            "#2195 — live-verified: a FEWER(1)+HARD(3) flashcards request echoes "
            "back data[9][1][6] == [1, 3] while data[9][1][7] stays null."
        ),
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_QUIZ_OPTIONS_POS",
        "AppArtifactGenerationOptions",
        "quizGenerationOptions",
        note=(
            "#2195 — the quiz sibling, live-verified the same way: a "
            "FEWER(1)+HARD(3) quiz request echoes back data[9][1][7] == [1, 3]."
        ),
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_OPTION_QUANTITY_POS",
        "QuizGenerationOptions",
        "questionQuantity",
        note=(
            "one constant indexes BOTH option messages: FlashcardsGenerationOptions "
            "declares cardQuantity at the same tag 1. Asserted here against the quiz "
            "copy; the flashcards copy is checked by "
            "test_flashcards_option_pair_shares_the_quiz_tags."
        ),
    ),
    Mapping(
        "artifacts",
        "ArtifactRow",
        "_OPTION_DIFFICULTY_POS",
        "QuizGenerationOptions",
        "quizDifficulty",
        note="likewise shared with FlashcardsGenerationOptions.flashcardsDifficulty (tag 2).",
    ),
    # ---- Answer row: AnswerResponse / TailwindDoc -------------------------
    # ---- Citation + TailwindDoc tree (#2120, #2128) ------------------------
    # Every one of these was UNMAPPED or absent before #2120. Recovering the
    # ``tailwind_doc`` section of the schema is what turned the citation
    # descent from guesswork into an assertion — and it is what showed the
    # fragment's elements sit one level below ``Citation.fragment``, which was
    # the whole of the truncation bug.
    Mapping("chat", "CitationDetail", "_FRAGMENT_POS", "Citation", "fragment", section=DOC),
    Mapping(
        "chat",
        "CitationDetail",
        "_SOURCE_ID_POS",
        "Citation",
        "sourceAttribution",
        section=DOC,
    ),
    Mapping(
        "chat",
        "CitationDetail",
        "_FRAGMENT_ELEMENTS_POS",
        "TailwindDocFragment",
        "elements",
        section=DOC,
        note=(
            "the second level of the descent: Citation.fragment is a message, "
            "and stopping at it yields a 1-element wrapper — the #2120 defect"
        ),
    ),
    Mapping(
        "chat",
        "CitationDetail",
        "_FRAGMENT_RANGE_START_POS",
        "Range",
        "startIndex",
        section=DOC,
    ),
    Mapping("chat", "CitationDetail", "_FRAGMENT_RANGE_END_POS", "Range", "endIndex", section=DOC),
    Mapping("documents", "DocumentBodyRow", "_CONTENT_POS", "Body", "content", section=DOC),
    Mapping(
        "documents",
        "DocumentBodyRow",
        "_ANNOTATIONS_POS",
        "Body",
        "inlineObjectLocations",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_START_POS",
        "StructuralElement",
        "startIndex",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_END_POS",
        "StructuralElement",
        "endIndex",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_PARAGRAPH_POS",
        "StructuralElement",
        "paragraph",
        section=DOC,
    ),
    Mapping(
        "documents", "StructuralElementRow", "_TABLE_POS", "StructuralElement", "table", section=DOC
    ),
    Mapping(
        "documents", "StructuralElementRow", "_IMAGE_POS", "StructuralElement", "image", section=DOC
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_CODE_BLOCK_POS",
        "StructuralElement",
        "codeBlock",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_A2UI_BLOCK_POS",
        "StructuralElement",
        "a2uiBlock",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_THOUGHT_POS",
        "StructuralElement",
        "thought",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_FUNCTION_CALL_POS",
        "StructuralElement",
        "functionCall",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_FUNCTION_RESPONSE_POS",
        "StructuralElement",
        "functionResponse",
        section=DOC,
    ),
    Mapping(
        "documents",
        "StructuralElementRow",
        "_HORIZONTAL_RULE_POS",
        "StructuralElement",
        "horizontalRule",
        section=DOC,
    ),
    Mapping("documents", "TableRow", "_TABLE_ROWS_POS", "Table", "tableRows", section=DOC),
    Mapping("documents", "TableRow", "_ROW_START_POS", "TableRow", "startIndex", section=DOC),
    Mapping("documents", "TableRow", "_ROW_END_POS", "TableRow", "endIndex", section=DOC),
    Mapping("documents", "TableRow", "_ROW_CELLS_POS", "TableRow", "tableCells", section=DOC),
    Mapping("documents", "TableRow", "_CELL_START_POS", "TableCell", "startIndex", section=DOC),
    Mapping("documents", "TableRow", "_CELL_END_POS", "TableCell", "endIndex", section=DOC),
    Mapping("documents", "TableRow", "_CELL_CONTENT_POS", "TableCell", "content", section=DOC),
    Mapping("documents", "ParagraphRow", "_ELEMENTS_POS", "Paragraph", "elements", section=DOC),
    Mapping("documents", "ParagraphRow", "_STYLE_POS", "Paragraph", "paragraphStyle", section=DOC),
    Mapping(
        "documents", "ParagraphRow", "_BULLET_INFO_POS", "Paragraph", "bulletInfo", section=DOC
    ),
    Mapping(
        "documents",
        "ParagraphRow",
        "_NAMED_STYLE_POS",
        "ParagraphStyle",
        "namedStyleType",
        section=DOC_COMMON,
    ),
    Mapping(
        "documents",
        "ParagraphElementRow",
        "_START_POS",
        "ParagraphElement",
        "startIndex",
        section=DOC,
    ),
    Mapping(
        "documents",
        "ParagraphElementRow",
        "_END_POS",
        "ParagraphElement",
        "endIndex",
        section=DOC,
    ),
    Mapping(
        "documents",
        "ParagraphElementRow",
        "_TEXT_RUN_POS",
        "ParagraphElement",
        "textRun",
        section=DOC,
    ),
    Mapping("documents", "TextRunRow", "_CONTENT_POS", "TextRun", "content", section=DOC),
    Mapping("documents", "TextRunRow", "_STYLE_POS", "TextRun", "textStyle", section=DOC),
    Mapping("documents", "TextRunRow", "_BOLD_POS", "TextStyle", "bold", section=DOC_COMMON),
    Mapping("documents", "TextRunRow", "_ITALIC_POS", "TextStyle", "italic", section=DOC_COMMON),
    Mapping(
        "documents",
        "TextRunRow",
        "_UNDERLINE_POS",
        "TextStyle",
        "underline",
        section=DOC_COMMON,
    ),
    Mapping("documents", "TextRunRow", "_URL_POS", "TextStyle", "url", section=DOC_COMMON),
    Mapping(
        "documents",
        "BulletInfoRow",
        "_NESTING_LEVEL_POS",
        "BulletInfo",
        "nestingLevel",
        section=DOC_COMMON,
    ),
    Mapping(
        "documents",
        "AnnotationEntryRow",
        "_OBJECT_ID_POS",
        "AnnotationMapEntry",
        "objectId",
        section=DOC,
        note=(
            "objectId is tag 1 and contentRange tag 2 — the order #2120's issue "
            "body transcribed the other way round; the live capture matches the "
            "schema, and the id is what joins an annotation to its citation"
        ),
    ),
    Mapping(
        "documents",
        "AnnotationEntryRow",
        "_RANGE_POS",
        "AnnotationMapEntry",
        "contentRange",
        section=DOC,
    ),
    Mapping(
        "documents", "AnnotationEntryRow", "_OBJECT_ID_VALUE_POS", "ObjectId", "id", section=DOC
    ),
    Mapping(
        "documents", "AnnotationEntryRow", "_RANGE_START_POS", "Range", "startIndex", section=DOC
    ),
    Mapping("documents", "AnnotationEntryRow", "_RANGE_END_POS", "Range", "endIndex", section=DOC),
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
    Mapping(
        "chat",
        "AnswerRow",
        "_DOC_BODY_POS",
        "TailwindDoc",
        "body",
        section=DOC,
        note=(
            "nested inside responseDoc; the answer's own document body, whose "
            "annotation map anchors each citation to a range of the answer (#2120)"
        ),
    ),
    Mapping(
        "chat",
        "AnswerRow",
        "_ANSWER_MARKER_POS",
        "TailwindDoc",
        "type",
        section=DOC,
        note="nested inside responseDoc, not a top-level AnswerResponse index",
    ),
    Mapping(
        "chat",
        "AnswerRow",
        "_EMPTY_ANSWER_REASON_POS",
        "AnswerResponse",
        "emptyAnswerReason",
        note=(
            "the authoritative expected-empty-answer signal (UNANSWERABLE / "
            "FILTERED); read to keep the drift warning off deliberate non-answers"
        ),
    ),
    # ---- Notes: ProjectNote ------------------------------------------------
    Mapping("notes", "NoteRow", "_ID_POS", "ProjectNote", "id"),
    Mapping("notes", "NoteRow", "_CONTENT_POS", "ProjectNote", "content"),
    Mapping("notes", "NoteRow", "_INNER_CONTENT_POS", "ProjectNote", "content"),
    Mapping("notes", "NoteRow", "_INNER_TITLE_POS", "ProjectNote", "name"),
    Mapping(
        "chat",
        "StreamEnvelopeRow",
        "_IS_FINAL_RESPONSE_POS",
        "GenerateFreeFormStreamedResponse",
        "isFinalResponse",
        note=(
            "#2122 — the backend's explicit end-of-stream marker, previously "
            "unread while the parser inferred the answer with a longest-wins "
            "heuristic. Live: false on every chunk but the last and true on "
            "exactly the last, across a 5-chunk and a 6-chunk stream."
        ),
    ),
    Mapping(
        "chat",
        "StreamEnvelopeRow",
        "_NEXT_STEPS_POS",
        "GenerateFreeFormStreamedResponse",
        "nextStepSuggestions",
        note="#2119 — typed follow-up chips carried on the final live stream envelope",
    ),
    Mapping(
        "chat",
        "StreamEnvelopeRow",
        "_NEXT_STEPS_ROWS_POS",
        "NextStepSuggestions",
        "nextSteps",
        note="nested inside GenerateFreeFormStreamedResponse.nextStepSuggestions",
    ),
    Mapping("chat", "NextStepSuggestionRow", "_QUESTION_POS", "NextStep", "suggestion"),
    Mapping(
        "chat",
        "NextStepSuggestionRow",
        "_TYPE_CODE_POS",
        "NextStep",
        "suggestionType",
    ),
    # ---- Chat: ConversationTurnKey (inside AnswerResponse tag 3) -----------
    # The three slots of the key at ``answer_row[2]``. Slot 0 keeps its proto
    # name on the public type; slot 1 does NOT, because its proto name
    # contradicts every observation. See ``ConversationTurnKey``'s docstring.
    Mapping(
        "chat",
        "AnswerRow",
        "_TURN_KEY_SESSION_ID_POS",
        "ConversationTurnKey",
        "sessionId",
        note=(
            "#2122 — surfaced as ConversationTurnKey.session_id, under its proto "
            "name because the evidence about what it holds is mixed: a live "
            "two-turn probe saw the hPTbtc-resolved conversation id here, while "
            "this repo's 4 recorded chat cassettes show it differing from the "
            "recorded hPTbtc id. It is the same slot AnswerRow."
            "server_conversation_id reads, which #659 established is a "
            "per-stream id. Nothing is claimed for it"
        ),
    ),
    Mapping(
        "chat",
        "AnswerRow",
        "_TURN_KEY_TURN_ID_POS",
        "ConversationTurnKey",
        "conversationId",
        note=(
            "#2122 — surfaced as ConversationTurnKey.turn_id, NOT under its "
            "proto name: live, this slot held a DIFFERENT uuid on each of two "
            "turns of one conversation, so it identifies the turn"
        ),
    ),
    Mapping(
        "chat",
        "AnswerRow",
        "_TURN_KEY_TURN_CODE_POS",
        "ConversationTurnKey",
        "fieldType",
        note=(
            "#2122 — surfaced as ConversationTurnKey.turn_code and NOT "
            "interpreted. The extractor's `fieldType` label is a placeholder "
            "name, and the observed values (2187103311 / 3083048340 / "
            "2502166488 — one per turn, constant across that turn's chunks) are "
            "not type tags. Carried verbatim so the key can be rebuilt."
        ),
    ),
    # ---- Research: DiscoveredSource ---------------------------------------
    Mapping("research", "ResearchResultRow", "_URL_POS", "DiscoveredSource", "sourceUrl"),
    Mapping("research", "ResearchResultRow", "_TITLE_POS", "DiscoveredSource", "title"),
    Mapping(
        "research",
        "ResearchResultRow",
        "_HINT_POS",
        "DiscoveredSource",
        "hint",
        note=(
            "#2122 — the backend's one-line 'why this source' note. Live: "
            "populated on 10/10 fast-research rows; the deep-research report "
            "row leaves it null"
        ),
    ),
    Mapping("research", "ResearchResultRow", "_RESULT_TYPE_POS", "DiscoveredSource", "corpusType"),
    # ---- Notebooks: PromptSuggestion --------------------------------------
    Mapping("notebooks", "ProjectRow", "_EMOJI_POS", "Project", "emoji"),
    Mapping(
        "notebooks",
        "ProjectRow",
        "_PREMIUM_FEATURE_INFO_POS",
        "Project",
        "premiumFeatureInfo",
    ),
    Mapping(
        "notebooks",
        "ProjectRow",
        "_CHAT_SESSIONS_POS",
        "Project",
        "chatSessions",
    ),
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
        "settings",
        MODULE_LEVEL,
        "_NOTEBOOK_LIMIT_INDEX",
        "TierLimits",
        tag=2,
        note=(
            "maxProjects — name unrecovered; tag from BuilderInfo, verified live on both transports"
        ),
    ),
    Mapping(
        "settings",
        MODULE_LEVEL,
        "_SOURCE_LIMIT_INDEX",
        "TierLimits",
        tag=3,
        note="maxSourcesPerProject — name unrecovered; verified live on both transports",
    ),
    # ---- Share status: GetProjectDetailsResponse (#2130) -------------------
    # GET_SHARE_STATUS decodes the sharing service's GetProjectDetailsResponse.
    # Live shape, identical on 10/10 notebooks in a 2026-08 sweep:
    #   [[<user rows>], null, 1000, true, null, null, [3, true, true], false]
    Mapping(
        "sharing",
        "ShareStatusRow",
        "_PUBLIC_BLOCK_POS",
        "GetProjectDetailsResponse",
        "publicSettings",
        section=SHARING,
        note="the long-standing data[1] descent, now named",
    ),
    Mapping(
        "sharing",
        "ShareStatusRow",
        "_IS_PUBLIC_INNER_POS",
        "ProjectPublicSettings",
        "isPubliclyReadable",
        section=SHARING_COMMON,
        note="the inner data[1][0] flag behind ShareStatus.is_public",
    ),
    Mapping(
        "sharing",
        "ShareStatusRow",
        "_MAX_SHARE_LIMIT_POS",
        "GetProjectDetailsResponse",
        "maxIndividualsShareLimit",
        section=SHARING,
        note=(
            "#2130 — live 1000 on 10/10 notebooks sampled. Previously read by "
            "nobody and described in the parser docstring as the bare literal "
            "1000. Read by ShareStatus.max_individuals_share_limit."
        ),
    ),
    Mapping(
        "sharing",
        "ShareStatusRow",
        "_PUBLIC_SHARING_ALLOWED_POS",
        "GetProjectDetailsResponse",
        "isPublicSharingAllowed",
        section=SHARING,
        note=(
            "#2130 — live true on 10/10 notebooks sampled. The tenant/policy "
            "gate on making a notebook public. Read by "
            "ShareStatus.is_public_sharing_allowed."
        ),
    ),
    # ---- GetArtifactCustomizationChoices (sqTeoe) --------------------------
    # The recovered ``ArtifactCustomizationChoices`` declares the slide-deck
    # (tag 3) and tailored-report (tag 4) families; the audio / video families
    # at tags 1 / 2 are live-only (see UNMAPPED). Every family is a one-field
    # message whose repeated rows sit at ``family[0]``.
    Mapping(
        "customization",
        "CustomizationChoicesRow",
        "_SLIDE_DECK_POS",
        "ArtifactCustomizationChoices",
        "slidesCustomizationChoices",
    ),
    Mapping(
        "customization",
        "CustomizationChoicesRow",
        "_REPORTS_POS",
        "ArtifactCustomizationChoices",
        "tailoredReportCustomizationChoices",
    ),
    Mapping(
        "customization",
        "CustomizationChoicesRow",
        "_FAMILY_ROWS_POS",
        "SlidesCustomizationChoices",
        "types",
        note=(
            "every family container is a one-field message: SlidesCustomizationChoices.types, "
            "TailoredReportCustomizationChoices.reportTypeOptions and the live-only audio / "
            "video containers all carry their rows at tag 1"
        ),
    ),
    # Format rows: the recovered ``SlidesType`` layout. The audio and video
    # families reuse it verbatim with their own format enums (AudioFormat /
    # VideoFormat) in the code slot — live-verified against the served labels.
    Mapping("customization", "CustomizationChoiceRow", "_CODE_POS", "SlidesType", "deckType"),
    Mapping("customization", "CustomizationChoiceRow", "_TITLE_POS", "SlidesType", "title"),
    Mapping(
        "customization", "CustomizationChoiceRow", "_DESCRIPTION_POS", "SlidesType", "description"
    ),
    Mapping(
        "customization",
        "ReportPresetRow",
        "_REPORT_TYPE_POS",
        "TailoredReportTypeOption",
        "reportType",
    ),
    Mapping(
        "customization",
        "ReportPresetRow",
        "_DESCRIPTION_POS",
        "TailoredReportTypeOption",
        "reportDescription",
    ),
    Mapping(
        "customization",
        "ReportPresetRow",
        "_DIRECTIVE_POS",
        "TailoredReportTypeOption",
        "reportDirective",
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
    # NOTE: _META_GOOGLE_DOCS_POS (index 0) is a real mapping (see MAPPINGS) —
    # it is SourceMetadata.googleDocsMetadata, not a bare URL.
    Unmapped("sources", "SourceRow", "_META_TIMESTAMP_POS", _UNRECOVERED),
    Unmapped("sources", "SourceRow", "_META_YOUTUBE_POS", _UNRECOVERED),
    Unmapped("sources", "SourceRow", "_META_MIME_POS", _UNRECOVERED),
    Unmapped(
        "sources",
        "SourceRow",
        "_EI_PROVIDER_POS",
        "ExpertIntelligenceSourceMetadata declares no tag 2 in the recovered "
        "mobile schema (contentId=1, title=3, …), but the web wire carries the "
        "ContentProvider enum here (index 1 == tag 2): live web capture puts `1` "
        "(GOOGLE_PLAY_BOOKS) at this slot on every ingested Play Book (#2292). "
        "Read by SourceRow.expert_intelligence.provider.",
    ),
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
    Unmapped("artifacts", "ArtifactRow", "_MEDIA_MIME_POS", _NESTED_LOCAL),
    Unmapped(
        "artifacts",
        "ArtifactRow",
        "_INFOGRAPHIC_ALT_TEXT_POS",
        "live web field absent from the recovered mobile Infographic message",
    ),
    Unmapped(
        "artifacts",
        "ArtifactRow",
        "_INFOGRAPHIC_TEXT_POS",
        "live web field absent from the recovered mobile Infographic message",
    ),
    Unmapped("artifacts", "ArtifactRow", "_INFOGRAPHIC_CONTENT_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_INFOGRAPHIC_FIRST_CONTENT_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_INFOGRAPHIC_IMAGE_DATA_POS", _NESTED_LOCAL),
    Unmapped(
        "artifacts",
        "ArtifactRow",
        "_SLIDE_ALT_TEXT_POS",
        "live web field absent from the recovered mobile Slide message",
    ),
    Unmapped(
        "artifacts",
        "ArtifactRow",
        "_SLIDE_TEXT_POS",
        "live web field absent from the recovered mobile Slide message",
    ),
    Unmapped(
        "artifacts",
        "ArtifactRow",
        "_IMAGE_WIDTH_POS",
        "live web ServedImage extension absent from the recovered mobile schema",
    ),
    Unmapped(
        "artifacts",
        "ArtifactRow",
        "_IMAGE_HEIGHT_POS",
        "live web ServedImage extension absent from the recovered mobile schema",
    ),
    Unmapped("artifacts", "ArtifactRow", "_DURATION_SECONDS_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_DURATION_NANOS_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_SLIDE_DECK_PDF_URL_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ArtifactRow", "_SLIDE_DECK_PPTX_URL_POS", _NESTED_LOCAL),
    Unmapped("artifacts", "ReportSuggestionRow", "_TITLE_POS", _SHAPE_UNKNOWN),
    Unmapped("artifacts", "ReportSuggestionRow", "_DESCRIPTION_POS", _SHAPE_UNKNOWN),
    Unmapped("artifacts", "ReportSuggestionRow", "_PROMPT_POS", _SHAPE_UNKNOWN),
    Unmapped("artifacts", "ReportSuggestionRow", "_AUDIENCE_LEVEL_POS", _SHAPE_UNKNOWN),
    # collections.py — account-level web collection tuples are not present in
    # the recovered mobile schema.
    Unmapped("collections", "CollectionRow", "_NAME_POS", _SHAPE_UNKNOWN),
    Unmapped("collections", "CollectionRow", "_MEMBERS_POS", _SHAPE_UNKNOWN),
    Unmapped("collections", "CollectionRow", "_ID_POS", _SHAPE_UNKNOWN),
    Unmapped("collections", "CollectionRow", "_EMOJI_POS", _SHAPE_UNKNOWN),
    # chat.py
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
        "StreamFrameRow",
        "_TERMINAL_SEQUENCE_POS",
        "streamed frame envelope terminal-sequence slot",
    ),
    Unmapped(
        "chat",
        "ErrorPayloadRow",
        "_STATUS_POS",
        "google.rpc.Status envelope, not a Tailwind message",
    ),
    Unmapped(
        "chat",
        "ErrorPayloadRow",
        "_MESSAGE_POS",
        "google.rpc.Status.message (tag 2 -> index 1) — a PUBLIC google/rpc/status.proto "
        "field, not a Tailwind one, so android/schema.proto cannot check it. Its two "
        "siblings in the same envelope ARE live-confirmed (code at index 0: [3] / [5] "
        "live from CREATE_ARTIFACT 2026-08-13, [13] in notebooks_remove_from_recent.yaml; "
        "details at index 2: the recorded UserDisplayableError block). The message slot "
        "itself has NEVER been observed populated — see #2188",
    ),
    Unmapped("chat", "ErrorPayloadRow", "_ENTRIES_POS", "google.rpc.Status envelope"),
    Unmapped("chat", "CitationRow", "_CHUNK_BLOCK_POS", _NESTED_LOCAL),
    Unmapped("chat", "CitationRow", "_DETAIL_POS", _NESTED_LOCAL),
    Unmapped(
        "chat",
        "CitationDetail",
        "_SCORE_POS",
        "reads Citation tag 3, which the recovered schema does not name",
    ),
    # notes.py
    Unmapped("notes", "NoteRow", "_STATUS_POS", "reads the NoteOrStatus wrapper, not ProjectNote"),
    Unmapped("notes", "NoteRow", "_INNER_META_POS", _NESTED_LOCAL),
    Unmapped("notes", "NoteRow", "_META_TIMESTAMP_POS", _NESTED_LOCAL),
    Unmapped("notes", "NoteRow", "_TS_SECONDS_POS", _NESTED_LOCAL),
    # research.py
    Unmapped("research", "ResearchTaskRow", "_ID_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskRow", "_INFO_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskRow", "_TS_SECONDS_POS", _NESTED_LOCAL),
    Unmapped("research", "ResearchTaskInfoRow", "_QUERY_TEXT_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskInfoRow", "_QUERY_SOURCE_TYPE_POS", _SHAPE_UNKNOWN),
    Unmapped("research", "ResearchTaskInfoRow", "_SOURCES_POS", _NESTED_LOCAL),
    Unmapped("research", "ResearchTaskInfoRow", "_SUMMARY_POS", _NESTED_LOCAL),
    Unmapped("research", "ResearchResultRow", "_CONTENT_TEXT_POS", _NESTED_LOCAL),
    Unmapped("research", "ResearchResultRow", "_CONTENT_KIND_POS", _NESTED_LOCAL),
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
    Unmapped("mind_maps", MODULE_LEVEL, "_CREATE_ARTIFACT_ENVELOPE_POS", _ENVELOPE),
    Unmapped("mind_maps", MODULE_LEVEL, "_CREATE_ARTIFACT_ID_POS", _NESTED_LOCAL),
    Unmapped("artifacts", MODULE_LEVEL, "_INTERACTIVE_TREE_LEAF_POS", _NESTED_LOCAL),
    Unmapped(
        "chat",
        MODULE_LEVEL,
        "_CHAT_SETTINGS_POS",
        "reads Project tag 8 (the chat persona/config — see #2123), which the "
        f"extractor did not recover a name for. {_UNRECOVERED}",
    ),
    Unmapped(
        "settings",
        MODULE_LEVEL,
        "_TIER_INDEX",
        "reads TierLimits tag 5 (subscription tier). The mobile BuilderInfo "
        "registers only tags 2-4, so tag 5 is addUnused and cannot be pinned "
        "against the proto — but the wire carries 5 elements and the live value "
        "(1=free / 2=Pro) matches _types/common.py on both transports.",
    ),
    # sharing.py (#2130)
    Unmapped(
        "sharing",
        "ShareStatusRow",
        "_USERS_POS",
        f"{_NO_SUCH_FIELD} the web GET_SHARE_STATUS response carries the shared-user "
        "rows at index 0 (proto tag 1), but the recovered mobile "
        "GetProjectDetailsResponse declares only tags 2-4 — it has no tag 1 at all. "
        "The slot is unambiguous live (every row is [email, permission, [], "
        "[name, avatar]]) and has been decoded correctly since the first release, "
        "so this is a naming gap in the mobile schema, not a suspect read. "
        "Recorded honestly rather than mapped to a field that does not exist.",
    ),
    # Nested web shared-user rows are likewise absent from the recovered
    # GetProjectDetailsResponse schema, so record their established positions
    # without inventing protobuf field names.
    Unmapped("sharing", "SharedUserRow", "_EMAIL_POS", _SHAPE_UNKNOWN),
    Unmapped("sharing", "SharedUserRow", "_PERMISSION_POS", _SHAPE_UNKNOWN),
    Unmapped("sharing", "SharedUserRow", "_USER_INFO_POS", _SHAPE_UNKNOWN),
    Unmapped("sharing", "SharedUserRow", "_DISPLAY_NAME_POS", _NESTED_LOCAL),
    Unmapped("sharing", "SharedUserRow", "_AVATAR_URL_POS", _NESTED_LOCAL),
    # customization.py — GetArtifactCustomizationChoices (sqTeoe). The APK
    # schema declares only the slide-deck / report families; the audio and
    # video families are live-observed at tags 1 / 2 on both front doors
    # (2026-09-01, #2283) and carry the AudioFormat / VideoFormat codes.
    Unmapped("customization", MODULE_LEVEL, "_CHOICES_ENVELOPE_POS", _ENVELOPE),
    Unmapped(
        "customization",
        "CustomizationChoicesRow",
        "_AUDIO_POS",
        "live-only ArtifactCustomizationChoices tag 1 (audio formats); the APK schema "
        "declares only tags 3 and 4",
    ),
    Unmapped(
        "customization",
        "CustomizationChoicesRow",
        "_VIDEO_POS",
        "live-only ArtifactCustomizationChoices tag 2 (video formats); the APK schema "
        "declares only tags 3 and 4",
    ),
    # transfers.py — the #2283 mapping replies (CopySourcesAsync,
    # CopyArtifactsAsync, AddSourcesAsync). None of these response messages
    # exist in the recovered APK schema (the app never calls them); the
    # positions are web-derived and cross-checked tag-for-tag against the live
    # Android gRPC replies (docs/android/copy-append-suggestion-evidence.md).
    Unmapped("transfers", MODULE_LEVEL, "_MAPPING_ROWS_POS", _ENVELOPE),
    Unmapped("transfers", "CopiedSourceRow", "_ORIGINAL_POS", _SHAPE_UNKNOWN),
    Unmapped("transfers", "CopiedSourceRow", "_SOURCE_POS", _SHAPE_UNKNOWN),
    Unmapped("transfers", "CopiedArtifactRow", "_ORIGINAL_POS", _SHAPE_UNKNOWN),
    Unmapped("transfers", "CopiedArtifactRow", "_ARTIFACT_POS", _SHAPE_UNKNOWN),
    Unmapped("transfers", "SourceAckRow", "_SOURCE_POS", _SHAPE_UNKNOWN),
    Unmapped("transfers", "SourceAckRow", "_STATUS_POS", _SHAPE_UNKNOWN),
    Unmapped("transfers", "AddSourcesAsyncResponseRow", "_SOURCES_POS", _SHAPE_UNKNOWN),
    Unmapped("transfers", "AddSourcesAsyncResponseRow", "_ACKS_POS", _SHAPE_UNKNOWN),
    # notebooks.py — NextStepSuggestions (OcvKNc): the reply IS one
    # NextStepSuggestions message, so its rows are the first element.
    Unmapped("notebooks", MODULE_LEVEL, "_SUGGEST_NEXT_STEPS_ROWS_POS", _ENVELOPE),
)

#: ``GET_SHARE_STATUS`` slots that are POPULATED on every live response and
#: deliberately left undecoded, so a future reader does not mistake the silence
#: for "we checked and there is nothing there" (#2130).
#:
#: Not :data:`UNMAPPED` — that registry is keyed by an existing constant, and the
#: whole point here is that no constant exists. Indices 4 and 5 are omitted: they
#: are ``null`` on every row observed, so there is nothing to explain.
UNREAD_SHARE_STATUS_SLOTS: dict[int, str] = {
    6: (
        "proto tag 7 — live [3, true, true] on 10/10 notebooks sampled 2026-08. "
        "GetProjectDetailsResponse declares no tag 7, so nothing in the recovered "
        "schema names it and its semantics are unresolved. Surfacing it would mean "
        "inventing a field name, which is the exact defect class this registry "
        "exists to catch, so #2130 left it undecoded on purpose."
    ),
    7: (
        "proto tag 8 — live false on 10/10 notebooks sampled 2026-08. Unnamed in "
        "the recovered schema for the same reason as tag 7, and a lone constant "
        "false carries no signal a caller could act on. Left undecoded."
    ),
}


PINNED: tuple[Pinned, ...] = (
    Pinned(
        "chunks",
        "RelevantChunkSourceRow",
        "_SOURCE_ID_POS",
        0,
        "SourceRelevantChunks tag 1 — source id string",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkSourceRow",
        "_CHUNKS_POS",
        1,
        "SourceRelevantChunks tag 2 — repeated relevant chunks",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_CONTENT_POS",
        0,
        "RelevantChunk tag 1 — content wrapper",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_RANK_POS",
        1,
        "RelevantChunk tag 2 — global relevance rank",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_SPANS_POS",
        2,
        "RelevantChunk tag 3 — repeated source-relative spans",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_TEXT_POS",
        0,
        "RelevantChunkContent tag 1 — text wrapper",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_PARTS_POS",
        0,
        "RelevantChunkText tag 1 — repeated text parts",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_FIRST_SPAN_POS",
        0,
        "first element of the RelevantChunk.spans repeated field",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_SPAN_START_POS",
        1,
        "RelevantChunkSpan tag 2 — source-relative start offset",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "chunks",
        "RelevantChunkRow",
        "_SPAN_END_POS",
        2,
        "RelevantChunkSpan tag 3 — source-relative end offset",
        "docs/android/source-search-evidence.md#wire-layout: Web and Android live probes",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_DOWNLOAD_URL_POS",
        5,
        "Source tag 6 — direct download URL for the original uploaded file",
        "#2112: populated on 41/409 live source rows, always as a "
        "contribution.usercontent.google.com/download URL; independently rechecked "
        "on 27/305 rows across 12 notebooks",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_VIEWER_URL_POS",
        6,
        "Source tag 7 — Drive viewer URL for the original uploaded file",
        "#2112: populated alongside tag 6 on 41/409 live source rows, always as a "
        "drive.google.com/viewer/upload URL; independently rechecked on 27/305 rows",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_CONTENT_DESCRIPTOR_POS",
        7,
        "Source tag 8 — original-content blob descriptor",
        "#2112: populated alongside tags 6/7 on 41/409 live rows with blobref, MIME, "
        "and opaque token payload; independently rechecked on 27/305 rows",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_CONTENT_DESCRIPTOR_MIME_POS",
        2,
        "Source tag-8 blob descriptor index 2 — true content MIME",
        "#2112: non-null MIME (including text/markdown) on every one of the 41 rows "
        "whose Source tag-8 descriptor was populated",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_META_WORD_COUNT_POS",
        1,
        "SourceMetadata tag 2 — inferred source word/size count",
        "#2114: populated on 409/409 sampled rows with values such as 1498, 4048, "
        "and 116962; the word-count meaning is inferred from values and the confirmed "
        "per-source word limit, not a recovered proto name",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_META_REVISION_POS",
        3,
        "SourceMetadata tag 4 — inferred [revision id, timestamp] handle",
        "#2114: populated on 409/409 sampled rows as [uuid, [seconds, nanos]]; the "
        "revision meaning is inferred because the mobile schema marks the slot unused",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_META_LAST_MODIFIED_POS",
        14,
        "SourceMetadata tag 15 — inferred last-modified timestamp",
        "#2114: populated on 409/409 sampled rows as [seconds, nanos]; the temporal "
        "meaning is inferred from live values rather than a recovered proto name",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_REVISION_ID_POS",
        0,
        "SourceMetadata tag-4 revision handle index 0 — opaque revision UUID",
        "#2114: 409/409 live revision handles used [uuid, [seconds, nanos]]",
    ),
    Pinned(
        "sources",
        "SourceRow",
        "_REVISION_TIMESTAMP_POS",
        1,
        "SourceMetadata tag-4 revision handle index 1 — revision timestamp",
        "#2114: 409/409 live revision handles used [uuid, [seconds, nanos]]",
    ),
    Pinned(
        "chat",
        "CitationDetail",
        "_FRAGMENT_RANGE_POS",
        3,
        "Citation tag 4 — the cited fragment's SOURCE-side character range",
        "live-confirmed on two independent captures: for every citation the value "
        "equals the union of that fragment's own element ranges, and on a "
        "536-character answer one citation reported [1130, 1695] — so it cannot be "
        "the answer-text range this client documented it as until #2120",
    ),
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
    Pinned(
        "research",
        "ResearchTaskRow",
        "_UPDATED_AT_POS",
        2,
        "POLL_RESEARCH task row — last-update time, [seconds, nanos]",
        "#2122 live: polled one run twice, 7.6s apart; this slot advanced with "
        "the second poll while _CREATED_AT_POS held the first poll's value. "
        "Reproduced on two accounts. Corroborated by the repo's cassettes: 9/9 "
        "task rows, advanced across all 3 within-cassette repeated-row "
        "transitions. #2122's issue text labels these two slots the other way "
        "round; the wire does not",
    ),
    Pinned(
        "research",
        "ResearchTaskRow",
        "_CREATED_AT_POS",
        3,
        "POLL_RESEARCH task row — creation time, [seconds, nanos]",
        "#2122 live: constant across both polls of one run on two accounts, "
        "and equal to _UPDATED_AT_POS on the first poll. Cassettes: constant "
        "across all 4 repeated-row transitions in 9/9 rows",
    ),
    Pinned(
        "research",
        "ResearchTaskRow",
        "_ACCOUNT_ID_POS",
        4,
        "POLL_RESEARCH task row — opaque account id owning the run",
        "#2122 live: '400237754469' for one account and '838504205497' for a "
        "second, each constant across every task and poll of that account. Two "
        "accounts with two distinct stable values is what upgrades this from "
        "'a constant' (all of the repo's cassettes carry the first value) to "
        "'account-scoped'. Whether it names the run's starter or the notebook's "
        "owner is NOT established — both were the same account in both probes",
    ),
    Pinned(
        "research",
        "ResearchTaskInfoRow",
        "_DISCOVERY_MODE_POS",
        2,
        "POLL_RESEARCH task_info — DiscoveryMode the run is executing under",
        "Two-sided: it echoes the value the START_*_RESEARCH params carry at "
        "the same enum. Live #2122: 1 (DEFAULT_LLM_SEARCH) on every fast run, "
        "on two accounts; cassettes: 1 on 6/6 fast rows and 5 (DEEP_RESEARCH) "
        "on 3/3 deep rows",
    ),
    Pinned(
        "research",
        "ResearchResultRow",
        "_CONTENT_BLOCK_POS",
        6,
        "DiscoveredSource tag 7 — typed content block",
        "Two deep-research captures 14 months apart: report rows carry kind 3 with "
        "markdown at block[0]; 62/62 web rows carry kind 1/2 snippets at block[2]",
    ),
    Pinned(
        "research",
        "ResearchResultRow",
        "_SOURCE_ORDINAL_POS",
        8,
        "DiscoveredSource tag 9 — per-task ordinal for a discovered source",
        "Issue #2141 live capture: 41/63 rows (all kind-1) carried integer values "
        "1-41, i.e. a bijection onto 1..N. Whether that ordinal equals the "
        "report's own citation numbering is NOT established: "
        "tests/cassettes/web/research_deep_poll_long.yaml carries 24 such ordinals "
        "and its report contains no [cite: N] markers at all, so the mapping "
        "recorded here is the ordinal itself, not a marker resolution table",
    ),
)


#: Client enums checked against the recovered backend enums, as
#: ``{our qualified name: (backend enum name, {our_value: BACKEND_MEMBER})}``.
#: Only members we actually claim are listed; the test reports backend members we
#: do not model as informational, not as failures.
ENUM_BINDINGS: dict[str, tuple[str, dict[int, str]]] = {
    "ArtifactTypeCode": (
        "ArtifactType",
        {
            1: "ARTIFACT_TYPE_AUDIO_OVERVIEW",
            2: "ARTIFACT_TYPE_TAILORED_REPORT",
            3: "ARTIFACT_TYPE_EXPLAINER_VIDEO",
            4: "ARTIFACT_TYPE_APP",
            5: "ARTIFACT_TYPE_MINDMAP",
            6: "ARTIFACT_TYPE_FANTASY_MAP",
            7: "ARTIFACT_TYPE_INFOGRAPHIC",
            8: "ARTIFACT_TYPE_SLIDES",
            9: "ARTIFACT_TYPE_TABLE",
            10: "ARTIFACT_TYPE_FILE",
        },
    ),
    "MagicArtifactType": (
        "MagicArtifactType",
        {
            0: "MAGIC_ARTIFACT_TYPE_UNSPECIFIED",
            1: "MINDMAP",
            2: "AUDIO_OVERVIEW",
            3: "VIDEO_OVERVIEW",
            4: "NOTE",
            5: "TABLE",
            6: "LINE_CHART",
            7: "FLASHCARDS",
            8: "REPORT",
            9: "CONVERSATIONAL_TEXT_CHIP",
            10: "VIDEO_OVERVIEW_TEXT_CHIP",
            11: "AUDIO_OVERVIEW_TEXT_CHIP",
            12: "REPORT_TEXT_CHIP",
            13: "FLASHCARDS_TEXT_CHIP",
            14: "QUIZ_TEXT_CHIP",
            15: "SOURCE_DISCOVERY_TEXT_CHIP",
        },
    ),
    "SourceStatus": (
        "SourceStatus",
        {
            1: "SOURCE_STATUS_PENDING",
            2: "SOURCE_STATUS_COMPLETE",
            3: "SOURCE_STATUS_ERROR",
            5: "SOURCE_STATUS_TENTATIVE",
        },
    ),
    # rpc/types.py::DriveSourceStatus (#2111). Only ACTIVE has been observed on
    # the wire; the rest are bound from the recovered backend enum, which is
    # exactly what this table is for — the binding is the evidence, not a live
    # sighting. UNKNOWN(-1) is a client sentinel, declared in
    # ``_CLIENT_SYNTHETIC_VALUES``.
    "DriveSourceStatus": (
        "UserDriveSourceStatus",
        {
            1: "DRIVE_SOURCE_STATUS_INACCESSIBLE",
            2: "DRIVE_SOURCE_STATUS_SYNCING",
            3: "DRIVE_SOURCE_STATUS_ACTIVE",
            4: "DRIVE_SOURCE_STATUS_DELETED",
            5: "DRIVE_SOURCE_STATUS_GEN_AI_ACCESS_DENIED",
        },
    ),
    # rpc/types.py::DiscoveryMode (#2122). 1 and 5 are live-observed on both
    # the request and the response side; the rest are bound from the recovered
    # backend enum. UNKNOWN(-1) is a client sentinel, declared in
    # ``_CLIENT_SYNTHETIC_VALUES``; UNSPECIFIED(0) is deliberately unmodelled
    # and declared in ``ENUM_GAPS``.
    "DiscoveryMode": (
        "DiscoveryMode",
        {
            1: "DEFAULT_LLM_SEARCH",
            2: "RAW_SEARCH",
            3: "CURIOUS_SEARCH",
            4: "CURIOUS_RAW_SEARCH",
            5: "DEEP_RESEARCH",
            6: "LITE_LLM_SEARCH",
        },
    ),
    "ArtifactStatus": (
        "ArtifactStatus",
        {
            0: "ARTIFACT_STATUS_UNKNOWN",
            1: "ARTIFACT_STATUS_INITIALIZED",
            2: "ARTIFACT_STATUS_PROCESSING",
            3: "ARTIFACT_STATUS_READY",
            4: "ARTIFACT_STATUS_FAILED",
            5: "ARTIFACT_STATUS_SUGGESTED",
            6: "ARTIFACT_PENDING_REVIEW",
        },
    ),
    # rpc/types.py::QuizQuantity. Shared by quiz AND flashcards; the two backend
    # enums (``QuizGenerationOptions.QuestionQuantity`` and
    # ``FlashcardsGenerationOptions.CardQuantity``) declare identical values, so
    # binding to either one gates both. #2117 landed precisely because these
    # values were pinned to a snapshot without ever being bound to the backend:
    # ``MORE`` sat at 2 as a documented "API limitation" while the backend has
    # always declared it as 3.
    "QuizQuantity": (
        "QuizGenerationOptions_QuestionQuantity",
        {
            1: "QUESTION_QUANTITY_FEWER",
            2: "QUESTION_QUANTITY_STANDARD",
            3: "QUESTION_QUANTITY_MORE",
        },
    ),
    # rpc/types.py::QuizDifficulty, the sibling of the pair above.
    "QuizDifficulty": (
        "QuizGenerationOptions_QuizDifficulty",
        {
            1: "QUIZ_DIFFICULTY_EASY",
            2: "QUIZ_DIFFICULTY_MEDIUM",
            3: "QUIZ_DIFFICULTY_HARD",
        },
    ),
    # _types/sources.py::_SOURCE_TYPE_CODE_MAP
    "SourceType": (
        "OriginalSourceContentType",
        {
            0: "SOURCE_CONTENT_TYPE_UNKNOWN",
            1: "SOURCE_CONTENT_TYPE_GOOGLE_DOC",
            2: "SOURCE_CONTENT_TYPE_GOOGLE_SLIDES",
            3: "SOURCE_CONTENT_TYPE_PDF",
            4: "SOURCE_CONTENT_TYPE_TEXT",
            5: "SOURCE_CONTENT_TYPE_URL",
            6: "SOURCE_CONTENT_TYPE_POWERPOINT",
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
    "DriveSourceStatus": (
        (
            0,
            "DRIVE_SOURCE_STATUS_UNSPECIFIED",
            "#2111 — deliberately unmodelled. It means 'no claim', which is what "
            "an absent slot already means, so SourceRow.drive_status normalizes "
            "an explicit 0 to None rather than giving one state two "
            "representations. proto3 omits the zero default, so the wire almost "
            "never carries it in the first place.",
        ),
    ),
    "DiscoveryMode": (
        (
            0,
            "DISCOVERY_MODE_UNSPECIFIED",
            "#2122 — deliberately unmodelled, for the same reason as "
            "DRIVE_SOURCE_STATUS_UNSPECIFIED above: it means 'no claim', which "
            "is what an absent slot already means, so "
            "ResearchTaskInfoRow.discovery_mode normalizes an explicit 0 to "
            "None rather than giving one state two representations.",
        ),
    ),
    "SourceStatus": (
        (0, "SOURCE_STATUS_UNSPECIFIED", "#2124 — fails closed as UNKNOWN"),
        (4, "SOURCE_STATUS_PENDING_DELETION", "#2124 — fails closed as UNKNOWN"),
    ),
    "SourceType": (
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
