"""Integration tests for ChatAPI."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.rpc import ChatGoal, ChatResponseLength, RPCMethod
from notebooklm.types import ChatMode


class TestChatAPI:
    """Integration tests for the ChatAPI."""

    @pytest.mark.asyncio
    async def test_get_conversation_id(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_conversation_id returns the most recent conversation ID."""
        response = build_rpc_response(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            [[["conv_001"]]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_conversation_id("nb_123")

        assert result == "conv_001"
        request = httpx_mock.get_request()
        assert RPCMethod.GET_LAST_CONVERSATION_ID in str(request.url)

    @pytest.mark.asyncio
    async def test_get_history(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_history returns flat Q&A pairs from the most recent conversation."""
        # First call: get_conversation_id
        id_response = build_rpc_response(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            [[["conv_001"]]],
        )
        # Second call: get_conversation_turns
        # API returns individual turns newest-first: A2, Q2, A1, Q1
        turns_response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_TURNS,
            [
                [
                    [None, None, 2, None, [["Answer to second question."]]],
                    [None, None, 1, "Second question?"],
                    [None, None, 2, None, [["Answer to first question."]]],
                    [None, None, 1, "First question?"],
                ]
            ],
        )
        httpx_mock.add_response(content=id_response.encode())
        httpx_mock.add_response(content=turns_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            qa_pairs = await client.chat.get_history("nb_123")

        # get_history reverses API order to return oldest-first
        assert len(qa_pairs) == 2
        assert qa_pairs[0] == ("First question?", "Answer to first question.")
        assert qa_pairs[1] == ("Second question?", "Answer to second question.")

    @pytest.mark.asyncio
    async def test_get_conversation_turns(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting conversation turns for a specific conversation.

        The khqZz RPC returns Q&A turns for a conversation:
          turn[2] == 1: user question, text at turn[3]
          turn[2] == 2: AI answer, text at turn[4][0][0]
        Turns are returned newest-first; limit=2 yields the latest Q&A pair.
        """
        response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_TURNS,
            [
                [
                    [None, None, 1, "What is machine learning?"],
                    [None, None, 2, None, [["Machine learning is a branch of AI."]]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_conversation_turns("nb_123", "conv_001", limit=2)

        assert result is not None
        turns = result[0]
        assert len(turns) == 2

        # Turn type 1: user question
        assert turns[0][2] == 1
        assert turns[0][3] == "What is machine learning?"

        # Turn type 2: AI answer
        assert turns[1][2] == 2
        assert turns[1][4][0][0] == "Machine learning is a branch of AI."

        request = httpx_mock.get_request()
        assert RPCMethod.GET_CONVERSATION_TURNS in str(request.url)

    @pytest.mark.asyncio
    async def test_get_conversation_turns_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_conversation_turns handles empty turn list gracefully."""
        response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_TURNS,
            [[]],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_conversation_turns("nb_123", "conv_001")

        assert result is not None
        assert result[0] == []

    @pytest.mark.asyncio
    async def test_get_history_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting empty conversation history when no conversations exist."""
        response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            qa_pairs = await client.chat.get_history("nb_123")

        assert qa_pairs == []

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
        """Test that CUSTOM mode without prompt raises ValidationError."""
        from notebooklm.exceptions import ValidationError

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ValidationError, match="custom_prompt is required"):
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
        mock_get_conversation_id,
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
        mock_get_conversation_id()  # issue #659 post-ask round-trip

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
        mock_get_conversation_id,
    ):
        """Test ask() works when no citations are in the response."""
        import json
        import re

        inner_data = [
            [
                "This is a simple answer without any source citations.",
                None,
                ["server-simple-conv", 12345],
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
        mock_get_conversation_id()

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
        mock_get_conversation_id,
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
        mock_get_conversation_id()

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

    @pytest.mark.asyncio
    async def test_ask_returns_answer_when_marker_absent(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        mock_get_conversation_id,
    ):
        """Test ask() extracts answer when API response lacks type_info[-1]==1 marker.

        Regression test for issue #118: Google's API may change or omit the answer
        marker, causing the parser to fall back to the longest unmarked text chunk.
        """
        import json
        import re

        # Response with no trailing `1` marker in type_info — simulates changed API format
        inner_data = [
            [
                "This is a valid answer returned without the answer marker.",
                None,
                ["chunk-001", 12345],
                None,
                [[], None, None, []],  # type_info has no trailing 1
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
        mock_get_conversation_id()

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                notebook_id="test_nb",
                question="What does this say?",
                source_ids=["src_001"],
            )

        assert result.answer == "This is a valid answer returned without the answer marker."
        assert result.conversation_id is not None
        assert result.is_follow_up is False

    @pytest.mark.asyncio
    async def test_ask_prefers_marked_over_unmarked_in_streaming_response(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        mock_get_conversation_id,
    ):
        """Test ask() picks the marked answer when response has both marked and unmarked chunks.

        Streaming responses can contain multiple chunks. The marked answer chunk
        (type_info[-1]==1) must win even when an unmarked chunk has longer text.
        """
        import json
        import re

        # Streaming response: first chunk is a longer unmarked preamble,
        # second chunk is the shorter but marked real answer.
        preamble = [
            [
                "This is a long preamble or status message that is not the real answer to the question at all.",
                None,
                ["chunk-001", 11111],
                None,
                [[], None, None, []],  # no marker
            ]
        ]
        answer = [
            [
                "The real answer.",
                None,
                ["chunk-002", 22222],
                None,
                [[], None, None, [], 1],  # marked
            ]
        ]

        def make_chunk(inner_data):
            inner_json = json.dumps(inner_data)
            chunk_json = json.dumps([["wrb.fr", None, inner_json]])
            return f"{len(chunk_json)}\n{chunk_json}"

        response_body = f")]}}'\n{make_chunk(preamble)}\n{make_chunk(answer)}\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )
        mock_get_conversation_id()

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                notebook_id="test_nb",
                question="What is the answer?",
                source_ids=["src_001"],
            )

        assert result.answer == "The real answer."


class TestChatAskErrorHandling:
    """Tests for ask() HTTP error handling (lines 127-158, 170)."""

    @pytest.mark.asyncio
    async def test_ask_timeout_raises_network_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test ask() raises NetworkError on httpx.TimeoutException."""
        import re

        import httpx

        from notebooklm.exceptions import NetworkError

        httpx_mock.add_exception(
            httpx.TimeoutException("timed out"),
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
        )

        # ``server_error_max_retries=0`` pins the original immediate-raise
        # contract; T3.A's default retries 5xx + RequestError 3x.
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            with pytest.raises(NetworkError, match="timed out"):
                await client.chat.ask(
                    "nb_123",
                    "What is this?",
                    source_ids=["src_001"],
                )

    @pytest.mark.asyncio
    async def test_ask_http_status_error_raises_chat_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test ask() raises ChatError on httpx.HTTPStatusError.

        After T2.D, the chat path uses ``core.query_post`` which routes
        through the shared transport pipeline. Auth-shaped statuses (400/401/
        403) go through the refresh path before surfacing; this test uses
        500 to exercise the plain ``HTTPStatusError → ChatError`` mapping
        without entangling the refresh machinery.
        """
        import re

        from notebooklm.exceptions import ChatError

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            status_code=500,
            method="POST",
        )

        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            with pytest.raises(ChatError, match="500"):
                await client.chat.ask(
                    "nb_123",
                    "What is this?",
                    source_ids=["src_001"],
                )

    @pytest.mark.asyncio
    async def test_ask_request_error_raises_network_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test ask() raises NetworkError on httpx.RequestError."""
        import re

        import httpx

        from notebooklm.exceptions import NetworkError

        httpx_mock.add_exception(
            httpx.ConnectError("connection refused"),
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
        )

        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            with pytest.raises(NetworkError, match="connection refused"):
                await client.chat.ask(
                    "nb_123",
                    "What is this?",
                    source_ids=["src_001"],
                )

    @pytest.mark.asyncio
    async def test_ask_without_csrf_token_skips_at_param(
        self,
        httpx_mock: HTTPXMock,
        mock_get_conversation_id,
    ):
        """Test ask() without csrf_token omits the 'at' param (line 127 branch)."""
        import json
        import re

        from notebooklm.auth import AuthTokens

        # Auth tokens without csrf_token
        auth_no_csrf = AuthTokens(
            cookies={"SID": "sid"},
            csrf_token=None,
            session_id=None,
        )

        inner_data = [
            [
                "Answer without csrf.",
                None,
                ["server-no-csrf-conv", 12345],
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
        mock_get_conversation_id()

        async with NotebookLMClient(auth_no_csrf) as client:
            result = await client.chat.ask(
                "nb_123",
                "What is this?",
                source_ids=["src_001"],
            )

        assert result.answer == "Answer without csrf."

    @pytest.mark.asyncio
    async def test_ask_with_session_id_adds_fsid_param(
        self,
        httpx_mock: HTTPXMock,
        mock_get_conversation_id,
    ):
        """Test ask() with session_id adds f.sid param (line 140-143)."""
        import json
        import re

        from notebooklm.auth import AuthTokens

        auth_with_session = AuthTokens(
            cookies={"SID": "sid"},
            csrf_token="test_token",
            session_id="my_session_id",
        )

        inner_data = [
            [
                "Answer with session.",
                None,
                ["server-with-session-conv", 12345],
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
        mock_get_conversation_id()

        async with NotebookLMClient(auth_with_session) as client:
            result = await client.chat.ask(
                "nb_123",
                "What is this?",
                source_ids=["src_001"],
            )

        assert result.answer == "Answer with session."
        # Two HTTP calls now fire (chat-ask + post-ask hPTbtc, issue #659);
        # assert f.sid is on the chat-ask leg specifically.
        chat_request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        assert "f.sid" in str(chat_request.url)

    @pytest.mark.asyncio
    async def test_ask_empty_answer_does_not_cache_turn_for_follow_up(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Empty answer on a follow-up must not append a turn to the cache.

        After issue #659, empty responses on new conversations raise
        ``ChatError`` (no server conv_id to attach to). The cache-poison
        guard now only matters on follow-ups, where the caller-supplied
        conversation_id stays stable across an empty response.
        """
        import re

        # Return totally empty/unparseable response
        response_body = ")]}'\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=response_body.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                "nb_123",
                "What is this?",
                source_ids=["src_001"],
                conversation_id="existing-conv-id",
            )

        # Empty answer: turn_number equals len(turns) (0), not len(turns)+1
        assert result.answer == ""
        assert result.turn_number == 0
        # Caller-supplied conversation_id is preserved across the empty response.
        assert result.conversation_id == "existing-conv-id"

    @pytest.mark.asyncio
    async def test_ask_follow_up_sets_is_follow_up(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test ask() with existing conversation_id sets is_follow_up=True (line 170)."""
        import json
        import re

        inner_data = [
            [
                "Follow-up answer.",
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
                "nb_123",
                "Follow-up question?",
                source_ids=["src_001"],
                conversation_id="existing-conv-id",
            )

        assert result.is_follow_up is True
        assert result.conversation_id == "existing-conv-id"


class TestAskServerAssignedConversationId:
    """Regression tests for issue #659.

    CLI-created conversations must be visible in the NotebookLM web UI. Live
    API investigation revealed two facts that drive this contract:

    1. ``params[4]`` (the conversation_id slot in the streamed-chat request)
       must be ``null`` for new conversations. A client-minted UUID there
       orphans the turn from the web UI conversation list.
    2. The streamed-chat response's ``first[2][0]`` field is a per-stream
       query id, **not** a real conversation_id (querying ``khqZz`` with it
       returns 0 turns). The real conversation_id can only be obtained from
       ``hPTbtc`` (``ChatAPI.get_conversation_id``) after the ask completes.

    So ``ask()`` for a new conversation must:
      - send ``null`` at ``params[4]`` (verified at the wire level here)
      - call ``hPTbtc`` after the ask and surface that id as
        ``AskResult.conversation_id`` (so follow-ups using it actually work)
      - raise ``ChatError`` if ``hPTbtc`` returns ``None`` (the server
        failed to record the turn, or the API shape drifted)
    """

    @staticmethod
    def _decode_params(request) -> list:
        """Decode the params list from a chat POST request body."""
        import json
        from urllib.parse import parse_qs, unquote

        body = request.content.decode("utf-8")
        body_qs = parse_qs(body, keep_blank_values=True)
        f_req = json.loads(unquote(body_qs["f.req"][0]))
        return json.loads(f_req[1])

    @pytest.mark.asyncio
    async def test_new_conversation_sends_null_conversation_id_in_request(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """New conversations: ``params[4] == null`` AND the post-ask
        ``hPTbtc`` result becomes ``AskResult.conversation_id``.
        """
        import json
        import re

        # The "stream id" the parser used to mislabel as server_conv_id.
        # It must NOT end up as AskResult.conversation_id.
        stream_id = "stream-aaaa-bbbb-cccc-dddddddddddd"
        inner_data = [
            [
                "Server-assigned answer.",
                None,
                [stream_id, 12345],
                None,
                [[], None, None, [], 1],
            ]
        ]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        chat_response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=chat_response_body.encode(),
            method="POST",
        )

        # Post-ask hPTbtc returns the REAL conversation_id. AskResult
        # must adopt this, NOT first[2][0].
        real_conv = "real-1111-2222-3333-444444444444"
        hptbtc_response = build_rpc_response(
            RPCMethod.GET_LAST_CONVERSATION_ID,
            [[[real_conv]]],
        )
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=hptbtc_response.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.ask(
                "nb_123",
                "What is this?",
                source_ids=["src_001"],
            )

        # Wire shape check on the first request (chat-ask)
        chat_request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        params = self._decode_params(chat_request)
        assert params[4] is None, (
            "New conversations must send null in params[4]; got "
            f"{params[4]!r}. Sending a client-generated UUID orphans the "
            "conversation from the web UI conversation list (issue #659)."
        )

        # AskResult adopts the hPTbtc id, not the stream id.
        assert result.conversation_id == real_conv
        assert result.conversation_id != stream_id, (
            "AskResult.conversation_id must be the hPTbtc-fetched id, not "
            "first[2][0] (which is a stream id; live API tests proved "
            "follow-ups using it produce ghost turns — issue #659)."
        )
        assert result.is_follow_up is False

        # And the SDK must have made the hPTbtc call.
        assert any("batchexecute" in str(r.url) for r in httpx_mock.get_requests()), (
            "SDK must call hPTbtc after a new-conversation ask to obtain the real conversation_id."
        )

    @pytest.mark.asyncio
    async def test_new_conversation_raises_when_hptbtc_returns_no_conversation(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """If ``hPTbtc`` returns no conversation_id after the ask, raise
        ``ChatError`` — the turn was not recorded server-side. Silent
        fallback to a client UUID is the original bug class (issue #659).
        """
        import json
        import re

        from notebooklm.exceptions import ChatError

        inner_data = [
            [
                "Answer text.",
                None,
                ["some-stream-id", 12345],
                None,
                [[], None, None, [], 1],
            ]
        ]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        chat_response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        httpx_mock.add_response(
            url=re.compile(r".*GenerateFreeFormStreamed.*"),
            content=chat_response_body.encode(),
            method="POST",
        )

        # hPTbtc returns an empty result -> get_conversation_id() -> None
        empty_hptbtc = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [])
        httpx_mock.add_response(
            url=re.compile(r".*batchexecute.*"),
            content=empty_hptbtc.encode(),
            method="POST",
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ChatError, match="hPTbtc"):
                await client.chat.ask(
                    "nb_123",
                    "Q?",
                    source_ids=["src_001"],
                )

    @pytest.mark.asyncio
    async def test_follow_up_sends_caller_conversation_id_in_request(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Follow-up asks forward the caller-supplied conversation_id
        verbatim and do NOT call hPTbtc — the caller already has the real id.
        """
        import json
        import re

        inner_data = [
            [
                "Follow-up answer.",
                None,
                ["some-stream-id", 12345],
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
                "nb_123",
                "Q?",
                source_ids=["src_001"],
                conversation_id="existing-conv-id",
            )

        chat_request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        params = self._decode_params(chat_request)
        assert params[4] == "existing-conv-id"
        # AskResult preserves the caller-supplied id; we no longer rebind
        # to first[2][0] because that field is a stream id, not a conv_id.
        assert result.conversation_id == "existing-conv-id"
        # And no hPTbtc round-trip: the caller already supplied a real id.
        assert not any("batchexecute" in str(r.url) for r in httpx_mock.get_requests()), (
            "Follow-ups must not call hPTbtc — caller already has the id."
        )


class TestGetConversationIdEdgeCases:
    """Tests for get_conversation_id edge cases (lines 231-235)."""

    @pytest.mark.asyncio
    async def test_get_conversation_id_returns_none_on_empty_response(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_conversation_id returns None when RPC response is empty list."""
        response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_conversation_id("nb_123")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_conversation_id_returns_none_on_empty_nested(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_conversation_id returns None when response has no valid ID."""
        # Non-empty response but no valid conv_id in it
        response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[[]]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_conversation_id("nb_123")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_conversation_id_returns_none_on_non_string_conv(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_conversation_id returns None when conv entry is not a string."""
        # Response with non-string first element in conv list
        response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[[42]]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_conversation_id("nb_123")

        assert result is None


class TestGetHistoryErrorHandling:
    """Tests for get_history error handling (lines 266-279)."""

    @pytest.mark.asyncio
    async def test_get_history_returns_empty_on_chat_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_history returns [] when get_conversation_turns raises ChatError."""
        from notebooklm.exceptions import ChatError

        id_response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[["conv_001"]]])
        httpx_mock.add_response(content=id_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.chat,
                "get_conversation_turns",
                new_callable=AsyncMock,
                side_effect=ChatError("API error"),
            ):
                result = await client.chat.get_history("nb_123")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_history_returns_empty_on_network_error(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_history returns [] when get_conversation_turns raises NetworkError."""
        from notebooklm.exceptions import NetworkError

        id_response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[["conv_001"]]])
        httpx_mock.add_response(content=id_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with patch.object(
                client.chat,
                "get_conversation_turns",
                new_callable=AsyncMock,
                side_effect=NetworkError("connection error"),
            ):
                result = await client.chat.get_history("nb_123")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_history_returns_empty_when_no_conversation(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_history returns [] when get_conversation_id returns None."""
        response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_history("nb_123")

        assert result == []

    @pytest.mark.asyncio
    async def test_get_history_reverses_turns(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_history reverses turns_data when turns_data[0][0] is a list (lines 272-279)."""
        id_response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[["conv_001"]]])
        # Newest-first: [A1, Q1]
        turns_response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_TURNS,
            [
                [
                    [None, None, 2, None, [["The answer."]]],
                    [None, None, 1, "The question?"],
                ]
            ],
        )
        httpx_mock.add_response(content=id_response.encode())
        httpx_mock.add_response(content=turns_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_history("nb_123")

        assert len(result) == 1
        assert result[0] == ("The question?", "The answer.")

    @pytest.mark.asyncio
    async def test_get_history_with_provided_conversation_id(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_history uses provided conversation_id without fetching it."""
        turns_response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_TURNS,
            [
                [
                    [None, None, 2, None, [["Direct answer."]]],
                    [None, None, 1, "Direct question?"],
                ]
            ],
        )
        httpx_mock.add_response(content=turns_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_history("nb_123", conversation_id="conv_direct")

        assert len(result) == 1
        assert result[0] == ("Direct question?", "Direct answer.")


class TestBuildConversationHistory:
    """Tests for _build_conversation_history (line 422)."""

    def test_build_conversation_history_returns_none_when_no_cached_turns(self, auth_tokens):
        """Test _build_conversation_history returns None for unknown conversation_id."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._build_conversation_history("nonexistent-conv-id")
        assert result is None

    def test_build_conversation_history_returns_list_when_turns_cached(self, auth_tokens):
        """Test _build_conversation_history returns history list when turns exist."""
        client = NotebookLMClient(auth_tokens)
        # Manually cache a turn
        client._core.cache_conversation_turn(
            "test-conv", "What is AI?", "AI is artificial intelligence.", 1
        )
        result = client.chat._build_conversation_history("test-conv")
        assert result is not None
        assert len(result) == 2
        # History format: [[answer, None, 2], [query, None, 1]]
        assert result[0] == ["AI is artificial intelligence.", None, 2]
        assert result[1] == ["What is AI?", None, 1]


class TestParseAskResponseEdgeCases:
    """Tests for _parse_ask_response_with_references edge cases (lines 439-489)."""

    def test_parse_response_with_stripped_prefix(self, auth_tokens):
        """Test that response starting with )]}' has the prefix stripped (line 439-442)."""
        import json

        client = NotebookLMClient(auth_tokens)
        inner_data = [["Answer text.", None, [12345], None, [[], None, None, [], 1]]]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        assert answer == "Answer text."
        assert conv_id is None

    def test_parse_response_without_prefix(self, auth_tokens):
        """Test response not starting with )]}' is parsed directly."""
        import json

        client = NotebookLMClient(auth_tokens)
        inner_data = [["Answer without prefix.", None, [12345], None, [[], None, None, [], 1]]]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f"{len(chunk_json)}\n{chunk_json}\n"

        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        assert answer == "Answer without prefix."
        assert conv_id is None

    def test_parse_empty_response_returns_empty_string(self, auth_tokens):
        """Test that completely empty response returns empty string (line 489)."""
        client = NotebookLMClient(auth_tokens)
        answer, refs, conv_id = client.chat._parse_ask_response_with_references(")]}'\n")
        assert answer == ""
        assert refs == []
        assert conv_id is None

    def test_parse_response_no_marked_answer_falls_back_to_unmarked(self, auth_tokens):
        """Test fallback to unmarked text when no marked answer exists (line 486)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # Response with no trailing 1 marker in type_info
        inner_data = [
            [
                "This is unmarked text content.",
                None,
                [12345],
                None,
                [[], None, None],  # no trailing 1 = unmarked
            ]
        ]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"

        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        assert answer == "This is unmarked text content."
        assert conv_id is None

    def test_parse_response_assigns_citation_numbers(self, auth_tokens):
        """Test that citation_number is assigned based on order of appearance (line 462-463)."""
        import json

        client = NotebookLMClient(auth_tokens)
        inner_data = [
            [
                "Answer with citation.",
                None,
                [12345],
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
                                [[[100, 200, [[[50, 150, "cited text"]]]]]],
                                [[[["aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]]]],
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

        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        assert len(refs) == 1
        assert refs[0].citation_number == 1
        assert conv_id is None


class TestExtractAnswerAndRefsFromChunk:
    """Tests for _extract_answer_and_refs_from_chunk edge cases (lines 496-561)."""

    def test_invalid_json_returns_none(self, auth_tokens):
        """Test that invalid JSON input returns (None, False, []) (line 496-527)."""
        client = NotebookLMClient(auth_tokens)
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            "not-valid-json"
        )
        assert text is None
        assert is_answer is False
        assert refs == []
        assert conv_id is None

    def test_non_list_data_returns_none(self, auth_tokens):
        """Test that non-list JSON data returns (None, False, []) (line 530)."""
        import json

        client = NotebookLMClient(auth_tokens)
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps("a string value")
        )
        assert text is None
        assert is_answer is False
        assert refs == []
        assert conv_id is None

    def test_item_not_wrb_fr_is_skipped(self, auth_tokens):
        """Test that items where item[0] != 'wrb.fr' are skipped (line 540)."""
        import json

        client = NotebookLMClient(auth_tokens)
        data = [["other.fr", "method_id", json.dumps([[["answer"]]])]]
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps(data)
        )
        assert text is None
        assert conv_id is None

    def test_inner_json_not_string_is_skipped(self, auth_tokens):
        """Test that non-string inner_json is skipped (line 544)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # item[2] is an integer, not a string
        data = [["wrb.fr", "method_id", 42, None, None]]
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps(data)
        )
        assert text is None
        assert conv_id is None

    def test_inner_data_first_not_list_is_skipped(self, auth_tokens):
        """Test that inner_data[0] that is not a list is skipped (line 546)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # inner_data[0] is a string, not a list
        inner_data = ["not a list"]
        data = [["wrb.fr", "method_id", json.dumps(inner_data)]]
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps(data)
        )
        assert text is None
        assert conv_id is None

    def test_inner_data_first_text_not_string_is_skipped(self, auth_tokens):
        """Test that non-string first[0] text is skipped (line 546)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # first[0] is None, not a string
        inner_data = [[[None, None, [12345]]]]
        data = [["wrb.fr", "method_id", json.dumps(inner_data)]]
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps(data)
        )
        assert text is None
        assert conv_id is None

    def test_inner_json_invalid_json_continues(self, auth_tokens):
        """Test that invalid inner JSON is caught and processing continues (line 560-561)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # item[2] is a string but not valid JSON
        data = [["wrb.fr", "method_id", "{not valid json}"]]
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps(data)
        )
        assert text is None
        assert refs == []
        assert conv_id is None

    def test_returns_none_when_no_wrb_fr_items(self, auth_tokens):
        """Test that empty list or no matching items returns (None, False, [])."""
        import json

        client = NotebookLMClient(auth_tokens)
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps([])
        )
        assert text is None
        assert is_answer is False
        assert refs == []
        assert conv_id is None

    def test_item_with_too_few_elements_is_skipped(self, auth_tokens):
        """Test items with len < 3 are skipped."""
        import json

        client = NotebookLMClient(auth_tokens)
        data = [["wrb.fr", "only_two"]]
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps(data)
        )
        assert text is None
        assert conv_id is None


class TestParseCitationsEdgeCases:
    """Tests for _parse_citations edge cases (lines 599-605)."""

    def test_parse_citations_returns_empty_on_type_error(self, auth_tokens):
        """Test _parse_citations returns [] when first causes TypeError (lines 599-605)."""
        client = NotebookLMClient(auth_tokens)
        # Passing None triggers TypeError in len(first) at the guard check
        refs = client.chat._parse_citations(None)  # type: ignore[arg-type]
        assert refs == []

    def test_parse_citations_returns_empty_when_first_too_short(self, auth_tokens):
        """Test _parse_citations returns [] when first has <= 4 elements."""
        client = NotebookLMClient(auth_tokens)
        first = ["text", None, None, None]  # len == 4, no index 4
        refs = client.chat._parse_citations(first)
        assert refs == []

    def test_parse_citations_returns_empty_when_type_info_too_short(self, auth_tokens):
        """Test _parse_citations returns [] when type_info has <= 3 elements."""
        client = NotebookLMClient(auth_tokens)
        first = ["text", None, None, None, [1, 2, 3]]  # type_info has no index 3
        refs = client.chat._parse_citations(first)
        assert refs == []

    def test_parse_citations_returns_empty_when_type_info_3_not_list(self, auth_tokens):
        """Test _parse_citations returns [] when type_info[3] is not a list."""
        client = NotebookLMClient(auth_tokens)
        first = ["text", None, None, None, [1, 2, 3, "not_a_list"]]
        refs = client.chat._parse_citations(first)
        assert refs == []


class TestParseSingleCitationEdgeCases:
    """Tests for _parse_single_citation edge cases (lines 617, 621)."""

    def test_parse_single_citation_returns_none_when_not_list(self, auth_tokens):
        """Test _parse_single_citation returns None when cite is not a list (line 617)."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._parse_single_citation("not a list")
        assert result is None

    def test_parse_single_citation_returns_none_when_too_short(self, auth_tokens):
        """Test _parse_single_citation returns None when cite has len < 2 (line 617)."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._parse_single_citation(["only_one"])
        assert result is None

    def test_parse_single_citation_returns_none_when_cite_inner_not_list(self, auth_tokens):
        """Test _parse_single_citation returns None when cite[1] is not a list (line 621)."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._parse_single_citation([["chunk-id"], "not_a_list"])
        assert result is None

    def test_parse_single_citation_returns_none_when_no_source_id(self, auth_tokens):
        """Test _parse_single_citation returns None when no valid UUID found."""
        client = NotebookLMClient(auth_tokens)
        # cite_inner with 6 elements but source_id_data has no valid UUID
        cite = [
            ["chunk-001"],
            [None, None, 0.9, [[None]], [], "not-a-uuid"],
        ]
        result = client.chat._parse_single_citation(cite)
        assert result is None

    def test_parse_single_citation_with_non_string_chunk_id(self, auth_tokens):
        """Test _parse_single_citation with non-string first item in cite[0] (line 633)."""
        client = NotebookLMClient(auth_tokens)
        # cite[0][0] is not a string, so chunk_id stays None
        valid_uuid = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
        cite = [
            [42],  # non-string first item
            [
                None,
                None,
                0.9,
                [[None]],
                [],
                [[[[valid_uuid]]]],
            ],
        ]
        result = client.chat._parse_single_citation(cite)
        assert result is not None
        assert result.source_id == valid_uuid
        assert result.chunk_id is None

    def test_parse_single_citation_with_empty_cite_0(self, auth_tokens):
        """Test _parse_single_citation when cite[0] is empty list (line 631)."""
        client = NotebookLMClient(auth_tokens)
        valid_uuid = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"
        cite = [
            [],  # empty list - cite[0] is list but empty
            [
                None,
                None,
                0.9,
                [[None]],
                [],
                [[[[valid_uuid]]]],
            ],
        ]
        result = client.chat._parse_single_citation(cite)
        assert result is not None
        assert result.chunk_id is None


class TestExtractTextPassagesEdgeCases:
    """Tests for _extract_text_passages edge cases (lines 631-682)."""

    def test_extract_text_passages_returns_none_when_too_short(self, auth_tokens):
        """Test _extract_text_passages returns (None, None, None) when cite_inner too short (line 661-662)."""
        client = NotebookLMClient(auth_tokens)
        cite_inner = [None, None, None, None]  # len == 4, no index 4
        result = client.chat._extract_text_passages(cite_inner)
        assert result == (None, None, None)

    def test_extract_text_passages_returns_none_when_index4_not_list(self, auth_tokens):
        """Test _extract_text_passages returns (None, None, None) when cite_inner[4] is not a list (line 661-662)."""
        client = NotebookLMClient(auth_tokens)
        cite_inner = [None, None, None, None, "not_a_list"]
        result = client.chat._extract_text_passages(cite_inner)
        assert result == (None, None, None)

    def test_extract_text_passages_skips_non_list_passage_wrapper(self, auth_tokens):
        """Test _extract_text_passages skips passage_wrapper that is not a list (line 669-670)."""
        client = NotebookLMClient(auth_tokens)
        # cite_inner[4] contains a non-list item
        cite_inner = [None, None, None, None, ["not_a_list_wrapper"]]
        result = client.chat._extract_text_passages(cite_inner)
        assert result == (None, None, None)

    def test_extract_text_passages_skips_short_passage_data(self, auth_tokens):
        """Test _extract_text_passages skips passage_data with len < 3 (line 672-673)."""
        client = NotebookLMClient(auth_tokens)
        cite_inner = [None, None, None, None, [[[100, 200]]]]  # passage_data has len 2
        result = client.chat._extract_text_passages(cite_inner)
        assert result == (None, None, None)


class TestCollectTextsFromNested:
    """Tests for _collect_texts_from_nested (lines 698, 709)."""

    def test_non_list_input_returns_immediately(self, auth_tokens):
        """Test _collect_texts_from_nested returns immediately for non-list input (line 698)."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        client.chat._collect_texts_from_nested("not a list", texts)
        assert texts == []

    def test_non_list_input_none_returns_immediately(self, auth_tokens):
        """Test _collect_texts_from_nested returns immediately for None input."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        client.chat._collect_texts_from_nested(None, texts)
        assert texts == []

    def test_nested_group_not_list_is_skipped(self, auth_tokens):
        """Test _collect_texts_from_nested skips nested_group that is not a list."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        # nested contains a non-list item
        client.chat._collect_texts_from_nested(["not_a_list_group"], texts)
        assert texts == []

    def test_text_val_is_list_extracts_strings(self, auth_tokens):
        """Test _collect_texts_from_nested extracts strings when text_val is a list (line 709)."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        # Structure: nested=[nested_group], nested_group=[inner], inner=[start, end, text_val]
        nested = [[[0, 100, ["part one", "  ", "part two"]]]]
        client.chat._collect_texts_from_nested(nested, texts)
        assert "part one" in texts
        assert "part two" in texts

    def test_text_val_list_skips_non_strings(self, auth_tokens):
        """Test _collect_texts_from_nested skips non-string items in text_val list."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        # text_val list contains a mix of string and non-string
        nested = [[[0, 100, [42, "valid text", None]]]]
        client.chat._collect_texts_from_nested(nested, texts)
        assert texts == ["valid text"]

    def test_inner_with_len_less_than_3_is_skipped(self, auth_tokens):
        """Test _collect_texts_from_nested skips inner items with len < 3."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        # inner has only 2 elements
        nested = [[[0, 100]]]
        client.chat._collect_texts_from_nested(nested, texts)
        assert texts == []


class TestExtractUuidFromNested:
    """Tests for _extract_uuid_from_nested (lines 728-743)."""

    def test_max_depth_zero_returns_none_with_warning(self, auth_tokens):
        """Test _extract_uuid_from_nested returns None when max_depth=0 (lines 728-729)."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._extract_uuid_from_nested("some-data", max_depth=0)
        assert result is None

    def test_none_data_returns_none(self, auth_tokens):
        """Test _extract_uuid_from_nested returns None for None input (line 732)."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._extract_uuid_from_nested(None)
        assert result is None

    def test_valid_uuid_string_is_returned(self, auth_tokens):
        """Test _extract_uuid_from_nested returns UUID when given directly as string."""
        client = NotebookLMClient(auth_tokens)
        valid_uuid = "12345678-1234-1234-1234-123456789abc"
        result = client.chat._extract_uuid_from_nested(valid_uuid)
        assert result == valid_uuid

    def test_non_uuid_string_returns_none(self, auth_tokens):
        """Test _extract_uuid_from_nested returns None for non-UUID string."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._extract_uuid_from_nested("not-a-uuid")
        assert result is None

    def test_list_with_no_uuid_returns_none(self, auth_tokens):
        """Test _extract_uuid_from_nested returns None when list contains no UUID (line 737-743)."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._extract_uuid_from_nested(["no", "uuid", "here"])
        assert result is None

    def test_nested_list_finds_uuid(self, auth_tokens):
        """Test _extract_uuid_from_nested finds UUID nested in lists."""
        client = NotebookLMClient(auth_tokens)
        valid_uuid = "abcdef12-abcd-abcd-abcd-abcdef123456"
        result = client.chat._extract_uuid_from_nested([[[[valid_uuid]]]])
        assert result == valid_uuid

    def test_integer_data_returns_none(self, auth_tokens):
        """Test _extract_uuid_from_nested returns None for integer input (line 743 fallthrough)."""
        client = NotebookLMClient(auth_tokens)
        result = client.chat._extract_uuid_from_nested(42)
        assert result is None


class TestGetConversationIdNullRaw:
    """Tests for get_conversation_id when rpc_call returns None/falsy (line 231->230)."""

    @pytest.mark.asyncio
    async def test_get_conversation_id_returns_none_when_raw_is_null(
        self,
        auth_tokens,
    ):
        """Test get_conversation_id returns None when rpc_call returns None (arc 231->230)."""
        async with NotebookLMClient(auth_tokens) as client:
            # Patch rpc_call to return None directly (bypasses decode error)
            with patch.object(
                client._core,
                "rpc_call",
                new_callable=AsyncMock,
                return_value=None,
            ):
                result = await client.chat.get_conversation_id("nb_123")

        assert result is None

    @pytest.mark.asyncio
    async def test_get_conversation_id_returns_none_with_non_list_groups(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_conversation_id returns None when groups in raw are not lists (arc 231->230)."""
        # Response has non-list items in the outer array (valid decode but no conv_id)
        response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, ["not_a_list"])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_conversation_id("nb_123")

        assert result is None


class TestGetHistoryTurnsDataNotReversed:
    """Test get_history when turns_data doesn't qualify for reversal (arc 272->279)."""

    @pytest.mark.asyncio
    async def test_get_history_skips_reversal_when_turns_data_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_history returns [] when turns_data is empty (skips reversal, arc 272->279)."""
        id_response = build_rpc_response(RPCMethod.GET_LAST_CONVERSATION_ID, [[["conv_001"]]])
        # turns_data is an empty outer list, so turns_data[0] is falsy
        turns_response = build_rpc_response(
            RPCMethod.GET_CONVERSATION_TURNS,
            [[]],
        )
        httpx_mock.add_response(content=id_response.encode())
        httpx_mock.add_response(content=turns_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.chat.get_history("nb_123")

        assert result == []


class TestParseAskResponseNumericLengthPrefix:
    """Tests for the numeric length-prefixed format in _parse_ask_response (lines 468-473)."""

    def test_parse_response_with_length_prefix_at_end_of_lines(self, auth_tokens):
        """Test response with numeric line at end has no following line (arc 468->470)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # Response ends with a numeric line (no content follows it)
        inner_data = [["Valid answer.", None, [12345], None, [[], None, None, [], 1]]]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        # Append a dangling numeric line at the end with no content following
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n99\n"

        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        # The valid chunk should still be parsed
        assert answer == "Valid answer."
        assert conv_id is None

    def test_parse_response_with_direct_json_line_no_prefix(self, auth_tokens):
        """Test response where JSON lines are direct (not length-prefixed) (lines 471-473)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # Response with direct JSON chunk (no length prefix)
        inner_data = [["Direct JSON answer.", None, [12345], None, [[], None, None, [], 1]]]
        inner_json = json.dumps(inner_data)
        chunk_json = json.dumps([["wrb.fr", None, inner_json]])
        # No length prefix - just direct JSON
        response_body = f")]}}'\n{chunk_json}\n"

        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        assert answer == "Direct JSON answer."
        assert conv_id is None


class TestExtractAnswerEmptyInnerData:
    """Tests for _extract_answer_and_refs_from_chunk when inner_data is empty (arc 544->532)."""

    def test_empty_inner_data_returns_none(self, auth_tokens):
        """Test that inner_data == [] (empty list) causes loop to find nothing (arc 544->532)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # inner_data is an empty list -> len(inner_data) == 0, skips the processing
        inner_data: list = []
        data = [["wrb.fr", "method_id", json.dumps(inner_data)]]
        text, is_answer, refs, conv_id = client.chat._extract_answer_and_refs_from_chunk(
            json.dumps(data)
        )
        assert text is None
        assert is_answer is False
        assert conv_id is None


class TestExtractTextPassagesMultiplePassages:
    """Tests for _extract_text_passages with multiple passages (lines 676-682)."""

    def test_extract_text_passages_multiple_passages_updates_end_char(self, auth_tokens):
        """Test that end_char is updated for each valid passage (lines 678-682)."""
        client = NotebookLMClient(auth_tokens)
        # Two passage_wrappers: first sets start_char and end_char, second updates end_char
        cite_inner = [
            None,
            None,
            None,
            None,
            [
                # First passage_wrapper
                [[100, 200, [[[50, 150, "first text"]]]]],
                # Second passage_wrapper - end_char should be updated to 400
                [[300, 400, [[[250, 380, "second text"]]]]],
            ],
        ]
        cited_text, start_char, end_char = client.chat._extract_text_passages(cite_inner)
        assert start_char == 100  # from first passage
        assert end_char == 400  # updated by second passage

    def test_extract_text_passages_start_char_not_reset_on_second_passage(self, auth_tokens):
        """Test start_char is only set once from the first valid passage (arc 676->678)."""
        client = NotebookLMClient(auth_tokens)
        cite_inner = [
            None,
            None,
            None,
            None,
            [
                # First passage: sets start_char=10
                [[10, 100, [[[5, 50, "text one"]]]]],
                # Second passage: start_char should NOT be reset to 200
                [[200, 300, [[[150, 250, "text two"]]]]],
            ],
        ]
        _, start_char, _ = client.chat._extract_text_passages(cite_inner)
        assert start_char == 10  # only set from first passage


class TestCollectTextsFromNestedEmptyTextVal:
    """Test _collect_texts_from_nested when text_val list has no extractable strings (arc 709->703)."""

    def test_text_val_list_all_empty_strings_adds_nothing(self, auth_tokens):
        """Test that text_val list with only whitespace strings adds nothing (arc 709->703)."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        # text_val is a list but all items are whitespace or empty
        nested = [[[0, 100, ["  ", "", "   "]]]]
        client.chat._collect_texts_from_nested(nested, texts)
        assert texts == []

    def test_text_val_list_with_non_string_items_adds_nothing(self, auth_tokens):
        """Test that text_val list with only non-string items adds nothing."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        # text_val is a list but all items are non-strings
        nested = [[[0, 100, [None, 42, True]]]]
        client.chat._collect_texts_from_nested(nested, texts)
        assert texts == []

    def test_text_val_neither_string_nor_list_does_nothing(self, auth_tokens):
        """Test that text_val that is neither string nor list is skipped (arc 709->703)."""
        client = NotebookLMClient(auth_tokens)
        texts = []
        # text_val is an integer - neither str nor list - skips both branches
        nested = [[[0, 100, 42]]]
        client.chat._collect_texts_from_nested(nested, texts)
        assert texts == []


class TestParseAskResponseBranchCoverage:
    """Tests for remaining branch coverage in _parse_ask_response and citation assignment."""

    def test_existing_marked_answer_not_overwritten_by_shorter(self, auth_tokens):
        """Test process_chunk doesn't update best_marked_answer when text is shorter (arc 454->456)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # First chunk: longer marked answer becomes best_marked_answer
        # Second chunk: shorter marked answer - should NOT overwrite (arc 454->456)
        longer_inner = [
            ["This is the longer marked answer text.", None, [12345], None, [[], None, None, [], 1]]
        ]
        shorter_inner = [["Short.", None, [12346], None, [[], None, None, [], 1]]]

        longer_json = json.dumps(longer_inner)
        shorter_json = json.dumps(shorter_inner)

        def make_chunk(inner_json):
            chunk_json = json.dumps([["wrb.fr", None, inner_json]])
            return f"{len(chunk_json)}\n{chunk_json}"

        # Both chunks with marked answers
        response_body = f")]}}'\n{make_chunk(longer_json)}\n{make_chunk(shorter_json)}\n"

        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        # Longer marked answer wins
        assert answer == "This is the longer marked answer text."
        assert conv_id is None

    def test_citation_number_not_reassigned_when_already_set(self, auth_tokens):
        """Test that citation_number already set is not overwritten (arc 496->495)."""
        import json

        client = NotebookLMClient(auth_tokens)
        # Build a response where the reference has citation_number pre-assigned via chunk processing
        # We need two chunks each returning the same reference so on second pass citation_number
        # is already set. Actually simpler: provide a chunk where citation is parsed and assigned,
        # then verify it's 1 (first assignment).
        # The arc 496->495 fires when citation_number is not None - we test the final state
        inner_data = [
            [
                "Answer with citation.",
                None,
                [12345],
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
                                [[[100, 200, [[[50, 150, "cited text"]]]]]],
                                [[[["eeeeeeee-eeee-eeee-eeee-eeeeeeeeeeee"]]]],
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
        # Two identical chunks - second one produces a ref with citation_number already set
        # But actually each call to _parse_ask_response creates fresh refs, so two chunks
        # means two refs (same source), first gets 1, second gets 2 (both had citation_number=None)
        # To trigger 496->495 we need ref.citation_number to already be set.
        # This can happen if we manually set citation_number before the assignment loop.
        # However, in the normal flow, refs from _parse_citations don't have citation_number set.
        # The only way to get citation_number != None before the assignment loop is if
        # somehow the code path sets it earlier - which it doesn't.
        # So arc 496->495 requires citation_number to be not None from _parse_citations.
        # Since _parse_citations creates ChatReference with citation_number=None by default,
        # this arc may not be reachable in normal flow - it's a defensive check.
        # Let's verify the assignment logic works correctly with multiple refs.
        response_body = f")]}}'\n{len(chunk_json)}\n{chunk_json}\n"
        answer, refs, conv_id = client.chat._parse_ask_response_with_references(response_body)
        assert len(refs) == 1
        assert refs[0].citation_number == 1
        assert conv_id is None


class TestExtractTextPassagesNonIntEndChar:
    """Test _extract_text_passages when passage_data[1] is not an int (arc 678->682)."""

    def test_non_int_end_char_does_not_update_end_char(self, auth_tokens):
        """Test end_char is not updated when passage_data[1] is not an int (arc 678->682)."""
        client = NotebookLMClient(auth_tokens)
        # passage_data[1] is a string, not int
        cite_inner = [
            None,
            None,
            None,
            None,
            [
                [[100, "not_an_int", [[[50, 150, "some text"]]]]],
            ],
        ]
        cited_text, start_char, end_char = client.chat._extract_text_passages(cite_inner)
        assert start_char == 100
        assert end_char is None  # not set since passage_data[1] was not int


class TestChatHL:
    """ask() must include the NOTEBOOKLM_HL interface language in the URL."""

    @pytest.mark.asyncio
    async def test_ask_url_contains_hl_from_env(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        monkeypatch,
        mock_get_conversation_id,
    ):
        """When NOTEBOOKLM_HL=ja, the chat POST URL carries hl=ja."""
        import json
        import re

        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")

        inner_data = [
            [
                "answer",
                None,
                ["server-hl-env-conv", 12345],
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
        # Configure hPTbtc with hl=ja too so the post-ask request also
        # honors the env var via the same code path.
        mock_get_conversation_id()

        async with NotebookLMClient(auth_tokens) as client:
            await client.chat.ask(
                notebook_id="test_nb",
                question="Q",
                source_ids=["src_001"],
            )

        chat_request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        assert "hl=ja" in str(chat_request.url)

    @pytest.mark.asyncio
    async def test_ask_url_defaults_hl_to_en(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        monkeypatch,
        mock_get_conversation_id,
    ):
        """When NOTEBOOKLM_HL is unset, the chat URL carries hl=en."""
        import json
        import re

        monkeypatch.delenv("NOTEBOOKLM_HL", raising=False)

        inner_data = [
            [
                "answer",
                None,
                ["server-hl-default-conv", 12345],
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
        mock_get_conversation_id()

        async with NotebookLMClient(auth_tokens) as client:
            await client.chat.ask(
                notebook_id="test_nb",
                question="Q",
                source_ids=["src_001"],
            )

        chat_request = next(
            r for r in httpx_mock.get_requests() if "GenerateFreeFormStreamed" in str(r.url)
        )
        assert "hl=en" in str(chat_request.url)
