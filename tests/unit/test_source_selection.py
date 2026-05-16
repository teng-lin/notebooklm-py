"""Unit tests for multi-source selection in chat and artifact generation.

Tests that source_ids are correctly handled when:
1. Explicitly passed (subset of sources)
2. None (uses all sources via core.get_source_ids)

Verifies correct encoding of source IDs in RPC parameters:
- source_ids_triple = [[[sid]] for sid in source_ids]
- source_ids_double = [[sid] for sid in source_ids]
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._chat import ChatAPI
from notebooklm.exceptions import ValidationError
from notebooklm.rpc import InfographicStyle, VideoFormat, VideoStyle


@pytest.fixture
def mock_core():
    """Create a mock ClientCore.

    After Tier-2 T2.D, ``ChatAPI.ask`` goes through ``core.query_post`` and
    ``core.next_reqid`` instead of the legacy direct ``post`` / counter
    mutation. Tests that need to assert on URL or body now drive
    ``query_post`` via its ``build_request`` factory rather than poking the
    raw httpx client.
    """
    from notebooklm._core import _AuthSnapshot

    core = MagicMock()

    # ``ChatAPI.get_conversation_id`` uses ``core.rpc_call`` with the
    # ``hPTbtc`` (GET_LAST_CONVERSATION_ID) method. Issue #659: after a
    # new-conversation ask, ``ChatAPI.ask`` calls this to recover the real
    # conversation_id. Route only that method to a hPTbtc-shaped reply;
    # every other RPC honors ``mock_core.rpc_call.return_value`` so the
    # artifact tests in this module (which set ``return_value`` per call)
    # are unaffected.
    from notebooklm.rpc import RPCMethod as _RPC

    core.rpc_call = AsyncMock(return_value=MagicMock())

    async def _rpc_call_dispatch(method, params, *, source_path=None, allow_null=False):
        if method == _RPC.GET_LAST_CONVERSATION_ID:
            return [[["mock-core-conv-id"]]]
        return core.rpc_call.return_value

    core.rpc_call.side_effect = _rpc_call_dispatch
    core.get_source_ids = AsyncMock(return_value=[])
    core.auth = MagicMock()
    core.auth.csrf_token = "test_csrf"
    core.auth.session_id = "test_session"
    core.auth.authuser = 0
    core.auth.account_email = None
    # Reqid counter is now bumped via ``await core.next_reqid()``; the
    # ``_reqid_counter`` attribute remains for backwards-compat assertions.
    core._reqid_counter = 0
    core.next_reqid = AsyncMock(return_value=100000)
    core.get_http_client = MagicMock()
    core.get_cached_conversation = MagicMock(return_value=[])
    core.cache_conversation_turn = MagicMock()

    # Default ``query_post`` stub: invokes the caller-supplied
    # ``build_request`` factory with a frozen snapshot (so the URL/body the
    # test wants to assert on actually gets assembled) and returns a stock
    # answer response. Individual tests that need to inspect the URL/body
    # can read ``core._last_chat_request`` after calling ``ChatAPI.ask``.
    async def _query_post_default(*, build_request, parse_label):
        snapshot = _AuthSnapshot(
            csrf_token=core.auth.csrf_token,
            session_id=core.auth.session_id,
            authuser=core.auth.authuser,
            account_email=core.auth.account_email,
        )
        url, body, headers = build_request(snapshot)
        core._last_chat_request = {"url": url, "body": body, "headers": headers}
        resp = MagicMock()
        # ``first[2][0]`` carries the server-assigned conversation_id; new
        # conversations require this slot (issue #659).
        inner = json.dumps(
            [
                [
                    "Default answer long enough to be valid.",
                    None,
                    ["server-source-selection-conv", 12345],
                    None,
                    [1],
                ]
            ]
        )
        chunk = json.dumps([["wrb.fr", None, inner]])
        resp.text = f")]}}'\n{len(chunk)}\n{chunk}\n"
        return resp

    core.query_post = AsyncMock(side_effect=_query_post_default)
    return core


@pytest.fixture
def mock_notes_api():
    """Placeholder for the legacy ``ArtifactsAPI(core, notes_api)`` signature.

    After T6.F, ``ArtifactsAPI`` no longer reads ``notes_api`` (it consumes
    the shared ``_mind_map`` module directly). The arg is still accepted for
    backward compatibility and immediately discarded inside ``__init__``, so
    this fixture intentionally returns a bare mock — no method stubs, since
    none of the downstream code paths under test invoke any methods on it.
    Future readers: if you find yourself adding ``AsyncMock`` setup here,
    you probably want to drop the second positional arg from the call sites
    instead.
    """
    return MagicMock()


class TestChatSourceSelection:
    """Tests for source selection in ChatAPI.ask()."""

    @pytest.mark.asyncio
    async def test_ask_with_explicit_source_ids(self, mock_core):
        """Test ask() with explicitly provided source_ids."""
        api = ChatAPI(mock_core)

        result = await api.ask(
            notebook_id="nb_123",
            question="Test question?",
            source_ids=["src_001", "src_002"],
        )

        assert result.answer == "Default answer long enough to be valid."

        # query_post is the transport entry point; the request body is
        # captured into ``_last_chat_request`` by the mock_core fixture.
        body = mock_core._last_chat_request["body"]

        # The body should contain the encoded sources_array
        # sources_array = [[[sid]] for sid in source_ids]
        # For ["src_001", "src_002"], this becomes [[["src_001"]], [["src_002"]]]
        assert "src_001" in body
        assert "src_002" in body

    @pytest.mark.asyncio
    async def test_ask_with_none_fetches_all_sources(self, mock_core):
        """Test ask() with source_ids=None fetches all sources."""
        api = ChatAPI(mock_core)

        # Mock get_source_ids to return source IDs
        mock_core.get_source_ids.return_value = ["src_001", "src_002", "src_003"]

        result = await api.ask(
            notebook_id="nb_123",
            question="Test question?",
            source_ids=None,  # Should fetch all sources
        )

        assert result.answer == "Default answer long enough to be valid."

        # Verify get_source_ids was called on core
        mock_core.get_source_ids.assert_called_once_with("nb_123")

    @pytest.mark.asyncio
    async def test_ask_source_encoding_format(self, mock_core):
        """Verify the correct encoding format for source IDs in ask()."""
        api = ChatAPI(mock_core)

        await api.ask(
            notebook_id="nb_123",
            question="Test?",
            source_ids=["s1", "s2", "s3"],
        )

        # query_post should have been called once with a build_request factory
        # that produces the URL-encoded body with the triple-nested sources.
        mock_core.query_post.assert_called_once()
        body = mock_core._last_chat_request["body"]

        # The body contains URL-encoded f.req parameter
        # sources_array should be [[["s1"]], [["s2"]], [["s3"]]]
        # This gets encoded in the params as the first element
        # Extract f.req from body
        import re
        from urllib.parse import unquote

        match = re.search(r"f\.req=([^&]+)", body)
        assert match, f"f.req= missing from body: {body!r}"
        f_req_encoded = match.group(1)
        f_req_decoded = unquote(f_req_encoded)
        f_req_data = json.loads(f_req_decoded)
        # f_req is [None, params_json]
        params = json.loads(f_req_data[1])
        sources_array = params[0]

        # Verify the triple-nested format
        assert sources_array == [[["s1"]], [["s2"]], [["s3"]]]


class TestArtifactsSourceSelection:
    """Tests for source selection in ArtifactsAPI generation methods."""

    @pytest.mark.asyncio
    async def test_generate_audio_with_explicit_source_ids(self, mock_core, mock_notes_api):
        """Test generate_audio with explicitly provided source_ids."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock successful generation response
        mock_core.rpc_call.return_value = [
            ["artifact_123", "Audio", 1, None, 1]  # status 1 = in_progress
        ]

        result = await api.generate_audio(
            notebook_id="nb_123",
            source_ids=["src_001", "src_002"],
        )

        assert result.task_id == "artifact_123"
        assert result.status == "in_progress"

        # Verify RPC was called with correct source encoding
        mock_core.rpc_call.assert_called_once()
        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # params structure for audio:
        # [
        #   [2],
        #   notebook_id,
        #   [
        #     None, None, 1,  # type = audio
        #     source_ids_triple,  # [[[sid]] for sid]
        #     None, None,
        #     [None, [instructions, length_code, None, source_ids_double, language, None, format_code]]
        #   ]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        source_ids_double = inner_params[6][1][3]

        assert source_ids_triple == [[["src_001"]], [["src_002"]]]
        assert source_ids_double == [["src_001"], ["src_002"]]

    @pytest.mark.asyncio
    async def test_generate_audio_with_none_fetches_all_sources(self, mock_core, mock_notes_api):
        """Test generate_audio with source_ids=None fetches all sources."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock get_source_ids to return source IDs
        mock_core.get_source_ids.return_value = ["src_001", "src_002"]

        # Mock the generation RPC call
        mock_core.rpc_call.return_value = [["artifact_123", "Audio", 1, None, 1]]

        result = await api.generate_audio(
            notebook_id="nb_123",
            source_ids=None,
        )

        assert result.task_id == "artifact_123"

        # Verify get_source_ids was called
        mock_core.get_source_ids.assert_called_once_with("nb_123")

        # Verify CREATE_ARTIFACT RPC was called with fetched source IDs
        mock_core.rpc_call.assert_called_once()
        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]
        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_001"]], [["src_002"]]]

    @pytest.mark.asyncio
    async def test_generate_video_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_video has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_456", "Video", 3, None, 1]]

        await api.generate_video(
            notebook_id="nb_123",
            source_ids=["src_a", "src_b"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # Video params structure:
        # [
        #   [2], notebook_id,
        #   [None, None, 3, source_ids_triple, None, None, None, None,
        #    [None, None, [source_ids_double, language, instructions, None, format_code, style_code]]]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        video_config = inner_params[8][2]
        source_ids_double = video_config[0]

        assert source_ids_triple == [[["src_a"]], [["src_b"]]]
        assert source_ids_double == [["src_a"], ["src_b"]]

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_prompt_encoding(self, mock_core, mock_notes_api):
        """Test custom video style prompt is encoded after the style code."""
        api = ArtifactsAPI(mock_core, mock_notes_api)
        mock_core.rpc_call.return_value = [["artifact_456", "Video", 3, None, 1]]

        await api.generate_video(
            notebook_id="nb_123",
            source_ids=["src_a"],
            video_style=VideoStyle.CUSTOM,
            style_prompt="  Use hand-drawn diagrams  ",
        )

        params = mock_core.rpc_call.call_args.args[1]
        video_config = params[2][8][2]
        assert video_config[5] == VideoStyle.CUSTOM.value
        assert video_config[6] == "Use hand-drawn diagrams"

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_requires_prompt(self, mock_core, mock_notes_api):
        api = ArtifactsAPI(mock_core, mock_notes_api)

        with pytest.raises(ValidationError, match="style_prompt is required"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.CUSTOM,
            )

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_rejects_empty_prompt(
        self, mock_core, mock_notes_api
    ):
        api = ArtifactsAPI(mock_core, mock_notes_api)

        with pytest.raises(ValidationError, match="style_prompt is required"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.CUSTOM,
                style_prompt="",
            )

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_rejects_blank_prompt(
        self, mock_core, mock_notes_api
    ):
        api = ArtifactsAPI(mock_core, mock_notes_api)

        with pytest.raises(ValidationError, match="style_prompt is required"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.CUSTOM,
                style_prompt="   ",
            )

    @pytest.mark.asyncio
    async def test_generate_video_style_prompt_requires_custom_style(
        self, mock_core, mock_notes_api
    ):
        api = ArtifactsAPI(mock_core, mock_notes_api)

        with pytest.raises(ValidationError, match="style_prompt requires"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.ANIME,
                style_prompt="Use hand-drawn diagrams",
            )

    @pytest.mark.asyncio
    async def test_generate_video_cinematic_rejects_style_prompt(self, mock_core, mock_notes_api):
        api = ArtifactsAPI(mock_core, mock_notes_api)

        with pytest.raises(ValidationError, match="cinematic"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_format=VideoFormat.CINEMATIC,
                style_prompt="Use hand-drawn diagrams",
            )

    @pytest.mark.asyncio
    async def test_generate_report_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_report has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_789", "Report", 2, None, 1]]

        await api.generate_report(
            notebook_id="nb_123",
            source_ids=["src_x", "src_y", "src_z"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # Report params structure:
        # [
        #   [2], notebook_id,
        #   [None, None, 2, source_ids_triple, None, None, None,
        #    [None, [title, desc, None, source_ids_double, language, prompt, None, True]]]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        report_config = inner_params[7][1]
        source_ids_double = report_config[3]

        assert source_ids_triple == [[["src_x"]], [["src_y"]], [["src_z"]]]
        assert source_ids_double == [["src_x"], ["src_y"], ["src_z"]]

    @pytest.mark.asyncio
    async def test_generate_report_extra_instructions_appended(self, mock_core, mock_notes_api):
        """extra_instructions is appended to the built-in prompt with \\n\\n separator."""
        api = ArtifactsAPI(mock_core, mock_notes_api)
        mock_core.rpc_call.return_value = [["artifact_789", "Report", 2, None, 1]]

        await api.generate_report(
            notebook_id="nb_123",
            source_ids=["src_x"],
            extra_instructions="Focus on financial metrics",
        )

        params = mock_core.rpc_call.call_args.args[1]
        report_config = params[2][7][1]
        prompt = report_config[5]

        assert "Focus on financial metrics" in prompt
        assert "\n\nFocus on financial metrics" in prompt

    @pytest.mark.asyncio
    async def test_generate_report_extra_instructions_ignored_for_custom(
        self, mock_core, mock_notes_api
    ):
        """extra_instructions has no effect when report_format is CUSTOM."""
        from notebooklm.rpc.types import ReportFormat

        api = ArtifactsAPI(mock_core, mock_notes_api)
        mock_core.rpc_call.return_value = [["artifact_789", "Report", 2, None, 1]]

        await api.generate_report(
            notebook_id="nb_123",
            source_ids=["src_x"],
            report_format=ReportFormat.CUSTOM,
            custom_prompt="My custom prompt",
            extra_instructions="Should be ignored",
        )

        params = mock_core.rpc_call.call_args.args[1]
        report_config = params[2][7][1]
        prompt = report_config[5]

        assert "Should be ignored" not in prompt
        assert prompt == "My custom prompt"

    @pytest.mark.asyncio
    async def test_generate_quiz_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_quiz has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_quiz", "Quiz", 4, None, 1]]

        await api.generate_quiz(
            notebook_id="nb_123",
            source_ids=["src_1", "src_2"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        # Quiz params structure:
        # [
        #   [2], notebook_id,
        #   [None, None, 4, source_ids_triple, ...]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_1"]], [["src_2"]]]

    @pytest.mark.asyncio
    async def test_generate_flashcards_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_flashcards has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_fc", "Flashcards", 4, None, 1]]

        await api.generate_flashcards(
            notebook_id="nb_123",
            source_ids=["src_flash"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_flash"]]]

    @pytest.mark.asyncio
    async def test_generate_infographic_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_infographic has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_info", "Infographic", 7, None, 1]]

        await api.generate_infographic(
            notebook_id="nb_123",
            source_ids=["src_info_1", "src_info_2"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_info_1"]], [["src_info_2"]]]

    @pytest.mark.asyncio
    async def test_generate_infographic_style_encoding(self, mock_core, mock_notes_api):
        """Test generate_infographic encodes style in config slot 5."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_info", "Infographic", 7, None, 1]]

        await api.generate_infographic(
            notebook_id="nb_123",
            source_ids=["src_info_1"],
            style=InfographicStyle.PROFESSIONAL,
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        infographic_config = inner_params[14][0]

        assert infographic_config[5] == InfographicStyle.PROFESSIONAL.value

    @pytest.mark.asyncio
    async def test_generate_slide_deck_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_slide_deck has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_slide", "Slides", 8, None, 1]]

        await api.generate_slide_deck(
            notebook_id="nb_123",
            source_ids=["src_slide"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_slide"]]]

    @pytest.mark.asyncio
    async def test_generate_data_table_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_data_table has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_table", "Table", 9, None, 1]]

        await api.generate_data_table(
            notebook_id="nb_123",
            source_ids=["src_table_1", "src_table_2"],
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_table_1"]], [["src_table_2"]]]

    @pytest.mark.asyncio
    async def test_generate_mind_map_source_encoding(self, mock_core, mock_notes_api):
        """Test generate_mind_map has correct source encoding format."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock get_source_ids to return source IDs
        mock_core.get_source_ids.return_value = ["src_mm_1", "src_mm_2"]

        # Mock the mind map generation RPC call
        mock_core.rpc_call.return_value = [['{"name": "Mind Map", "children": []}']]

        await api.generate_mind_map(
            notebook_id="nb_123",
            source_ids=None,  # Will fetch sources
        )

        # Verify get_source_ids was called
        mock_core.get_source_ids.assert_called_once_with("nb_123")

        # After T6.F, ``generate_mind_map`` also drives the CREATE_NOTE +
        # UPDATE_NOTE calls itself (previously delegated to NotesAPI), so
        # rpc_call is invoked three times. The source-encoding assertion
        # targets the GENERATE_MIND_MAP call specifically.
        generate_call = next(
            c for c in mock_core.rpc_call.call_args_list if c.args[0].name == "GENERATE_MIND_MAP"
        )
        params = generate_call.args[1]

        # Mind map uses source_ids_nested = [[[sid]] for sid]
        source_ids_nested = params[0]

        assert source_ids_nested == [[["src_mm_1"]], [["src_mm_2"]]]

    @pytest.mark.asyncio
    async def test_generate_mind_map_passes_language_and_instructions(
        self, mock_core, mock_notes_api
    ):
        """Test generate_mind_map passes language and instructions to RPC payload."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.get_source_ids.return_value = ["src_1"]
        mock_core.rpc_call.return_value = [['{"name": "Mind Map", "children": []}']]

        await api.generate_mind_map(
            notebook_id="nb_123",
            source_ids=["src_1"],
            language="ja",
            instructions="Focus on key themes",
        )

        # Pick the GENERATE_MIND_MAP call specifically — CREATE_NOTE and
        # UPDATE_NOTE are now invoked alongside it.
        generate_call = next(
            c for c in mock_core.rpc_call.call_args_list if c.args[0].name == "GENERATE_MIND_MAP"
        )
        params = generate_call.args[1]

        # params[5] should contain the mind map config with language and instructions
        mind_map_config = params[5]
        assert mind_map_config[1][0][1] == "Focus on key themes"
        assert mind_map_config[2] == "ja"

    @pytest.mark.asyncio
    async def test_suggest_reports_uses_get_suggested_reports(self, mock_core, mock_notes_api):
        """Test suggest_reports uses GET_SUGGESTED_REPORTS RPC."""
        from notebooklm.rpc.types import RPCMethod

        api = ArtifactsAPI(mock_core, mock_notes_api)

        # Mock the GET_SUGGESTED_REPORTS RPC call
        # Response format: [[[title, description, null, null, prompt, audience_level], ...]]
        mock_core.rpc_call.return_value = [
            [["Report Title", "Description", None, None, "Custom prompt", 2]]
        ]

        result = await api.suggest_reports(notebook_id="nb_123")

        # Verify GET_SUGGESTED_REPORTS was called with correct params
        mock_core.rpc_call.assert_called_once()
        call_args = mock_core.rpc_call.call_args
        assert call_args.args[0] == RPCMethod.GET_SUGGESTED_REPORTS
        assert call_args.args[1] == [[2], "nb_123"]

        # Verify result parsing
        assert len(result) == 1
        assert result[0].title == "Report Title"


class TestEmptySourceIds:
    """Tests for edge cases with empty source lists."""

    @pytest.mark.asyncio
    async def test_generate_with_empty_source_list(self, mock_core, mock_notes_api):
        """Test generation with empty source_ids list produces empty arrays."""
        api = ArtifactsAPI(mock_core, mock_notes_api)

        mock_core.rpc_call.return_value = [["artifact_empty", "Audio", 1, None, 1]]

        await api.generate_audio(
            notebook_id="nb_123",
            source_ids=[],  # Explicit empty list
        )

        call_args = mock_core.rpc_call.call_args
        params = call_args.args[1]
        inner_params = params[2]

        source_ids_triple = inner_params[3]
        source_ids_double = inner_params[6][1][3]

        # Empty list should produce empty arrays
        assert source_ids_triple == []
        assert source_ids_double == []

    @pytest.mark.asyncio
    async def test_ask_with_empty_source_list(self, mock_core):
        """Test ask with empty source_ids list."""
        api = ChatAPI(mock_core)

        await api.ask(
            notebook_id="nb_123",
            question="Test?",
            source_ids=[],
        )

        # Verify the sources_array is empty in the request
        body = mock_core._last_chat_request["body"]

        import re
        from urllib.parse import unquote

        match = re.search(r"f\.req=([^&]+)", body)
        assert match, f"f.req= missing from body: {body!r}"
        f_req_encoded = match.group(1)
        f_req_decoded = unquote(f_req_encoded)
        f_req_data = json.loads(f_req_decoded)
        params = json.loads(f_req_data[1])
        sources_array = params[0]

        assert sources_array == []


class TestGetSourceIds:
    """Tests for ClientCore.get_source_ids method."""

    @pytest.mark.asyncio
    async def test_get_source_ids_extracts_correctly(self, auth_tokens):
        """Test get_source_ids correctly extracts source IDs from notebook data."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        # Mock notebook data with multiple sources
        # Structure: notebook_data[0][1] = sources list
        # Each source: [["source_id"], "Source Title", ...]
        core.rpc_call.return_value = [
            [
                "nb_123",  # notebook_info[0]
                [
                    # sources list - source[0] is ["id"], source[0][0] is the id
                    [["source_aaa"], "Source A Title"],
                    [["source_bbb"], "Source B Title"],
                    [["source_ccc"], "Source C Title"],
                ],
            ]
        ]

        source_ids = await core.get_source_ids("nb_123")

        assert source_ids == ["source_aaa", "source_bbb", "source_ccc"]

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_empty_notebook(self, auth_tokens):
        """Test get_source_ids handles notebook with no sources."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        core.rpc_call.return_value = [["nb_123", []]]

        source_ids = await core.get_source_ids("nb_123")

        assert source_ids == []

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_null_response(self, auth_tokens):
        """Test get_source_ids handles null API response."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        core.rpc_call.return_value = None

        source_ids = await core.get_source_ids("nb_123")

        assert source_ids == []

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_malformed_data(self, auth_tokens):
        """Test get_source_ids handles malformed source data gracefully."""
        from notebooklm._core import ClientCore

        core = ClientCore(auth_tokens)
        core.rpc_call = AsyncMock()

        # Malformed data - missing nested structure
        # Structure: source[0] must be a list, source[0][0] must be a string
        core.rpc_call.return_value = [
            [
                "nb_123",
                [
                    [None, "Missing ID"],  # Invalid: source[0] is None
                    [["valid_id"], "Valid Source"],  # Valid
                    "not a list",  # Invalid: not a list at all
                ],
            ]
        ]

        source_ids = await core.get_source_ids("nb_123")

        # Should only extract the valid source
        assert source_ids == ["valid_id"]
