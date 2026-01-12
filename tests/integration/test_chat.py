"""Integration tests for ChatAPI."""

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.rpc import ChatGoal, ChatResponseLength, RPCMethod
from notebooklm.types import ChatMode


class TestChatAPI:
    """Integration tests for the ChatAPI."""

    @pytest.mark.asyncio
    async def test_get_history(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting conversation history."""
        response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_HISTORY,
            [
                ["conv_001", "What is ML?", "Machine learning is...", 1704067200],
                ["conv_002", "Explain AI", "Artificial intelligence...", 1704153600],
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_history("nb_123")

        assert result is not None
        request = httpx_mock.get_request()
        assert RPCMethod.GET_CONVERSATION_HISTORY in str(request.url)

    @pytest.mark.asyncio
    async def test_get_history_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting empty conversation history."""
        response = build_rpc_response(RPCMethod.GET_CONVERSATION_HISTORY, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_history("nb_123")

        assert result == []

    @pytest.mark.asyncio
    async def test_configure_default_mode(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test configuring chat with default settings."""
        response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.chat.configure("nb_123")

        request = httpx_mock.get_request()
        assert RPCMethod.RENAME_NOTEBOOK in str(request.url)

    @pytest.mark.asyncio
    async def test_configure_learning_guide_mode(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test configuring chat as learning guide."""
        response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.chat.configure(
                "nb_123",
                goal=ChatGoal.LEARNING_GUIDE,
                response_length=ChatResponseLength.LONGER,
            )

        request = httpx_mock.get_request()
        assert RPCMethod.RENAME_NOTEBOOK in str(request.url)

    @pytest.mark.asyncio
    async def test_configure_custom_mode_without_prompt_raises(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that CUSTOM mode without prompt raises ValueError."""
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValueError, match="custom_prompt is required"):
                await client.chat.configure("nb_123", goal=ChatGoal.CUSTOM)

    @pytest.mark.asyncio
    async def test_configure_custom_mode_with_prompt(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test configuring chat with custom prompt."""
        response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.chat.configure(
                "nb_123",
                goal=ChatGoal.CUSTOM,
                custom_prompt="You are a helpful tutor.",
            )

        request = httpx_mock.get_request()
        assert RPCMethod.RENAME_NOTEBOOK in str(request.url)

    @pytest.mark.asyncio
    async def test_set_mode(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test setting chat mode with predefined config."""
        response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.chat.set_mode("nb_123", ChatMode.CONCISE)

        request = httpx_mock.get_request()
        assert RPCMethod.RENAME_NOTEBOOK in str(request.url)

    def test_get_cached_turns_empty(self, auth_tokens):
        """Test getting cached turns for new conversation."""
        client = NotebookLMClient(auth_tokens)
        turns = client.chat.get_cached_turns("nonexistent_conv")
        assert turns == []

    def test_clear_cache(self, auth_tokens):
        """Test clearing conversation cache."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat.clear_cache("some_conv")
        assert result is False

    def test_clear_all_cache(self, auth_tokens):
        """Test clearing all conversation caches."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat.clear_cache()
        assert result is True


class TestChatReferences:
    """Integration tests for chat references and citations."""

    @pytest.mark.asyncio
    async def test_ask_with_citations_returns_references(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test ask() returns references when citations are present."""
        import json
        import re

        # Build a realistic response with citations
        # Structure discovered via API analysis:
        # cite[1][4] = [[passage_wrapper]] where passage_wrapper[0] = [start, end, nested]
        # nested = [[inner]] where inner = [start2, end2, text]
        inner_data = [
            [
                "Machine learning is a subset of AI [1]. It uses algorithms to learn from data [2].",
                None,
                ["chunk-001", "chunk-002", 987654],
                None,
                [
                    [],
                    None,
                    None,
                    [
                        # First citation
                        [
                            ["chunk-001"],
                            [
                                None,
                                None,
                                0.95,
                                [[None]],
                                [  # cite[1][4] - text passages
                                    [  # passage_wrapper
                                        [  # passage_data
                                            100,  # start_char
                                            250,  # end_char
                                            [  # nested passages
                                                [  # nested_group
                                                    [  # inner
                                                        50,
                                                        120,
                                                        "Machine learning is a branch of artificial intelligence.",
                                                    ]
                                                ]
                                            ],
                                        ]
                                    ]
                                ],
                                [[[["11111111-1111-1111-1111-111111111111"]]]],
                                ["chunk-001"],
                            ],
                        ],
                        # Second citation
                        [
                            ["chunk-002"],
                            [
                                None,
                                None,
                                0.88,
                                [[None]],
                                [
                                    [
                                        [
                                            300,
                                            450,
                                            [
                                                [
                                                    [
                                                        280,
                                                        380,
                                                        "Algorithms learn patterns from training data.",
                                                    ]
                                                ]
                                            ],
                                        ]
                                    ]
                                ],
                                [[[["22222222-2222-2222-2222-222222222222"]]]],
                                ["chunk-002"],
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
                notebook_id="test_nb",
                question="What is machine learning?",
                source_ids=["src_001"],
            )

        # Verify answer
        assert "Machine learning" in result.answer
        assert "[1]" in result.answer
        assert "[2]" in result.answer

        # Verify references
        assert len(result.references) == 2

        # First reference
        ref1 = result.references[0]
        assert ref1.source_id == "11111111-1111-1111-1111-111111111111"
        assert ref1.citation_number == 1
        assert "artificial intelligence" in ref1.cited_text

        # Second reference
        ref2 = result.references[1]
        assert ref2.source_id == "22222222-2222-2222-2222-222222222222"
        assert ref2.citation_number == 2
        assert "training data" in ref2.cited_text

    @pytest.mark.asyncio
    async def test_ask_without_citations(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test ask() works when no citations are in the response."""
        import json
        import re

        inner_data = [
            [
                "This is a simple answer without any source citations.",
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
                notebook_id="test_nb",
                question="Simple question",
                source_ids=["src_001"],
            )

        assert result.answer == "This is a simple answer without any source citations."
        assert len(result.references) == 0

    @pytest.mark.asyncio
    async def test_references_include_char_positions(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that references include character position information."""
        import json
        import re

        inner_data = [
            [
                "Answer with citation [1].",
                None,
                ["chunk-001", 12345],
                None,
                [
                    [],
                    None,
                    None,
                    [
                        [
                            ["chunk-001"],
                            [
                                None,
                                None,
                                0.9,
                                [[None]],
                                [
                                    [
                                        [
                                            1000,  # start_char
                                            1500,  # end_char
                                            [[[[950, 1100, "Cited passage text."]]]],
                                        ]
                                    ]
                                ],
                                [[[["aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"]]]],
                                ["chunk-001"],
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
                notebook_id="test_nb",
                question="Question",
                source_ids=["src_001"],
            )

        assert len(result.references) == 1
        ref = result.references[0]
        assert ref.start_char == 1000
        assert ref.end_char == 1500
        assert ref.chunk_id == "chunk-001"
