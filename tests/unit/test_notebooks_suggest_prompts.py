"""Unit tests for ``NotebooksAPI.suggest_prompts`` (``otmP3b`` /
``GeneratePromptSuggestions``).

Pins the wire contract (param-builder shape + ``source_path``) and the
response decode, including the empty / degenerate payloads. No network: the
``rpc`` collaborator is a narrow mock and ``get_source_ids`` is patched, matching
the ``suggest_reports`` unwrap tests and the other ``NotebooksAPI`` unit tests.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._notebooks import NotebooksAPI, build_prompt_suggestions_params
from notebooklm._runtime.contracts import RpcCaller
from notebooklm.exceptions import ValidationError
from notebooklm.rpc import RPCMethod
from notebooklm.types import PromptSuggestion

# One live-verified response (issue #1612): a wrapped single-element envelope
# whose inner list holds ``[title, prompt]`` rows.
_RESPONSE = [
    [
        ["Professional Briefing", "\n- Summarize the material for business professionals."],
        ["Process Timeline", "\n- Present the history of the product as a timeline."],
    ]
]


@pytest.fixture
def mock_rpc() -> MagicMock:
    """Narrow ``RpcCaller`` fake wired with an ``AsyncMock`` ``rpc_call``."""
    return MagicMock(spec=RpcCaller, rpc_call=AsyncMock(return_value=_RESPONSE))


@pytest.fixture
def api(mock_rpc: MagicMock) -> NotebooksAPI:
    """A ``NotebooksAPI`` whose ``get_source_ids`` returns two ids by default.

    The default-all-sources path resolves ids through ``get_source_ids``; tests
    that pass explicit ``source_ids`` assert it is never awaited.
    """
    notebooks = NotebooksAPI(mock_rpc)
    notebooks.get_source_ids = AsyncMock(return_value=["src_a", "src_b"])  # type: ignore[method-assign]
    return notebooks


# ---------------------------------------------------------------------------
# Param-builder shape
# ---------------------------------------------------------------------------


class TestBuildPromptSuggestionsParams:
    def test_full_shape(self) -> None:
        params = build_prompt_suggestions_params("nb_1", ["s1", "s2"], mode=4, query="steer")
        assert params == [
            [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]],
            "nb_1",
            [["s1"], ["s2"]],
            4,
            None,
            "steer",
        ]

    def test_defaults_mode_4_and_null_query(self) -> None:
        params = build_prompt_suggestions_params("nb_1", ["s1"])
        assert params[3] == 4  # mode default
        assert params[4] is None  # fixed null slot
        assert params[5] is None  # query default

    def test_empty_source_ids_yields_empty_wrapper(self) -> None:
        params = build_prompt_suggestions_params("nb_1", [])
        assert params[2] == []

    def test_context_is_fresh_each_call(self) -> None:
        """Each call builds an independent (nested-mutable) context block."""
        a = build_prompt_suggestions_params("nb_1", [])
        b = build_prompt_suggestions_params("nb_1", [])
        assert a[0] == b[0]
        assert a[0] is not b[0]

    @pytest.mark.parametrize("mode", [1, 4, 9])
    def test_accepts_in_range_modes(self, mode: int) -> None:
        assert build_prompt_suggestions_params("nb_1", [], mode=mode)[3] == mode

    @pytest.mark.parametrize("mode", [0, -1, 10, 100])
    def test_rejects_out_of_range_mode(self, mode: int) -> None:
        with pytest.raises(ValueError, match="1..9"):
            build_prompt_suggestions_params("nb_1", [], mode=mode)

    @pytest.mark.parametrize("query", ["", "   ", "\n\t "])
    def test_blank_query_normalised_to_none(self, query: str) -> None:
        # Empty / whitespace-only steer carries no signal → null slot.
        assert build_prompt_suggestions_params("nb_1", ["s1"], query=query)[5] is None

    def test_non_blank_query_preserved_verbatim(self) -> None:
        assert build_prompt_suggestions_params("nb_1", ["s1"], query="  steer  ")[5] == "  steer  "


# ---------------------------------------------------------------------------
# suggest_prompts: dispatch + decode
# ---------------------------------------------------------------------------


class TestSuggestPrompts:
    @pytest.mark.asyncio
    async def test_decodes_rows_to_prompt_suggestions(
        self, api: NotebooksAPI, mock_rpc: MagicMock
    ) -> None:
        result = await api.suggest_prompts("nb_xyz", source_ids=["s1"], mode=4)
        assert result == [
            PromptSuggestion(
                title="Professional Briefing",
                prompt="\n- Summarize the material for business professionals.",
            ),
            PromptSuggestion(
                title="Process Timeline",
                prompt="\n- Present the history of the product as a timeline.",
            ),
        ]

    @pytest.mark.asyncio
    async def test_sends_expected_rpc(self, api: NotebooksAPI, mock_rpc: MagicMock) -> None:
        await api.suggest_prompts("nb_xyz", source_ids=["s1", "s2"], mode=7, query="q")

        mock_rpc.rpc_call.assert_awaited_once()
        args, kwargs = mock_rpc.rpc_call.call_args
        assert args[0] is RPCMethod.SUGGEST_PROMPTS
        assert args[1] == build_prompt_suggestions_params("nb_xyz", ["s1", "s2"], mode=7, query="q")
        assert kwargs["source_path"] == "/notebook/nb_xyz"
        assert kwargs["allow_null"] is True

    @pytest.mark.asyncio
    async def test_defaults_to_all_sources_when_source_ids_none(
        self, api: NotebooksAPI, mock_rpc: MagicMock
    ) -> None:
        await api.suggest_prompts("nb_xyz")

        api.get_source_ids.assert_awaited_once_with("nb_xyz")  # type: ignore[attr-defined]
        # The resolved source ids are wrapped into f3 of the params.
        _, _, source_block, *_ = mock_rpc.rpc_call.call_args.args[1]
        assert source_block == [["src_a"], ["src_b"]]

    @pytest.mark.asyncio
    async def test_explicit_source_ids_skip_resolution(self, api: NotebooksAPI) -> None:
        await api.suggest_prompts("nb_xyz", source_ids=["only"])
        api.get_source_ids.assert_not_awaited()  # type: ignore[attr-defined]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("mode", [0, 10])
    async def test_out_of_range_mode_raises_before_any_network(
        self, api: NotebooksAPI, mock_rpc: MagicMock, mode: int
    ) -> None:
        """A bad mode raises ValidationError before the source fetch or the RPC."""
        with pytest.raises(ValidationError, match="1..9"):
            await api.suggest_prompts("nb_xyz", mode=mode)
        api.get_source_ids.assert_not_awaited()  # type: ignore[attr-defined]
        mock_rpc.rpc_call.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_single_suggestion_wrapped(self, api: NotebooksAPI, mock_rpc: MagicMock) -> None:
        """A wrapped single-row envelope (``[[[t, p]]]``) decodes one suggestion."""
        mock_rpc.rpc_call.return_value = [[["Solo", "\n- One prompt."]]]
        result = await api.suggest_prompts("nb_xyz", source_ids=["s1"])
        assert result == [PromptSuggestion(title="Solo", prompt="\n- One prompt.")]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("payload", [None, [], [[]], "unexpected", [None]])
    async def test_degenerate_payloads_yield_empty(
        self, api: NotebooksAPI, mock_rpc: MagicMock, payload: object
    ) -> None:
        mock_rpc.rpc_call.return_value = payload
        assert await api.suggest_prompts("nb_xyz", source_ids=["s1"]) == []

    @pytest.mark.asyncio
    async def test_malformed_rows_are_skipped(self, api: NotebooksAPI, mock_rpc: MagicMock) -> None:
        """Short / non-list rows degrade out; well-formed rows survive."""
        mock_rpc.rpc_call.return_value = [
            [
                ["Good", "\n- prompt"],
                ["MissingPrompt"],  # too short (< 2) -> skipped
                "not-a-row",  # non-list -> skipped
                ["Also Good", "\n- another"],
            ]
        ]
        result = await api.suggest_prompts("nb_xyz", source_ids=["s1"])
        assert [s.title for s in result] == ["Good", "Also Good"]
