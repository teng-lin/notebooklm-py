"""Tests for artifact generation-prompt retrieval.

Covers the three layers added for ``artifact get-prompt``:

* :attr:`ArtifactRow.generation_prompt` — the position-aware decoder, pinned
  per artifact type so a wire reshape is caught here.
* :meth:`ArtifactListingService.get_prompt` — id lookup over the studio listing,
  raising :class:`ArtifactNotFoundError` on a miss.
* :meth:`ArtifactsAPI.get_prompt` — the public delegation.

The per-type positions below were verified live against a notebook holding one
artifact of every type; they mirror :data:`ArtifactRow._PROMPT_LOCATION` and act
as an independent canary on it.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifact.listing import ArtifactListingService
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteService
from notebooklm._row_adapters.artifacts import ArtifactRow
from notebooklm.exceptions import ArtifactNotFoundError, UnknownRPCMethodError
from notebooklm.rpc import (
    FLASHCARDS_VARIANT,
    INTERACTIVE_MIND_MAP_VARIANT,
    QUIZ_VARIANT,
    ArtifactTypeCode,
)

# (artifact type code, top-level block index, sub-path inside the block).
# An independent restatement of the verified position map.
_PROMPT_POSITIONS = [
    (ArtifactTypeCode.AUDIO.value, 6, (1, 0)),
    (ArtifactTypeCode.REPORT.value, 7, (1, 5)),
    (ArtifactTypeCode.VIDEO.value, 8, (2, 2)),
    (ArtifactTypeCode.QUIZ.value, 9, (1, 2)),  # quiz / flashcards / interactive mind map
    (ArtifactTypeCode.INFOGRAPHIC.value, 14, (0, 0)),
    (ArtifactTypeCode.SLIDE_DECK.value, 16, (0, 0)),
    (ArtifactTypeCode.DATA_TABLE.value, 18, (1, 0)),
]


def _nest(path: tuple[int, ...], value: object) -> list:
    """Build the smallest nested list where ``block[path[0]][path[1]]... == value``."""
    node: object = value
    for idx in reversed(path):
        level: list = [None] * (idx + 1)
        level[idx] = node
        node = level
    return node  # type: ignore[return-value]


def _row_with_prompt(type_code: int, top_pos: int, sub_path: tuple[int, ...], prompt: str) -> list:
    """A minimal LIST_ARTIFACTS row carrying ``prompt`` at its type's position."""
    row: list = ["art_1", "Title", type_code, None, 3]
    while len(row) <= top_pos:
        row.append(None)
    row[top_pos] = _nest(sub_path, prompt)
    return row


class TestGenerationPromptByType:
    @pytest.mark.parametrize(("type_code", "top_pos", "sub_path"), _PROMPT_POSITIONS)
    def test_prompt_round_trips_for_each_type(
        self, type_code: int, top_pos: int, sub_path: tuple[int, ...]
    ) -> None:
        prompt = "Explain the technique, one concept per row."
        row = ArtifactRow(_row_with_prompt(type_code, top_pos, sub_path, prompt))
        assert row.generation_prompt == prompt

    def test_position_map_matches_the_adapter(self) -> None:
        """The test's restated map is exactly the adapter's ``_PROMPT_LOCATION``."""
        expected = {tc: (top, *sub) for tc, top, sub in _PROMPT_POSITIONS}
        assert expected == ArtifactRow._PROMPT_LOCATION


class TestGenerationPromptAbsent:
    def test_unknown_type_returns_none(self) -> None:
        # Type 99 has no prompt location.
        row = ArtifactRow(["art_1", "Title", 99, None, 3])
        assert row.generation_prompt is None

    def test_note_backed_mind_map_type_returns_none(self) -> None:
        # Synthetic type 5 (note-backed mind map) is not a studio prompt source.
        row = ArtifactRow(["art_1", "Title", ArtifactTypeCode.MIND_MAP.value, None, 3])
        assert row.generation_prompt is None

    def test_short_row_without_block_returns_none(self) -> None:
        # Data-table type but the row never reaches position 18.
        row = ArtifactRow(["art_1", "Title", ArtifactTypeCode.DATA_TABLE.value, None, 3])
        assert row.generation_prompt is None

    def test_non_string_prompt_leaf_returns_none(self) -> None:
        row = ArtifactRow(_row_with_prompt(ArtifactTypeCode.AUDIO.value, 6, (1, 0), prompt=""))
        # An empty string is still a string and round-trips; a non-string leaf does not.
        raw = _row_with_prompt(ArtifactTypeCode.AUDIO.value, 6, (1, 0), prompt="placeholder")
        raw[6] = _nest((1, 0), 12345)  # int leaf where a prompt string is expected
        assert ArtifactRow(raw).generation_prompt is None
        assert row.generation_prompt == ""


class TestGenerationPromptDrift:
    def test_block_present_but_inner_shape_drifted_raises(self) -> None:
        # Position 6 is present (a list) but too short for [1][0]: genuine drift.
        row = ArtifactRow(["art_1", "Title", ArtifactTypeCode.AUDIO.value, None, 3, None, []])
        with pytest.raises(UnknownRPCMethodError):
            _ = row.generation_prompt


def _completed_row(artifact_id: str, type_code: int, prompt: str | None) -> list:
    if prompt is None:
        return [artifact_id, "Title", type_code, None, 3]  # type with no prompt block
    top_pos, *sub = ArtifactRow._PROMPT_LOCATION[type_code]
    row = _row_with_prompt(type_code, top_pos, tuple(sub), prompt)
    row[0] = artifact_id
    return row


class TestListingServiceGetPrompt:
    @pytest.mark.asyncio
    async def test_returns_prompt_for_matching_id(self) -> None:
        rows = [_completed_row("art_a", ArtifactTypeCode.REPORT.value, "Report prompt text")]
        list_raw = AsyncMock(return_value=rows)
        service = ArtifactListingService()
        result = await service.get_prompt("nb_1", "art_a", list_raw=list_raw)
        assert result == "Report prompt text"

    @pytest.mark.asyncio
    async def test_raises_not_found_for_absent_id(self) -> None:
        rows = [_completed_row("art_a", ArtifactTypeCode.REPORT.value, "x")]
        list_raw = AsyncMock(return_value=rows)
        service = ArtifactListingService()
        with pytest.raises(ArtifactNotFoundError):
            await service.get_prompt("nb_1", "missing", list_raw=list_raw)

    @pytest.mark.asyncio
    async def test_returns_none_when_artifact_has_no_prompt(self) -> None:
        # Artifact exists in the listing but its type carries no prompt block.
        rows = [_completed_row("art_a", ArtifactTypeCode.MIND_MAP.value, None)]
        list_raw = AsyncMock(return_value=rows)
        service = ArtifactListingService()
        result = await service.get_prompt("nb_1", "art_a", list_raw=list_raw)
        assert result is None


@pytest.fixture
def artifacts_api() -> ArtifactsAPI:
    from tests._fixtures.fake_core import make_fake_core

    rows = [_completed_row("art_a", ArtifactTypeCode.VIDEO.value, "Video prompt text")]
    core = make_fake_core(rpc_call=AsyncMock(return_value=rows))
    mind_maps = MagicMock(spec=NoteBackedMindMapService)
    mind_maps.list_mind_maps = AsyncMock(return_value=[])
    notebooks = MagicMock()
    notebooks.get_source_ids = AsyncMock(return_value=[])
    return ArtifactsAPI(
        rpc=core,
        drain=core,
        lifecycle=core,
        notebooks=notebooks,
        mind_maps=mind_maps,
        note_service=MagicMock(spec=NoteService),
    )


class TestArtifactsAPIGetPrompt:
    @pytest.mark.asyncio
    async def test_delegates_and_returns_prompt(self, artifacts_api: ArtifactsAPI) -> None:
        result = await artifacts_api.get_prompt("nb_1", "art_a")
        assert result == "Video prompt text"

    @pytest.mark.asyncio
    async def test_raises_not_found_for_absent_id(self, artifacts_api: ArtifactsAPI) -> None:
        with pytest.raises(ArtifactNotFoundError):
            await artifacts_api.get_prompt("nb_1", "missing")

    @pytest.mark.asyncio
    async def test_returns_none_for_note_backed_mind_map_id(self) -> None:
        # A note-backed mind map ID is not in the studio listing, so
        # ``_listing.get_prompt`` raises ArtifactNotFoundError. The public
        # API should catch that and return None when the ID belongs to a
        # note-backed mind map.
        from tests._fixtures.fake_core import make_fake_core

        studio_rows: list = []  # no studio artifacts
        core = make_fake_core(rpc_call=AsyncMock(return_value=studio_rows))
        mind_map_row = ["mm_1", "some content"]  # minimal raw note row; NoteRow(row).id == "mm_1"
        mind_maps = MagicMock(spec=NoteBackedMindMapService)
        mind_maps.list_mind_maps = AsyncMock(return_value=[mind_map_row])
        notebooks = MagicMock()
        notebooks.get_source_ids = AsyncMock(return_value=[])
        api = ArtifactsAPI(
            rpc=core,
            drain=core,
            lifecycle=core,
            notebooks=notebooks,
            mind_maps=mind_maps,
            note_service=MagicMock(spec=NoteService),
        )
        result = await api.get_prompt("nb_1", "mm_1")
        assert result is None

    @pytest.mark.asyncio
    async def test_raises_not_found_when_id_absent_from_both_listings(self) -> None:
        # ID is in neither the studio listing nor the mind-map listing.
        from tests._fixtures.fake_core import make_fake_core

        studio_rows: list = []
        core = make_fake_core(rpc_call=AsyncMock(return_value=studio_rows))
        mind_maps = MagicMock(spec=NoteBackedMindMapService)
        mind_maps.list_mind_maps = AsyncMock(return_value=[])
        notebooks = MagicMock()
        notebooks.get_source_ids = AsyncMock(return_value=[])
        api = ArtifactsAPI(
            rpc=core,
            drain=core,
            lifecycle=core,
            notebooks=notebooks,
            mind_maps=mind_maps,
            note_service=MagicMock(spec=NoteService),
        )
        with pytest.raises(ArtifactNotFoundError):
            await api.get_prompt("nb_1", "ghost")


class TestGenerationPromptRealWireShapes:
    """Pin ``generation_prompt`` against the REAL ``LIST_ARTIFACTS`` shapes.

    :class:`TestGenerationPromptByType` above nests the prompt at the very path
    the adapter reads (derived from the same position map), so it proves the map
    is *self-consistent* but cannot catch a position that is wrong on the wire.
    These cases instead encode the actual recorded options-block layouts — the
    variant, language, and difficulty/quantity present *alongside* the prompt —
    with the prompt at a **literal** index, so they fail if ``_PROMPT_LOCATION``
    drifts and confirm the prompt is read past the variant rather than confused
    with a sibling slot. Shapes verified live against a notebook holding a
    prompted artifact of each kind (the basis for PR #1580).
    """

    # (variant, real ``data[9][1]`` options array with the prompt at index 2).
    # Quiz/flashcards/interactive-mind-map share the type-4 family but differ in
    # the surrounding options (quiz [2,2] at idx 7, flashcards at idx 6, mind map
    # none) — exactly the layout that makes a fixed (9, 1, 2) non-obvious.
    _TYPE4_CASES = [
        (QUIZ_VARIANT, lambda p: [2, None, p, "en", None, None, None, [2, 2], True]),
        (FLASHCARDS_VARIANT, lambda p: [1, None, p, "en", None, None, [2, 2], None, True]),
        (INTERACTIVE_MIND_MAP_VARIANT, lambda p: [4, None, p, "en", None, None, None, None, True]),
    ]

    @pytest.mark.parametrize(("variant", "options"), _TYPE4_CASES)
    def test_type4_prompt_read_past_variant_and_options(self, variant, options) -> None:
        prompt = "Focus only on the three astronauts."
        # Timestamp block sits at index 15 (_TIMESTAMP_POS) on the real wire, so
        # indices 10-14 are padded before it — keeping the row faithful.
        row = [
            "art_1", "Title", ArtifactTypeCode.QUIZ.value, [[["s"]]], 3,
            None, None, None, None, [None, options(prompt)],
            None, None, None, None, None, [1700000000, 0],
        ]  # fmt: skip
        adapter = ArtifactRow(row)
        # The prompt is at [9][1][2]; the variant at [9][1][0] and the
        # difficulty/quantity tuple must NOT be mistaken for it.
        assert adapter.variant == variant
        assert adapter.generation_prompt == prompt

    def test_type4_no_prompt_real_shape_returns_none(self) -> None:
        # Real no-prompt quiz row: [9][1][2] is ``None`` (a "no prompt was set"
        # case, distinct from a wrong position) — must read as None, not raise.
        row = [
            "art_1", "Title", ArtifactTypeCode.QUIZ.value, [[["s"]]], 3,
            None, None, None, None,
            [None, [2, None, None, "en", None, None, None, [2, 2], True]],
            None, None, None, None, None, [1700000000, 0],
        ]  # fmt: skip
        assert ArtifactRow(row).generation_prompt is None

    def test_infographic_prompt_real_config_block(self) -> None:
        prompt = "Highlight the crew and the landing site."
        # Real infographic config block: ``data[14][0] == [<prompt>, 'en', None, 1, 2]``.
        row: list = ["art_1", "Title", ArtifactTypeCode.INFOGRAPHIC.value, None, 3]
        while len(row) <= 14:
            row.append(None)
        row[14] = [[prompt, "en", None, 1, 2]]
        assert ArtifactRow(row).generation_prompt == prompt
