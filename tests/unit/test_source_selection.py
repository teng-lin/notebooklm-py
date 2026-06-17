"""Unit tests for multi-source selection in chat and artifact generation.

Tests that source_ids are correctly handled when:
1. Explicitly passed (subset of sources)
2. None (uses all sources via NotebooksAPI.get_source_ids)

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
from notebooklm.rpc import (
    AudioFormat,
    AudioLength,
    InfographicDetail,
    InfographicOrientation,
    InfographicStyle,
    QuizDifficulty,
    QuizQuantity,
    SlideDeckFormat,
    SlideDeckLength,
    VideoFormat,
    VideoStyle,
)


@pytest.fixture
def mock_core():
    """Create a mock Session.

    After Wave 8 of session-decoupling, ``ChatAPI.ask`` reaches the network
    through its injected :class:`RuntimeTransport` collaborator via
    ``self._transport.perform_authed_post`` (constructor-injected by the
    ``_chat_from_mock_core`` helper below, which maps the bag-of-attributes
    ``mock_core`` fixture onto the four keyword-only collaborator slots).
    The fixture stubs ``mock_core.session_transport.perform_authed_post`` —
    that ``AsyncMock`` is the value ``_chat_from_mock_core`` passes as
    ``transport=`` — and invokes the caller-supplied ``build_request``
    factory so URL/body assertions still exercise the production request
    builder.
    """
    from types import SimpleNamespace

    from notebooklm._request_types import AuthSnapshot

    # ``ChatAPI.get_conversation_id`` calls ``rpc_executor.rpc_call`` with
    # the ``hPTbtc`` (GET_LAST_CONVERSATION_ID) method. Issue #659: after a
    # new-conversation ask, ``ChatAPI.ask`` calls this to recover the real
    # conversation_id. Route only that method to a hPTbtc-shaped reply;
    # every other RPC honors ``rpc_call.return_value`` so the artifact
    # tests in this module (which set ``return_value`` per call) are
    # unaffected.
    from notebooklm.rpc import RPCMethod as _RPC

    rpc_call = AsyncMock(return_value=MagicMock())

    # Forward declare so the dispatcher and default-transport closures can
    # capture the eventual ``core`` symbol. The actual ``SimpleNamespace``
    # assembly happens further below, after both closures are defined.
    auth = SimpleNamespace(
        csrf_token="test_csrf",
        session_id="test_session",
        authuser=0,
        account_email=None,
    )

    async def _rpc_call_dispatch(method, params, **kwargs):
        if method == _RPC.GET_LAST_CONVERSATION_ID:
            return [[["mock-core-conv-id"]]]
        return rpc_call.return_value

    rpc_call.side_effect = _rpc_call_dispatch

    # Default ``perform_authed_post`` stub on the session-transport
    # collaborator: invokes the caller-supplied ``build_request`` factory
    # with a frozen snapshot (so the URL/body the test wants to assert on
    # actually gets assembled) and returns a stock answer response.
    # Individual tests that need to inspect the URL/body can read
    # ``core._last_chat_request`` after calling ``ChatAPI.ask``. The
    # chat-side ``parse_label`` is forwarded as ``log_label``.
    async def _perform_authed_post_default(
        *,
        build_request,
        log_label,
        read_timeout=None,
        disable_read_timeout_retries=False,
    ):
        snapshot = AuthSnapshot(
            csrf_token=auth.csrf_token,
            session_id=auth.session_id,
            authuser=auth.authuser,
            account_email=auth.account_email,
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

    # Assemble the bag-of-attributes fixture in one ``SimpleNamespace`` call
    # so every collaborator slot ``ChatAPI`` and ``ArtifactsAPI`` read from
    # the fixture lands at construction time. ADR-0007 specifically forbids
    # the ``core.<attr> = <value>`` re-assignment pattern (which is why this
    # is *not* built via ``make_fake_core`` + post-construction stubs); the
    # SimpleNamespace constructor satisfies the policy by setting every
    # attribute up-front.
    #
    # Wave 8 of session-decoupling: chat now reaches the network through
    # ``session_transport.perform_authed_post`` rather than the legacy
    # ``transport_post`` facade on Session. Reqid is bumped via
    # ``await self._reqid.next_reqid()``; the bag below is passed as the
    # ``reqid=`` collaborator by ``_chat_from_mock_core``.
    session_transport = SimpleNamespace(
        perform_authed_post=AsyncMock(side_effect=_perform_authed_post_default),
    )

    # ArtifactsAPI uses the same fixture as its runtime collaborator and
    # exercises ``register_drain_hook`` (close-time hook) and
    # ``operation_scope`` (drain-coordinated scope). Stub both up-front so
    # the fixture satisfies both ChatAPI's reqid/transport surfaces and
    # ArtifactsAPI's runtime surface in one bag.
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _operation_scope_factory(label: str):
        yield None

    drain_hooks: dict = {}

    def _register_drain_hook(name: str, hook):
        drain_hooks[name] = hook

    core = SimpleNamespace(
        rpc_executor=SimpleNamespace(rpc_call=rpc_call),
        # ``rpc_call`` mirrors ``rpc_executor.rpc_call`` so the SimpleNamespace
        # also satisfies the composite ``ArtifactsRuntime`` shape
        # (``RpcCaller`` + ``LoopGuard`` + ``OperationScopeProvider`` +
        # ``DrainHookRegistration``) when threaded into ``ArtifactsAPI`` as
        # the runtime adapter.
        rpc_call=rpc_call,
        auth=auth,
        next_reqid=AsyncMock(return_value=100000),
        assert_bound_loop=MagicMock(return_value=None),
        get_http_client=MagicMock(),
        session_transport=session_transport,
        _last_chat_request=None,
        operation_scope=MagicMock(side_effect=_operation_scope_factory),
        register_drain_hook=MagicMock(side_effect=_register_drain_hook),
        _drain_hooks=drain_hooks,
    )
    return core


@pytest.fixture
def mock_notebooks_api():
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[])
    return notebooks


def _chat_from_mock_core(mock_core, *, notebooks=None) -> ChatAPI:
    """Build a ``ChatAPI`` from the ``mock_core`` fixture's surfaces.

    Wave 8 of session-decoupling (ADR-0014 Rule 2 Corollary): ``ChatAPI``
    takes its four direct collaborators by keyword arg. The legacy single-
    arg ``ChatAPI(mock_core)`` form is gone; this helper preserves the
    test shape by mapping the bag-of-attributes mock_core fixture onto
    the new constructor surface (rpc, transport, reqid, loop_guard).
    Tests pass ``mock_core.rpc_executor.rpc_call`` for ``rpc.rpc_call`` and the
    fixture's pre-wired ``mock_core.session_transport.perform_authed_post``
    for the transport entry point.
    """
    return ChatAPI(
        rpc=mock_core.rpc_executor,
        transport=mock_core.session_transport,
        reqid=mock_core,
        loop_guard=mock_core,
        notebooks=notebooks,
    )


@pytest.fixture
def mock_mind_map_service():
    """Bundle of stand-in services required by ``ArtifactsAPI.__init__``.

    These tests exercise generation/encoding paths that never call the
    mind-map services. The ``mind_maps`` + ``note_service`` parameters
    are both required (Phase 5 / refactor-history.md Migration Plan steps 6-7)
    so we return a dict of stand-in mocks that construction sites can
    splat into ``ArtifactsAPI(...)`` calls via
    ``**mock_mind_map_service``.
    """
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    return {
        "mind_maps": MagicMock(spec=NoteBackedMindMapService),
        "note_service": MagicMock(spec=NoteService),
    }


class TestChatSourceSelection:
    """Tests for source selection in ChatAPI.ask()."""

    @pytest.mark.asyncio
    async def test_ask_with_explicit_source_ids(self, mock_core):
        """Test ask() with explicitly provided source_ids."""
        api = _chat_from_mock_core(mock_core)

        result = await api.ask(
            notebook_id="nb_123",
            question="Test question?",
            source_ids=["src_001", "src_002"],
        )

        assert result.answer == "Default answer long enough to be valid."

        # session_transport.perform_authed_post is the session entry point;
        # the request body is captured into ``_last_chat_request`` by the
        # mock_core fixture.
        body = mock_core._last_chat_request["body"]

        # The body should contain the encoded sources_array
        # sources_array = [[[sid]] for sid in source_ids]
        # For ["src_001", "src_002"], this becomes [[["src_001"]], [["src_002"]]]
        assert "src_001" in body
        assert "src_002" in body

    @pytest.mark.asyncio
    async def test_ask_with_none_fetches_all_sources(self, mock_core, mock_notebooks_api):
        """Test ask() with source_ids=None fetches all sources."""
        api = _chat_from_mock_core(mock_core, notebooks=mock_notebooks_api)

        # Mock get_source_ids to return source IDs
        mock_notebooks_api.get_source_ids.return_value = ["src_001", "src_002", "src_003"]

        result = await api.ask(
            notebook_id="nb_123",
            question="Test question?",
            source_ids=None,  # Should fetch all sources
        )

        assert result.answer == "Default answer long enough to be valid."

        # Verify get_source_ids was called on notebooks API
        mock_notebooks_api.get_source_ids.assert_called_once_with("nb_123")

    @pytest.mark.asyncio
    async def test_ask_source_encoding_format(self, mock_core):
        """Verify the correct encoding format for source IDs in ask()."""
        api = _chat_from_mock_core(mock_core)

        await api.ask(
            notebook_id="nb_123",
            question="Test?",
            source_ids=["s1", "s2", "s3"],
        )

        # session_transport.perform_authed_post should have been called once
        # with a build_request factory that produces the URL-encoded body
        # with the triple-nested sources.
        mock_core.session_transport.perform_authed_post.assert_awaited_once()
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
    async def test_generate_audio_with_explicit_source_ids(self, mock_core, mock_mind_map_service):
        """Test generate_audio with explicitly provided source_ids."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        # Mock successful generation response
        mock_core.rpc_executor.rpc_call.return_value = [
            ["artifact_123", "Audio", 1, None, 1]  # status 1 = in_progress
        ]

        result = await api.generate_audio(
            notebook_id="nb_123",
            source_ids=["src_001", "src_002"],
        )

        assert result.task_id == "artifact_123"
        assert result.status == "in_progress"

        # Verify RPC was called with correct source encoding
        mock_core.rpc_executor.rpc_call.assert_called_once()
        call_args = mock_core.rpc_executor.rpc_call.call_args
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
        audio_config = inner_params[6][1]
        source_ids_double = audio_config[3]

        assert source_ids_triple == [[["src_001"]], [["src_002"]]]
        assert source_ids_double == [["src_001"], ["src_002"]]
        assert audio_config[1] == AudioLength.DEFAULT.value
        assert audio_config[6] == AudioFormat.DEEP_DIVE.value

    @pytest.mark.asyncio
    async def test_generate_audio_explicit_options_override_defaults(
        self, mock_core, mock_mind_map_service
    ):
        """Explicit audio format and length are encoded instead of API defaults."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_123", "Audio", 1, None, 1]]

        await api.generate_audio(
            notebook_id="nb_123",
            source_ids=["src_001"],
            audio_format=AudioFormat.DEBATE,
            audio_length=AudioLength.LONG,
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        audio_config = params[2][6][1]
        assert audio_config[1] == AudioLength.LONG.value
        assert audio_config[6] == AudioFormat.DEBATE.value

    @pytest.mark.asyncio
    async def test_generate_audio_with_none_fetches_all_sources(
        self, mock_core, mock_mind_map_service, mock_notebooks_api
    ):
        """Test generate_audio with source_ids=None fetches all sources."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=mock_notebooks_api,
            **mock_mind_map_service,
        )

        # Mock get_source_ids to return source IDs
        mock_notebooks_api.get_source_ids.return_value = ["src_001", "src_002"]

        # Mock the generation RPC call
        mock_core.rpc_executor.rpc_call.return_value = [["artifact_123", "Audio", 1, None, 1]]

        result = await api.generate_audio(
            notebook_id="nb_123",
            source_ids=None,
        )

        assert result.task_id == "artifact_123"

        # Verify get_source_ids was called
        mock_notebooks_api.get_source_ids.assert_called_once_with("nb_123")

        # Verify CREATE_ARTIFACT RPC was called with fetched source IDs
        mock_core.rpc_executor.rpc_call.assert_called_once()
        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]
        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_001"]], [["src_002"]]]

    @pytest.mark.asyncio
    async def test_generate_video_source_encoding(self, mock_core, mock_mind_map_service):
        """Test generate_video has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [["artifact_456", "Video", 3, None, 1]]

        await api.generate_video(
            notebook_id="nb_123",
            source_ids=["src_a", "src_b"],
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        # Video params structure:
        # [
        #   client_options, notebook_id,
        #   [None, None, 3, source_ids_triple, None, None, None, None,
        #    [None, None, [source_ids_double, language, instructions, None, format_code, style_code]]]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        video_config = inner_params[8][2]
        source_ids_double = video_config[0]

        assert source_ids_triple == [[["src_a"]], [["src_b"]]]
        assert source_ids_double == [["src_a"], ["src_b"]]
        assert video_config[4] == VideoFormat.EXPLAINER.value
        assert video_config[5] == VideoStyle.AUTO_SELECT.value

    @pytest.mark.asyncio
    async def test_generate_video_explicit_options_override_defaults(
        self, mock_core, mock_mind_map_service
    ):
        """Explicit video format and style are encoded instead of API defaults."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_456", "Video", 3, None, 1]]

        await api.generate_video(
            notebook_id="nb_123",
            source_ids=["src_a"],
            video_format=VideoFormat.BRIEF,
            video_style=VideoStyle.ANIME,
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        video_config = params[2][8][2]
        assert video_config[4] == VideoFormat.BRIEF.value
        assert video_config[5] == VideoStyle.ANIME.value

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_prompt_encoding(
        self, mock_core, mock_mind_map_service
    ):
        """Test custom video style prompt is encoded like the live Web UI."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_456", "Video", 3, None, 1]]

        await api.generate_video(
            notebook_id="nb_123",
            source_ids=["src_a"],
            video_style=VideoStyle.CUSTOM,
            style_prompt="  Use hand-drawn diagrams  ",
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        video_config = params[2][8][2]
        assert video_config[5] is None
        assert video_config[6] == "Use hand-drawn diagrams"

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_requires_prompt(
        self, mock_core, mock_mind_map_service
    ):
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        with pytest.raises(ValidationError, match="style_prompt is required"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.CUSTOM,
            )

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_rejects_empty_prompt(
        self, mock_core, mock_mind_map_service
    ):
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        with pytest.raises(ValidationError, match="style_prompt is required"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.CUSTOM,
                style_prompt="",
            )

    @pytest.mark.asyncio
    async def test_generate_video_custom_style_rejects_blank_prompt(
        self, mock_core, mock_mind_map_service
    ):
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        with pytest.raises(ValidationError, match="style_prompt is required"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.CUSTOM,
                style_prompt="   ",
            )

    @pytest.mark.asyncio
    async def test_generate_video_style_prompt_requires_custom_style(
        self, mock_core, mock_mind_map_service
    ):
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        with pytest.raises(ValidationError, match="style_prompt requires"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_style=VideoStyle.ANIME,
                style_prompt="Use hand-drawn diagrams",
            )

    @pytest.mark.asyncio
    async def test_generate_video_cinematic_rejects_style_prompt(
        self, mock_core, mock_mind_map_service
    ):
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        with pytest.raises(ValidationError, match="cinematic"):
            await api.generate_video(
                notebook_id="nb_123",
                source_ids=["src_a"],
                video_format=VideoFormat.CINEMATIC,
                style_prompt="Use hand-drawn diagrams",
            )

    @pytest.mark.asyncio
    async def test_generate_report_source_encoding(self, mock_core, mock_mind_map_service):
        """Test generate_report has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [["artifact_789", "Report", 2, None, 1]]

        await api.generate_report(
            notebook_id="nb_123",
            source_ids=["src_x", "src_y", "src_z"],
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        # Report params structure:
        # [
        #   client_options, notebook_id,
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
    async def test_generate_report_extra_instructions_appended(
        self, mock_core, mock_mind_map_service
    ):
        """extra_instructions is appended to the built-in prompt with \\n\\n separator."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_789", "Report", 2, None, 1]]

        await api.generate_report(
            notebook_id="nb_123",
            source_ids=["src_x"],
            extra_instructions="Focus on financial metrics",
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        report_config = params[2][7][1]
        prompt = report_config[5]

        assert "Focus on financial metrics" in prompt
        assert "\n\nFocus on financial metrics" in prompt

    @pytest.mark.asyncio
    async def test_generate_report_extra_instructions_ignored_for_custom(
        self, mock_core, mock_mind_map_service
    ):
        """extra_instructions has no effect when report_format is CUSTOM."""
        from notebooklm.rpc.types import ReportFormat

        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_789", "Report", 2, None, 1]]

        await api.generate_report(
            notebook_id="nb_123",
            source_ids=["src_x"],
            report_format=ReportFormat.CUSTOM,
            custom_prompt="My custom prompt",
            extra_instructions="Should be ignored",
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        report_config = params[2][7][1]
        prompt = report_config[5]

        assert "Should be ignored" not in prompt
        assert prompt == "My custom prompt"

    @pytest.mark.asyncio
    async def test_generate_quiz_source_encoding(self, mock_core, mock_mind_map_service):
        """Test generate_quiz has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [["artifact_quiz", "Quiz", 4, None, 1]]

        await api.generate_quiz(
            notebook_id="nb_123",
            source_ids=["src_1", "src_2"],
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        # Quiz params structure:
        # [
        #   client_options, notebook_id,
        #   [None, None, 4, source_ids_triple, ...]
        # ]
        inner_params = params[2]
        source_ids_triple = inner_params[3]
        quiz_options = inner_params[9][1][7]

        assert source_ids_triple == [[["src_1"]], [["src_2"]]]
        assert quiz_options == [QuizQuantity.STANDARD.value, QuizDifficulty.MEDIUM.value]

    @pytest.mark.asyncio
    async def test_generate_quiz_explicit_options_override_defaults(
        self, mock_core, mock_mind_map_service
    ):
        """Explicit quiz quantity and difficulty are encoded instead of defaults."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_quiz", "Quiz", 4, None, 1]]

        await api.generate_quiz(
            notebook_id="nb_123",
            source_ids=["src_1"],
            quantity=QuizQuantity.FEWER,
            difficulty=QuizDifficulty.HARD,
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        quiz_options = params[2][9][1][7]
        assert quiz_options == [QuizQuantity.FEWER.value, QuizDifficulty.HARD.value]

    @pytest.mark.asyncio
    async def test_generate_flashcards_source_encoding(self, mock_core, mock_mind_map_service):
        """Test generate_flashcards has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [["artifact_fc", "Flashcards", 4, None, 1]]

        await api.generate_flashcards(
            notebook_id="nb_123",
            source_ids=["src_flash"],
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]
        flashcard_options = inner_params[9][1][6]

        assert source_ids_triple == [[["src_flash"]]]
        assert flashcard_options == [QuizDifficulty.MEDIUM.value, QuizQuantity.STANDARD.value]

    @pytest.mark.asyncio
    async def test_generate_flashcards_explicit_options_override_defaults(
        self, mock_core, mock_mind_map_service
    ):
        """Explicit flashcard quantity and difficulty preserve flashcard option order."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_fc", "Flashcards", 4, None, 1]]

        await api.generate_flashcards(
            notebook_id="nb_123",
            source_ids=["src_flash"],
            quantity=QuizQuantity.FEWER,
            difficulty=QuizDifficulty.EASY,
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        flashcard_options = params[2][9][1][6]
        assert flashcard_options == [QuizDifficulty.EASY.value, QuizQuantity.FEWER.value]

    @pytest.mark.asyncio
    async def test_generate_infographic_source_encoding(self, mock_core, mock_mind_map_service):
        """Test generate_infographic has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [
            ["artifact_info", "Infographic", 7, None, 1]
        ]

        await api.generate_infographic(
            notebook_id="nb_123",
            source_ids=["src_info_1", "src_info_2"],
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]
        infographic_config = inner_params[14][0]

        assert source_ids_triple == [[["src_info_1"]], [["src_info_2"]]]
        assert infographic_config[3] == InfographicOrientation.LANDSCAPE.value
        assert infographic_config[4] == InfographicDetail.STANDARD.value
        assert infographic_config[5] == InfographicStyle.AUTO_SELECT.value

    @pytest.mark.asyncio
    async def test_generate_infographic_visual_option_encoding(
        self, mock_core, mock_mind_map_service
    ):
        """Test generate_infographic encodes explicit visual options in config slots."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [
            ["artifact_info", "Infographic", 7, None, 1]
        ]

        await api.generate_infographic(
            notebook_id="nb_123",
            source_ids=["src_info_1"],
            orientation=InfographicOrientation.PORTRAIT,
            detail_level=InfographicDetail.DETAILED,
            style=InfographicStyle.PROFESSIONAL,
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        infographic_config = inner_params[14][0]

        assert infographic_config[3] == InfographicOrientation.PORTRAIT.value
        assert infographic_config[4] == InfographicDetail.DETAILED.value
        assert infographic_config[5] == InfographicStyle.PROFESSIONAL.value

    @pytest.mark.asyncio
    async def test_generate_slide_deck_source_encoding(self, mock_core, mock_mind_map_service):
        """Test generate_slide_deck has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [["artifact_slide", "Slides", 8, None, 1]]

        await api.generate_slide_deck(
            notebook_id="nb_123",
            source_ids=["src_slide"],
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]
        slide_config = inner_params[16][0]

        assert source_ids_triple == [[["src_slide"]]]
        assert slide_config[2] == SlideDeckFormat.DETAILED_DECK.value
        assert slide_config[3] == SlideDeckLength.DEFAULT.value

    @pytest.mark.asyncio
    async def test_generate_slide_deck_explicit_options_override_defaults(
        self, mock_core, mock_mind_map_service
    ):
        """Explicit slide deck format and length are encoded instead of defaults."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )
        mock_core.rpc_call.return_value = [["artifact_slide", "Slides", 8, None, 1]]

        await api.generate_slide_deck(
            notebook_id="nb_123",
            source_ids=["src_slide"],
            slide_format=SlideDeckFormat.PRESENTER_SLIDES,
            slide_length=SlideDeckLength.SHORT,
        )

        params = mock_core.rpc_executor.rpc_call.call_args.args[1]
        slide_config = params[2][16][0]
        assert slide_config[2] == SlideDeckFormat.PRESENTER_SLIDES.value
        assert slide_config[3] == SlideDeckLength.SHORT.value

    @pytest.mark.asyncio
    async def test_generate_data_table_source_encoding(self, mock_core, mock_mind_map_service):
        """Test generate_data_table has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [["artifact_table", "Table", 9, None, 1]]

        await api.generate_data_table(
            notebook_id="nb_123",
            source_ids=["src_table_1", "src_table_2"],
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
        params = call_args.args[1]

        inner_params = params[2]
        source_ids_triple = inner_params[3]

        assert source_ids_triple == [[["src_table_1"]], [["src_table_2"]]]

    @pytest.mark.asyncio
    async def test_generate_mind_map_source_encoding(
        self, mock_core, mock_mind_map_service, mock_notebooks_api
    ):
        """Test generate_mind_map has correct source encoding format."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=mock_notebooks_api,
            **mock_mind_map_service,
        )

        # Mock get_source_ids to return source IDs
        mock_notebooks_api.get_source_ids.return_value = ["src_mm_1", "src_mm_2"]

        # Mock the mind map generation RPC call
        mock_core.rpc_executor.rpc_call.return_value = [['{"name": "Mind Map", "children": []}']]

        await api.generate_mind_map(
            notebook_id="nb_123",
            source_ids=None,  # Will fetch sources
        )

        # Verify get_source_ids was called
        mock_notebooks_api.get_source_ids.assert_called_once_with("nb_123")

        # After the mind-map relocation, ``generate_mind_map`` also drives the CREATE_NOTE +
        # UPDATE_NOTE calls itself (previously delegated to NotesAPI), so
        # rpc_call is invoked three times. The source-encoding assertion
        # targets the GENERATE_MIND_MAP call specifically.
        generate_call = next(
            c
            for c in mock_core.rpc_executor.rpc_call.call_args_list
            if c.args[0].name == "GENERATE_MIND_MAP"
        )
        params = generate_call.args[1]

        # Mind map uses source_ids_nested = [[[sid]] for sid]
        source_ids_nested = params[0]

        assert source_ids_nested == [[["src_mm_1"]], [["src_mm_2"]]]

    @pytest.mark.asyncio
    async def test_generate_mind_map_passes_language_and_instructions(
        self, mock_core, mock_mind_map_service
    ):
        """Test generate_mind_map passes language and instructions to RPC payload."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [['{"name": "Mind Map", "children": []}']]

        await api.generate_mind_map(
            notebook_id="nb_123",
            source_ids=["src_1"],
            language="ja",
            instructions="Focus on key themes",
        )

        # Pick the GENERATE_MIND_MAP call specifically — CREATE_NOTE and
        # UPDATE_NOTE are now invoked alongside it.
        generate_call = next(
            c
            for c in mock_core.rpc_executor.rpc_call.call_args_list
            if c.args[0].name == "GENERATE_MIND_MAP"
        )
        params = generate_call.args[1]

        # params[5] should contain the mind map config with language and instructions
        mind_map_config = params[5]
        assert mind_map_config[1][0][1] == "Focus on key themes"
        assert mind_map_config[2] == "ja"

    @pytest.mark.asyncio
    async def test_suggest_reports_uses_get_suggested_reports(
        self, mock_core, mock_mind_map_service
    ):
        """Test suggest_reports uses GET_SUGGESTED_REPORTS RPC."""
        from notebooklm.rpc.types import RPCMethod

        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        # Mock the GET_SUGGESTED_REPORTS RPC call
        # Response format: [[[title, description, null, null, prompt, audience_level], ...]]
        mock_core.rpc_executor.rpc_call.return_value = [
            [["Report Title", "Description", None, None, "Custom prompt", 2]]
        ]

        result = await api.suggest_reports(notebook_id="nb_123")

        # Verify GET_SUGGESTED_REPORTS was called with correct params
        mock_core.rpc_executor.rpc_call.assert_called_once()
        call_args = mock_core.rpc_executor.rpc_call.call_args
        assert call_args.args[0] == RPCMethod.GET_SUGGESTED_REPORTS
        assert call_args.args[1] == [[2], "nb_123"]

        # Verify result parsing
        assert len(result) == 1
        assert result[0].title == "Report Title"


class TestEmptySourceIds:
    """Tests for edge cases with empty source lists."""

    @pytest.mark.asyncio
    async def test_generate_with_empty_source_list(self, mock_core, mock_mind_map_service):
        """Test generation with empty source_ids list produces empty arrays."""
        api = ArtifactsAPI(
            rpc=mock_core,
            drain=mock_core,
            lifecycle=mock_core,
            notebooks=MagicMock(),
            **mock_mind_map_service,
        )

        mock_core.rpc_executor.rpc_call.return_value = [["artifact_empty", "Audio", 1, None, 1]]

        await api.generate_audio(
            notebook_id="nb_123",
            source_ids=[],  # Explicit empty list
        )

        call_args = mock_core.rpc_executor.rpc_call.call_args
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
        api = _chat_from_mock_core(mock_core)

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
    """Tests for NotebooksAPI.get_source_ids method."""

    @pytest.mark.asyncio
    async def test_get_source_ids_extracts_correctly(self):
        """Test get_source_ids correctly extracts source IDs from notebook data."""
        from notebooklm._notebooks import NotebooksAPI
        from tests._fixtures.fake_core import make_fake_core

        rpc = AsyncMock()
        core = make_fake_core(rpc_call=rpc)
        api = NotebooksAPI(core.rpc_executor)

        # Mock notebook data with multiple sources
        # Structure: notebook_data[0][1] = sources list
        # Each source: [["source_id"], "Source Title", ...]
        rpc.return_value = [
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

        source_ids = await api.get_source_ids("nb_123")

        assert source_ids == ["source_aaa", "source_bbb", "source_ccc"]

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_empty_notebook(self):
        """Test get_source_ids handles notebook with no sources."""
        from notebooklm._notebooks import NotebooksAPI
        from tests._fixtures.fake_core import make_fake_core

        rpc = AsyncMock()
        core = make_fake_core(rpc_call=rpc)
        api = NotebooksAPI(core.rpc_executor)

        rpc.return_value = [["nb_123", []]]

        source_ids = await api.get_source_ids("nb_123")

        assert source_ids == []

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_null_response(self):
        """Test get_source_ids handles null API response."""
        from notebooklm._notebooks import NotebooksAPI
        from tests._fixtures.fake_core import make_fake_core

        rpc = AsyncMock()
        core = make_fake_core(rpc_call=rpc)
        api = NotebooksAPI(core.rpc_executor)

        rpc.return_value = None

        source_ids = await api.get_source_ids("nb_123")

        assert source_ids == []

    @pytest.mark.asyncio
    async def test_get_source_ids_handles_malformed_data(self):
        """Test get_source_ids handles malformed source data gracefully."""
        from notebooklm._notebooks import NotebooksAPI
        from tests._fixtures.fake_core import make_fake_core

        rpc = AsyncMock()
        core = make_fake_core(rpc_call=rpc)
        api = NotebooksAPI(core.rpc_executor)

        # Malformed data - missing nested structure
        # Structure: source[0] must be a list, source[0][0] must be a string
        rpc.return_value = [
            [
                "nb_123",
                [
                    [None, "Missing ID"],  # Invalid: source[0] is None
                    [["valid_id"], "Valid Source"],  # Valid
                    "not a list",  # Invalid: not a list at all
                ],
            ]
        ]

        source_ids = await api.get_source_ids("nb_123")

        # Should only extract the valid source
        assert source_ids == ["valid_id"]
