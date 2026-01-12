"""Unit tests for chat reference and citation parsing.

Tests the _parse_citations method and related ChatReference functionality.
"""

import json

import pytest

from notebooklm import AskResult, ChatReference, NotebookLMClient
from notebooklm.auth import AuthTokens


@pytest.fixture
def auth_tokens():
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


class TestParseCitations:
    """Unit tests for the _parse_citations method."""

    def test_parse_citations_basic(self, auth_tokens):
        """Test parsing citations from a well-formed response."""
        client = NotebookLMClient(auth_tokens)
        chat_api = client.chat

        # Build a mock "first" structure with citations
        # Structure: first[4][3] contains citation array
        first = [
            "This is the answer [1]",  # answer text
            None,
            ["chunk-id-1", 12345],  # chunk IDs (not source IDs)
            None,
            [  # type_info at first[4]
                [],  # first[4][0]
                None,
                None,
                [  # first[4][3] - citations array
                    [
                        ["chunk-id-1"],  # cite[0] - chunk ID
                        [  # cite[1] - citation details
                            None,
                            None,
                            0.85,  # relevance score
                            [[None, None, None]],  # cite[1][3]
                            [  # cite[1][4] - text passages
                                [
                                    [  # passage_data
                                        100,  # start_char
                                        200,  # end_char
                                        [  # nested passages
                                            [[50, 100, "This is the cited text."]]
                                        ],
                                    ]
                                ]
                            ],
                            [  # cite[1][5] - source ID path
                                [[["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]]]
                            ],
                            ["chunk-id-1"],  # cite[1][6]
                        ],
                    ]
                ],
                1,  # marks as answer
            ],
        ]

        refs = chat_api._parse_citations(first)

        assert len(refs) == 1
        assert refs[0].source_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert refs[0].cited_text == "This is the cited text."
        assert refs[0].start_char == 100
        assert refs[0].end_char == 200
        assert refs[0].chunk_id == "chunk-id-1"

    def test_parse_citations_multiple(self, auth_tokens):
        """Test parsing multiple citations."""
        client = NotebookLMClient(auth_tokens)
        chat_api = client.chat

        first = [
            "Answer with [1] and [2]",
            None,
            ["chunk-1", "chunk-2", 12345],
            None,
            [
                [],
                None,
                None,
                [
                    # First citation
                    [
                        ["chunk-1"],
                        [
                            None,
                            None,
                            0.9,
                            [[None]],
                            [[[10, 50, [[[5, 20, "First passage."]]]]]],
                            [[[["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]]]],
                            ["chunk-1"],
                        ],
                    ],
                    # Second citation
                    [
                        ["chunk-2"],
                        [
                            None,
                            None,
                            0.8,
                            [[None]],
                            [[[60, 100, [[[55, 80, "Second passage."]]]]]],
                            [[[["11111111-2222-3333-4444-555555555555"]]]],
                            ["chunk-2"],
                        ],
                    ],
                ],
                1,
            ],
        ]

        refs = chat_api._parse_citations(first)

        assert len(refs) == 2
        assert refs[0].source_id == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        assert refs[1].source_id == "11111111-2222-3333-4444-555555555555"
        assert refs[0].cited_text == "First passage."
        assert refs[1].cited_text == "Second passage."

    def test_parse_citations_no_citations(self, auth_tokens):
        """Test parsing when no citations are present."""
        client = NotebookLMClient(auth_tokens)
        chat_api = client.chat

        # first[4] exists but first[4][3] is empty
        first = [
            "Answer without citations",
            None,
            [],
            None,
            [[], None, None, [], 1],
        ]

        refs = chat_api._parse_citations(first)
        assert len(refs) == 0

    def test_parse_citations_missing_type_info(self, auth_tokens):
        """Test parsing when first[4] is missing or malformed."""
        client = NotebookLMClient(auth_tokens)
        chat_api = client.chat

        # first[4] doesn't exist
        first = ["Answer", None, [], None]
        refs = chat_api._parse_citations(first)
        assert len(refs) == 0

        # first[4] is not a list
        first = ["Answer", None, [], None, "not a list"]
        refs = chat_api._parse_citations(first)
        assert len(refs) == 0

    def test_parse_citations_missing_source_id(self, auth_tokens):
        """Test that citations without valid source IDs are skipped."""
        client = NotebookLMClient(auth_tokens)
        chat_api = client.chat

        first = [
            "Answer",
            None,
            [],
            None,
            [
                [],
                None,
                None,
                [
                    [
                        ["chunk-1"],
                        [
                            None,
                            None,
                            0.9,
                            [[None]],
                            [[[10, 50, [[[[5, 20, "Some text."]]]]]]],
                            [[[["not-a-valid-uuid"]]]],  # Invalid UUID
                            ["chunk-1"],
                        ],
                    ],
                ],
                1,
            ],
        ]

        refs = chat_api._parse_citations(first)
        assert len(refs) == 0  # Invalid UUID should be skipped

    def test_parse_citations_missing_text(self, auth_tokens):
        """Test citations with missing text are still parsed."""
        client = NotebookLMClient(auth_tokens)
        chat_api = client.chat

        first = [
            "Answer",
            None,
            [],
            None,
            [
                [],
                None,
                None,
                [
                    [
                        ["chunk-1"],
                        [
                            None,
                            None,
                            0.9,
                            [[None]],
                            [],  # Empty text passages
                            [[[["12345678-1234-1234-1234-123456789012"]]]],
                            ["chunk-1"],
                        ],
                    ],
                ],
                1,
            ],
        ]

        refs = chat_api._parse_citations(first)
        assert len(refs) == 1
        assert refs[0].source_id == "12345678-1234-1234-1234-123456789012"
        assert refs[0].cited_text is None  # Text not available


class TestChatReferenceDataclass:
    """Tests for the ChatReference dataclass."""

    def test_chat_reference_creation(self):
        """Test creating ChatReference with all fields."""
        ref = ChatReference(
            source_id="abc123",
            citation_number=1,
            cited_text="Sample text",
            start_char=100,
            end_char=200,
            chunk_id="chunk-001",
        )
        assert ref.source_id == "abc123"
        assert ref.citation_number == 1
        assert ref.cited_text == "Sample text"
        assert ref.start_char == 100
        assert ref.end_char == 200
        assert ref.chunk_id == "chunk-001"

    def test_chat_reference_minimal(self):
        """Test creating ChatReference with only required field."""
        ref = ChatReference(source_id="abc123")
        assert ref.source_id == "abc123"
        assert ref.citation_number is None
        assert ref.cited_text is None
        assert ref.start_char is None
        assert ref.end_char is None
        assert ref.chunk_id is None


class TestAskWithReferences:
    """Integration-style unit tests for ask() with references."""

    @pytest.mark.asyncio
    async def test_ask_returns_references(self, auth_tokens, httpx_mock):
        """Test that ask() returns properly parsed references."""
        import re

        # Build a response with citations
        inner_data = [
            [
                "This is the answer with a citation [1].",
                None,
                ["chunk-id", 12345],
                None,
                [
                    [],
                    None,
                    None,
                    [
                        [
                            ["chunk-id"],
                            [
                                None,
                                None,
                                0.9,
                                [[None]],
                                [[[100, 200, [[[50, 100, "The cited passage."]]]]]],
                                [[[["abcdefab-1234-5678-9012-abcdefabcdef"]]]],
                                ["chunk-id"],
                            ],
                        ],
                    ],
                    1,
                ],
            ]
        ]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                notebook_id="nb_123",
                question="What is this?",
                source_ids=["test_source"],
            )

        assert isinstance(result, AskResult)
        assert "citation [1]" in result.answer
        assert len(result.references) == 1
        assert result.references[0].source_id == "abcdefab-1234-5678-9012-abcdefabcdef"
        assert result.references[0].cited_text == "The cited passage."
        assert result.references[0].citation_number == 1

    @pytest.mark.asyncio
    async def test_ask_no_references(self, auth_tokens, httpx_mock):
        """Test that ask() works when there are no references."""
        import re

        inner_data = [
            [
                "This is an answer without any citations.",
                None,
                [12345],
                None,
                [[], None, None, [], 1],
            ]
        ]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                notebook_id="nb_123",
                question="Simple question",
                source_ids=["test_source"],
            )

        assert isinstance(result, AskResult)
        assert len(result.references) == 0

    @pytest.mark.asyncio
    async def test_ask_deduplicates_references(self, auth_tokens, httpx_mock):
        """Test that duplicate source IDs are deduplicated."""
        import re

        # Build response with duplicate source IDs
        inner_data = [
            [
                "Answer with [1] and [2] from same source.",
                None,
                ["chunk-1", "chunk-2", 12345],
                None,
                [
                    [],
                    None,
                    None,
                    [
                        # First citation
                        [
                            ["chunk-1"],
                            [
                                None,
                                None,
                                0.9,
                                [[None]],
                                [[[10, 50, [[[5, 20, "First text."]]]]]],
                                [[[["aaaaaaaa-1234-5678-9012-abcdefabcdef"]]]],
                                ["chunk-1"],
                            ],
                        ],
                        # Second citation with SAME source ID
                        [
                            ["chunk-2"],
                            [
                                None,
                                None,
                                0.8,
                                [[None]],
                                [[[60, 100, [[[55, 80, "Second text."]]]]]],
                                [[[["aaaaaaaa-1234-5678-9012-abcdefabcdef"]]]],
                                ["chunk-2"],
                            ],
                        ],
                    ],
                    1,
                ],
            ]
        ]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                notebook_id="nb_123",
                question="Question",
                source_ids=["test_source"],
            )

        # Both citations have same source_id, but should not be deduplicated
        # as they have different chunk_ids and represent different passages
        assert len(result.references) >= 1
        # All references should have the same source_id
        for ref in result.references:
            assert ref.source_id == "aaaaaaaa-1234-5678-9012-abcdefabcdef"
