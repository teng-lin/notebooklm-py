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


@pytest.fixture
def chat_api(auth_tokens):
    """Provides a ChatAPI instance for testing."""
    return NotebookLMClient(auth_tokens).chat


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


class TestAnswerExtraction:
    """Tests for answer extraction from response chunks (issue #118).

    The answer parsing must handle:
    - Responses without the type_info[-1]==1 answer marker
    - Short answers below the minimum length threshold
    """

    @staticmethod
    def _build_response(*chunks: list) -> str:
        """Build a streaming response string from one or more inner_data chunks."""
        parts = [")]}'"]
        for chunk in chunks:
            inner_json = json.dumps(chunk)
            chunk_json = json.dumps([["wrb.fr", None, inner_json]])
            parts.append(f"\n{len(chunk_json)}\n{chunk_json}")
        parts.append("\n")
        return "".join(parts)

    def test_extract_answer_without_answer_marker(self, chat_api):
        """Test that answers are extracted even when type_info[-1] != 1.

        Google's API may change the answer marker. The parser should
        still extract valid text content as the answer.
        """
        inner_data = [
            [
                "This is a valid answer from NotebookLM about the topic.",
                None,
                ["chunk-id", 12345],
                None,
                [[], None, None, []],  # No trailing 1 marker
            ]
        ]

        answer, refs, _ = chat_api._parse_ask_response_with_references(
            self._build_response(inner_data)
        )
        assert answer == "This is a valid answer from NotebookLM about the topic."

    def test_extract_answer_with_different_marker_value(self, chat_api):
        """Test extraction when marker value changes from 1 to something else."""
        inner_data = [
            [
                "The answer text that should be extracted regardless of marker.",
                None,
                ["chunk-id", 12345],
                None,
                [[], None, None, [], 2],  # Different marker value
            ]
        ]

        answer, refs, _ = chat_api._parse_ask_response_with_references(
            self._build_response(inner_data)
        )
        assert answer == "The answer text that should be extracted regardless of marker."

    def test_extract_short_answer(self, chat_api):
        """Test that short answers (< 20 chars) are extracted.

        The user may ask 'Respond with exactly: OK' and get a short answer.
        """
        inner_data = [
            [
                "OK",
                None,
                ["chunk-id", 12345],
                None,
                [[], None, None, [], 1],
            ]
        ]

        answer, refs, _ = chat_api._parse_ask_response_with_references(
            self._build_response(inner_data)
        )
        assert answer == "OK"

    def test_extract_answer_no_type_info_at_all(self, chat_api):
        """Test extraction when first[4] is entirely missing."""
        inner_data = [
            [
                "An answer with no type_info metadata at all in the response.",
                None,
                ["chunk-id", 12345],
                None,
                # No first[4]
            ]
        ]

        answer, refs, _ = chat_api._parse_ask_response_with_references(
            self._build_response(inner_data)
        )
        assert answer == "An answer with no type_info metadata at all in the response."

    def test_shorter_marked_answer_beats_longer_unmarked(self, chat_api):
        """Shorter marked answer should win over longer unmarked text."""
        # Unmarked chunk is much longer
        unmarked_data = [
            [
                "This is a very long status message or streaming preamble that contains lots of text but is not the actual answer to the question.",
                None,
                ["chunk-1", 11111],
                None,
                [[], None, None, []],  # No marker
            ]
        ]
        # Marked chunk is shorter but is the real answer
        marked_data = [
            [
                "The actual short answer.",
                None,
                ["chunk-2", 22222],
                None,
                [[], None, None, [], 1],  # Has marker
            ]
        ]

        answer, refs, _ = chat_api._parse_ask_response_with_references(
            self._build_response(unmarked_data, marked_data)
        )
        assert answer == "The actual short answer."

    def test_skips_empty_and_non_string_text(self, chat_api):
        """Empty strings, None, and non-string first[0] values are skipped."""
        # Chunk with empty string text
        empty_data = [
            [
                "",
                None,
                ["chunk-1", 11111],
                None,
                [[], None, None, [], 1],
            ]
        ]
        # Chunk with None text
        none_data = [
            [
                None,
                None,
                ["chunk-2", 22222],
                None,
                [[], None, None, [], 1],
            ]
        ]
        # Chunk with integer text
        int_data = [
            [
                42,
                None,
                ["chunk-3", 33333],
                None,
                [[], None, None, [], 1],
            ]
        ]
        # Valid chunk that should be selected
        valid_data = [
            [
                "The valid answer after invalid chunks.",
                None,
                ["chunk-4", 44444],
                None,
                [[], None, None, [], 1],
            ]
        ]

        answer, refs, _ = chat_api._parse_ask_response_with_references(
            self._build_response(empty_data, none_data, int_data, valid_data)
        )
        assert answer == "The valid answer after invalid chunks."

    def test_prefers_marked_answer_over_unmarked(self, chat_api):
        """When both marked and unmarked answers exist, prefer the marked one."""
        unmarked_data = [
            [
                "This is a status message or partial streaming chunk text.",
                None,
                ["chunk-1", 11111],
                None,
                [[], None, None, []],  # No marker
            ]
        ]
        marked_data = [
            [
                "This is the real answer with proper marker.",
                None,
                ["chunk-2", 22222],
                None,
                [[], None, None, [], 1],  # Has marker
            ]
        ]

        answer, refs, _ = chat_api._parse_ask_response_with_references(
            self._build_response(unmarked_data, marked_data)
        )
        assert answer == "This is the real answer with proper marker."


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


class TestMultiChunkReferenceInflation:
    """Tests for issue #300: references inflated across multiple streaming chunks.

    The API streams multiple progressive chunks. Each chunk with an answer
    contains a full copy of the references. Before the fix, all_references
    accumulated from every chunk, causing 600+ refs when only ~20 were cited.
    """

    @staticmethod
    def _make_answer_chunk(answer_text: str, source_id: str, chunk_id: str) -> list:
        """Build a single streaming inner_data chunk with one citation."""
        return [
            [
                answer_text,
                None,
                [chunk_id, 12345],
                None,
                [
                    [],
                    None,
                    None,
                    [
                        [
                            [chunk_id],
                            [
                                None,
                                None,
                                0.9,
                                [[None]],
                                [[[10, 50, [[[5, 20, f"Text from {chunk_id}."]]]]]],
                                [[[[source_id]]]],
                                [chunk_id],
                            ],
                        ],
                    ],
                    1,
                ],
            ]
        ]

    @staticmethod
    def _build_multi_chunk_response(*chunks: list) -> str:
        parts = [")]}'"]
        for chunk in chunks:
            inner_json = json.dumps(chunk)
            chunk_json = json.dumps([["wrb.fr", None, inner_json]])
            parts.append(f"\n{len(chunk_json)}\n{chunk_json}")
        parts.append("\n")
        return "".join(parts)

    def test_multi_chunk_does_not_inflate_references(self, chat_api):
        """References must not multiply when the API streams repeated chunks.

        Issue #300: each streaming chunk repeated the full reference list, so
        N chunks x M refs = N*M entries instead of M. The fix tracks refs
        alongside the best answer instead of accumulating across all chunks.
        """
        source_id = "aaaabbbb-cccc-dddd-eeee-ffffffffffff"
        short_chunk = self._make_answer_chunk("Short.", source_id, "chunk-1")
        long_chunk = self._make_answer_chunk(
            "This is the longer final answer from NotebookLM.", source_id, "chunk-2"
        )

        response = self._build_multi_chunk_response(short_chunk, long_chunk)
        answer, refs, _ = chat_api._parse_ask_response_with_references(response)

        assert answer == "This is the longer final answer from NotebookLM."
        # One citation from the winning chunk only — not 2 (one per chunk)
        assert len(refs) == 1, f"Expected 1 reference, got {len(refs)}"
        assert refs[0].source_id == source_id

    def test_many_chunks_still_single_ref_set(self, chat_api):
        """Simulate 6 progressive streaming chunks (as in the bug report: 6x~67 = 402 sources)."""
        source_id = "12345678-1234-1234-1234-123456789012"
        chunks = [
            self._make_answer_chunk(
                f"Answer {'x' * i} final NotebookLM response.",
                source_id,
                f"chunk-{i}",
            )
            for i in range(1, 7)
        ]

        response = self._build_multi_chunk_response(*chunks)
        _, refs, _ = chat_api._parse_ask_response_with_references(response)

        # Should have exactly 1 reference from the longest (last) chunk, not 6
        assert len(refs) == 1, f"Expected 1 reference, got {len(refs)}"

    def test_refs_from_longest_winning_chunk(self, chat_api):
        """Refs must come from the chunk that produced the longest answer."""
        winning_source = "aaaabbbb-1111-2222-3333-ccccddddeeee"
        losing_source = "11112222-3333-4444-5555-666677778888"

        short_chunk = self._make_answer_chunk("Short answer.", losing_source, "chunk-short")
        long_chunk = self._make_answer_chunk(
            "This is definitively the longer, winning answer text.", winning_source, "chunk-long"
        )

        response = self._build_multi_chunk_response(short_chunk, long_chunk)
        answer, refs, _ = chat_api._parse_ask_response_with_references(response)

        assert answer == "This is definitively the longer, winning answer text."
        assert len(refs) == 1
        assert refs[0].source_id == winning_source, "Refs must belong to the winning chunk"
