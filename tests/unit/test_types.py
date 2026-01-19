"""Unit tests for types module dataclasses and parsing."""

import pytest

from notebooklm.types import (
    Artifact,
    AskResult,
    ChatMode,
    ChatReference,
    ConversationTurn,
    GenerationStatus,
    Note,
    Notebook,
    NotebookDescription,
    ReportSuggestion,
    Source,
    SourceFulltext,
)


class TestNotebook:
    def test_from_api_response_basic(self):
        """Test parsing basic notebook data."""
        data = ["My Notebook", [], "nb_123", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.id == "nb_123"
        assert notebook.title == "My Notebook"
        assert notebook.is_owner is True

    def test_from_api_response_with_timestamp(self):
        """Test parsing notebook with timestamp."""
        ts = 1704067200  # 2024-01-01 00:00:00 UTC
        data = [
            "Timestamped Notebook",
            [],
            "nb_456",
            "📘",
            None,
            [None, None, None, None, None, [ts, 0]],
        ]
        notebook = Notebook.from_api_response(data)

        assert notebook.id == "nb_456"
        assert notebook.created_at is not None
        # Check timestamp value rather than year (timezone-independent)
        assert notebook.created_at.timestamp() == ts

    def test_from_api_response_strips_thought_prefix(self):
        """Test that 'thought\\n' prefix is stripped from title."""
        data = ["thought\nActual Title", [], "nb_789", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.title == "Actual Title"

    def test_from_api_response_shared_notebook(self):
        """Test parsing shared notebook (is_owner=False)."""
        data = [
            "Shared Notebook",
            [],
            "nb_shared",
            "📓",
            None,
            [None, True],  # data[5][1] = True means shared
        ]
        notebook = Notebook.from_api_response(data)

        assert notebook.is_owner is False

    def test_from_api_response_empty_data(self):
        """Test parsing with minimal data."""
        data = []
        notebook = Notebook.from_api_response(data)

        assert notebook.id == ""
        assert notebook.title == ""
        assert notebook.is_owner is True

    def test_from_api_response_invalid_timestamp(self):
        """Test parsing with invalid timestamp data."""
        data = [
            "Notebook",
            [],
            "nb_123",
            "📓",
            None,
            [None, None, None, None, None, ["invalid", 0]],
        ]
        notebook = Notebook.from_api_response(data)

        assert notebook.created_at is None

    def test_from_api_response_non_string_title(self):
        """Test parsing when title is not a string."""
        data = [123, [], "nb_123", "📓"]
        notebook = Notebook.from_api_response(data)

        assert notebook.title == ""


class TestSource:
    def test_from_api_response_simple_format(self):
        """Test parsing simple flat format."""
        data = ["src_123", "Source Title"]
        source = Source.from_api_response(data)

        assert source.id == "src_123"
        assert source.title == "Source Title"
        assert source.source_type == "unknown"

    def test_from_api_response_nested_format(self):
        """Test parsing medium nested format."""
        data = [
            [
                ["src_456"],
                "Nested Source",
                [None, None, None, None, None, None, None, ["https://example.com"]],
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_456"
        assert source.title == "Nested Source"
        # URL extraction depends on nesting level - verify ID and title parsed correctly

    def test_from_api_response_deeply_nested(self):
        """Test parsing deeply nested format."""
        data = [
            [
                [
                    ["src_789"],
                    "Deep Source",
                    [None, None, None, None, None, None, None, ["https://deep.example.com"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.id == "src_789"
        assert source.title == "Deep Source"
        assert source.url == "https://deep.example.com"

    def test_from_api_response_youtube_source(self):
        """Test that YouTube sources are parsed with type code 9."""
        data = [
            [
                [
                    ["src_yt"],
                    "YouTube Video",
                    [None, None, None, None, 9, None, None, ["https://youtube.com/watch?v=abc"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.source_type == "youtube"
        assert source.source_type_code == 9

    def test_from_api_response_web_page_source(self):
        """Test that web page sources are parsed with type code 5."""
        data = [
            [
                [
                    ["src_web"],
                    "Web Article",
                    [None, None, None, None, 5, None, None, ["https://example.com/article"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        assert source.source_type == "web_page"
        assert source.source_type_code == 5

    @pytest.mark.parametrize(
        "type_code,expected_type",
        [
            (1, "google_docs"),
            (2, "google_other"),
            (3, "pdf"),
            (4, "pasted_text"),
            (5, "web_page"),
            (8, "markdown"),
            (9, "youtube"),
            (10, "media"),
            (11, "docx"),
            (13, "image"),
            (14, "google_spreadsheet"),
            (16, "csv"),
        ],
    )
    def test_from_api_response_source_type_codes(self, type_code, expected_type):
        """Test that source type codes are correctly mapped to type strings."""
        data = [
            [
                [
                    ["src_test"],
                    "Test Source",
                    [None, None, None, None, type_code, None, None, ["https://example.com"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)
        assert source.source_type == expected_type
        assert source.source_type_code == type_code

    def test_from_api_response_empty_data_raises(self):
        """Test that empty data raises ValueError."""
        with pytest.raises(ValueError, match="Invalid source data"):
            Source.from_api_response([])

    def test_from_api_response_none_raises(self):
        """Test that None raises ValueError."""
        with pytest.raises(ValueError, match="Invalid source data"):
            Source.from_api_response(None)


class TestSourceTypeBreakingChanges:
    """Test breaking changes in v0.3.0 source_type strings."""

    def test_web_page_replaces_url(self):
        """Test that URL sources now have type 'web_page' not 'url'."""
        data = [
            [
                [
                    ["src_web"],
                    "Title",
                    [None, None, None, None, 5, None, None, ["https://example.com"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)
        assert source.source_type == "web_page"  # NOT "url"
        assert source.source_type_code == 5

    def test_markdown_replaces_generated(self):
        """Test that generated text now has type 'markdown' not 'generated'."""
        data = [[[["src_md"], "Title", [None, None, None, None, 8, None, None, []]]]]
        source = Source.from_api_response(data)
        assert source.source_type == "markdown"  # NOT "generated"
        assert source.source_type_code == 8

    def test_docx_replaces_text(self):
        """Test that DOCX uploads now have type 'docx' not 'text'."""
        data = [[[["src_docx"], "Title", [None, None, None, None, 11, None, None, []]]]]
        source = Source.from_api_response(data)
        assert source.source_type == "docx"  # NOT "text"
        assert source.source_type_code == 11

    def test_google_spreadsheet_replaces_spreadsheet(self):
        """Test that spreadsheet now has type 'google_spreadsheet'."""
        data = [[[["src_sheet"], "Title", [None, None, None, None, 14, None, None, []]]]]
        source = Source.from_api_response(data)
        assert source.source_type == "google_spreadsheet"  # NOT "spreadsheet"
        assert source.source_type_code == 14

    def test_csv_type_added(self):
        """Test that CSV type is now available."""
        data = [[[["src_csv"], "Title", [None, None, None, None, 16, None, None, []]]]]
        source = Source.from_api_response(data)
        assert source.source_type == "csv"
        assert source.source_type_code == 16

    def test_migration_path_with_type_code(self):
        """Test documented migration path using source_type_code."""
        from notebooklm.rpc.types import SourceType

        data = [
            [
                [
                    ["src_web"],
                    "Title",
                    [None, None, None, None, 5, None, None, ["https://example.com"]],
                ]
            ]
        ]
        source = Source.from_api_response(data)

        # Old way (broken in 0.3.0):
        # if source.source_type == "url": ...  # This breaks!

        # New way (stable API):
        assert source.source_type_code == SourceType.WEB_PAGE  # This works!
        assert source.source_type_code == 5

    def test_source_type_code_none_handling(self):
        """Test parsing source without type code metadata."""
        # Minimal data without type code
        data = [[[["src_no_type"], "Title", []]]]
        source = Source.from_api_response(data)
        assert source.id == "src_no_type"
        assert source.source_type_code is None
        assert source.source_type == "unknown"

    def test_default_value_changed_to_unknown(self):
        """Test default source_type changed from 'text' to 'unknown'."""
        # Simple flat format (no type code)
        data = ["src_123", "Source Title"]
        source = Source.from_api_response(data)
        assert source.source_type == "unknown"  # NOT "text"


class TestArtifact:
    def test_from_api_response_basic(self):
        """Test parsing basic artifact data."""
        data = ["art_123", "Audio Overview", 1, None, 3]
        artifact = Artifact.from_api_response(data)

        assert artifact.id == "art_123"
        assert artifact.title == "Audio Overview"
        assert artifact.artifact_type == 1
        assert artifact.status == 3

    def test_from_api_response_with_timestamp(self):
        """Test parsing artifact with timestamp."""
        ts = 1704067200
        data = [
            "art_123",
            "Audio",
            1,
            None,
            3,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            None,
            [ts],
        ]
        artifact = Artifact.from_api_response(data)

        assert artifact.created_at is not None
        assert artifact.created_at.timestamp() == ts

    def test_from_api_response_with_variant(self):
        """Test parsing artifact with variant code (quiz/flashcards)."""
        data = ["art_quiz", "Quiz", 4, None, 3, None, None, None, None, [None, [2]]]
        artifact = Artifact.from_api_response(data)

        assert artifact.variant == 2
        assert artifact.is_quiz is True
        assert artifact.is_flashcards is False

    def test_from_api_response_flashcards_variant(self):
        """Test parsing flashcards artifact."""
        data = ["art_fc", "Flashcards", 4, None, 3, None, None, None, None, [None, [1]]]
        artifact = Artifact.from_api_response(data)

        assert artifact.variant == 1
        assert artifact.is_flashcards is True
        assert artifact.is_quiz is False

    def test_is_completed_property(self):
        """Test is_completed property."""
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])
        processing = Artifact.from_api_response(["id", "title", 1, None, 1])

        assert completed.is_completed is True
        assert processing.is_completed is False

    def test_is_processing_property(self):
        """Test is_processing property."""
        processing = Artifact.from_api_response(["id", "title", 1, None, 1])
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])

        assert processing.is_processing is True
        assert completed.is_processing is False

    def test_is_pending_property(self):
        """Test is_pending property for status=2 (transitional state)."""
        pending = Artifact.from_api_response(["id", "title", 1, None, 2])
        processing = Artifact.from_api_response(["id", "title", 1, None, 1])
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])

        assert pending.is_pending is True
        assert processing.is_pending is False
        assert completed.is_pending is False

    def test_status_str_property(self):
        """Test status_str property returns correct human-readable strings."""
        processing = Artifact.from_api_response(["id", "title", 1, None, 1])
        pending = Artifact.from_api_response(["id", "title", 1, None, 2])
        completed = Artifact.from_api_response(["id", "title", 1, None, 3])
        unknown = Artifact.from_api_response(["id", "title", 1, None, 99])

        assert processing.status_str == "in_progress"
        assert pending.status_str == "pending"
        assert completed.status_str == "completed"
        assert unknown.status_str == "unknown"

    def test_report_subtype_briefing_doc(self):
        """Test report_subtype for briefing doc."""
        artifact = Artifact.from_api_response(["id", "Briefing Doc: Topic", 2, None, 3])

        assert artifact.report_subtype == "briefing_doc"

    def test_report_subtype_study_guide(self):
        """Test report_subtype for study guide."""
        artifact = Artifact.from_api_response(["id", "Study Guide: Topic", 2, None, 3])

        assert artifact.report_subtype == "study_guide"

    def test_report_subtype_blog_post(self):
        """Test report_subtype for blog post."""
        artifact = Artifact.from_api_response(["id", "Blog Post: Topic", 2, None, 3])

        assert artifact.report_subtype == "blog_post"

    def test_report_subtype_generic(self):
        """Test report_subtype for generic report."""
        artifact = Artifact.from_api_response(["id", "Custom Report", 2, None, 3])

        assert artifact.report_subtype == "report"

    def test_report_subtype_non_report(self):
        """Test report_subtype for non-report artifact."""
        artifact = Artifact.from_api_response(["id", "Audio", 1, None, 3])

        assert artifact.report_subtype is None


class TestGenerationStatus:
    def test_properties(self):
        """Test all status properties."""
        pending = GenerationStatus(task_id="t1", status="pending")
        in_progress = GenerationStatus(task_id="t2", status="in_progress")
        completed = GenerationStatus(task_id="t3", status="completed")
        failed = GenerationStatus(task_id="t4", status="failed")

        assert pending.is_pending is True
        assert pending.is_in_progress is False

        assert in_progress.is_in_progress is True
        assert in_progress.is_pending is False

        assert completed.is_complete is True
        assert completed.is_failed is False

        assert failed.is_failed is True
        assert failed.is_complete is False

    def test_with_url_and_error(self):
        """Test status with optional fields."""
        status = GenerationStatus(
            task_id="t1",
            status="completed",
            url="https://audio.url",
            error=None,
        )

        assert status.url == "https://audio.url"
        assert status.error is None

    def test_with_metadata(self):
        """Test status with metadata."""
        status = GenerationStatus(
            task_id="t1",
            status="completed",
            metadata={"key": "value"},
        )

        assert status.metadata == {"key": "value"}

    def test_is_rate_limited(self):
        """Test is_rate_limited property detection."""
        # Rate limited via error_code (preferred)
        rate_limited_code = GenerationStatus(
            task_id="",
            status="failed",
            error="Request rejected by API",
            error_code="USER_DISPLAYABLE_ERROR",
        )
        assert rate_limited_code.is_rate_limited is True

        # Rate limited via error message (string matching fallback)
        rate_limited_msg = GenerationStatus(
            task_id="",
            status="failed",
            error="Request rejected by API - may indicate rate limiting or quota exceeded",
        )
        assert rate_limited_msg.is_rate_limited is True

        # Quota exceeded (also rate limited)
        quota_exceeded = GenerationStatus(
            task_id="",
            status="failed",
            error="Quota exceeded for this operation",
        )
        assert quota_exceeded.is_rate_limited is True

        # Other failure (not rate limited)
        other_failure = GenerationStatus(
            task_id="",
            status="failed",
            error="Generation failed - no artifact_id returned",
        )
        assert other_failure.is_rate_limited is False

        # Failed but no error message
        no_error = GenerationStatus(task_id="", status="failed", error=None)
        assert no_error.is_rate_limited is False

        # Completed status (never rate limited)
        completed = GenerationStatus(task_id="t1", status="completed")
        assert completed.is_rate_limited is False


class TestNotebookDescription:
    def test_from_api_response(self):
        """Test parsing NotebookDescription from dict."""
        data = {
            "summary": "This is a summary.",
            "suggested_topics": [
                {"question": "Q1?", "prompt": "P1"},
                {"question": "Q2?", "prompt": "P2"},
            ],
        }
        desc = NotebookDescription.from_api_response(data)

        assert desc.summary == "This is a summary."
        assert len(desc.suggested_topics) == 2
        assert desc.suggested_topics[0].question == "Q1?"
        assert desc.suggested_topics[0].prompt == "P1"

    def test_from_api_response_empty(self):
        """Test parsing with empty data."""
        data = {}
        desc = NotebookDescription.from_api_response(data)

        assert desc.summary == ""
        assert desc.suggested_topics == []


class TestReportSuggestion:
    def test_from_api_response(self):
        """Test parsing ReportSuggestion."""
        data = {
            "title": "Research Report",
            "description": "A detailed report",
            "prompt": "Write a report",
            "audience_level": 1,
        }
        suggestion = ReportSuggestion.from_api_response(data)

        assert suggestion.title == "Research Report"
        assert suggestion.description == "A detailed report"
        assert suggestion.prompt == "Write a report"
        assert suggestion.audience_level == 1

    def test_from_api_response_defaults(self):
        """Test parsing with missing optional fields."""
        data = {}
        suggestion = ReportSuggestion.from_api_response(data)

        assert suggestion.title == ""
        assert suggestion.audience_level == 2


class TestNote:
    def test_from_api_response(self):
        """Test parsing Note."""
        data = ["note_123", "Note Title", "Note content here"]
        note = Note.from_api_response(data, "nb_123")

        assert note.id == "note_123"
        assert note.notebook_id == "nb_123"
        assert note.title == "Note Title"
        assert note.content == "Note content here"

    def test_from_api_response_with_timestamp(self):
        """Test parsing Note with timestamp."""
        ts = 1704067200
        data = ["note_123", "Title", "Content", [ts]]
        note = Note.from_api_response(data, "nb_123")

        assert note.created_at is not None
        assert note.created_at.timestamp() == ts

    def test_from_api_response_empty(self):
        """Test parsing with minimal data."""
        data = []
        note = Note.from_api_response(data, "nb_123")

        assert note.id == ""
        assert note.title == ""
        assert note.content == ""


class TestChatMode:
    def test_enum_values(self):
        """Test ChatMode enum values."""
        assert ChatMode.DEFAULT.value == "default"
        assert ChatMode.LEARNING_GUIDE.value == "learning_guide"
        assert ChatMode.CONCISE.value == "concise"
        assert ChatMode.DETAILED.value == "detailed"


class TestConversationTurn:
    def test_creation(self):
        """Test ConversationTurn creation."""
        turn = ConversationTurn(
            query="What is AI?",
            answer="AI stands for Artificial Intelligence.",
            turn_number=1,
        )

        assert turn.query == "What is AI?"
        assert turn.answer == "AI stands for Artificial Intelligence."
        assert turn.turn_number == 1


class TestAskResult:
    def test_creation(self):
        """Test AskResult creation."""
        result = AskResult(
            answer="The answer is 42.",
            conversation_id="conv_123",
            turn_number=1,
            is_follow_up=False,
            raw_response="Full raw response",
        )

        assert result.answer == "The answer is 42."
        assert result.conversation_id == "conv_123"
        assert result.turn_number == 1
        assert result.is_follow_up is False
        assert result.raw_response == "Full raw response"

    def test_creation_with_references(self):
        """Test AskResult creation with references."""
        refs = [
            ChatReference(source_id="src-1", citation_number=1),
            ChatReference(source_id="src-2", citation_number=2),
        ]
        result = AskResult(
            answer="Based on [1] and [2]...",
            conversation_id="conv_123",
            turn_number=1,
            is_follow_up=False,
            references=refs,
        )

        assert len(result.references) == 2
        assert result.references[0].source_id == "src-1"
        assert result.references[1].citation_number == 2

    def test_default_references_empty(self):
        """Test that references defaults to empty list."""
        result = AskResult(
            answer="Answer",
            conversation_id="conv_123",
            turn_number=1,
            is_follow_up=False,
        )

        assert result.references == []


class TestChatReference:
    def test_creation_minimal(self):
        """Test ChatReference with just source_id."""
        ref = ChatReference(source_id="abc123-def456-789")

        assert ref.source_id == "abc123-def456-789"
        assert ref.citation_number is None
        assert ref.start_char is None
        assert ref.end_char is None

    def test_creation_full(self):
        """Test ChatReference with all fields."""
        ref = ChatReference(
            source_id="abc123-def456-789",
            citation_number=1,
            start_char=100,
            end_char=200,
        )

        assert ref.source_id == "abc123-def456-789"
        assert ref.citation_number == 1
        assert ref.start_char == 100
        assert ref.end_char == 200


class TestSourceFulltext:
    def test_creation(self):
        """Test SourceFulltext creation."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="My Source",
            content="This is the full content of the source.",
            source_type=5,  # web_page
            url="https://example.com",
            char_count=40,
        )

        assert fulltext.source_id == "src-123"
        assert fulltext.title == "My Source"
        assert fulltext.content == "This is the full content of the source."
        assert fulltext.source_type == 5
        assert fulltext.url == "https://example.com"
        assert fulltext.char_count == 40

    def test_creation_minimal(self):
        """Test SourceFulltext with minimal fields."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Title",
            content="Content",
        )

        assert fulltext.source_id == "src-123"
        assert fulltext.source_type is None
        assert fulltext.url is None
        assert fulltext.char_count == 0

    def test_find_citation_context_single_match(self):
        """Test finding a single citation in content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Before text. The citation text appears here. After text.",
        )

        matches = fulltext.find_citation_context("The citation text", context_chars=10)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 13  # Position of "The citation text"
        assert "The citation text" in context

    def test_find_citation_context_multiple_matches(self):
        """Test finding multiple non-overlapping matches."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="First keyword here. Some other text. Second keyword here.",
        )

        matches = fulltext.find_citation_context("keyword", context_chars=5)

        assert len(matches) == 2
        assert matches[0][1] == 6  # Position of first "keyword"
        assert matches[1][1] == 44  # Position of second "keyword"

    def test_find_citation_context_no_match(self):
        """Test when citation is not found."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Some content that doesn't contain the search term.",
        )

        matches = fulltext.find_citation_context("nonexistent")

        assert matches == []

    def test_find_citation_context_empty_cited_text(self):
        """Test with empty cited_text."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Some content here.",
        )

        assert fulltext.find_citation_context("") == []
        assert fulltext.find_citation_context(None) == []  # type: ignore

    def test_find_citation_context_empty_content(self):
        """Test with empty content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="",
        )

        matches = fulltext.find_citation_context("search term")

        assert matches == []

    def test_find_citation_context_long_citation_truncated(self):
        """Test that citations >40 chars are truncated for search."""
        long_citation = "A" * 50  # 50 chars, should be truncated to 40
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Prefix " + "A" * 40 + "B" * 10 + " Suffix",  # Only first 40 As match
        )

        matches = fulltext.find_citation_context(long_citation, context_chars=5)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 7  # Position after "Prefix "
        # Context should use search_text length (40), not cited_text length (50)
        assert len(context) <= 5 + 40 + 5  # context_chars + search_text + context_chars

    def test_find_citation_context_at_start(self):
        """Test citation at the very start of content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Citation at start. Rest of content.",
        )

        matches = fulltext.find_citation_context("Citation at start", context_chars=50)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 0

    def test_find_citation_context_at_end(self):
        """Test citation at the very end of content."""
        fulltext = SourceFulltext(
            source_id="src-123",
            title="Test",
            content="Beginning content. Citation at end",
        )

        matches = fulltext.find_citation_context("Citation at end", context_chars=50)

        assert len(matches) == 1
        context, pos = matches[0]
        assert pos == 19
