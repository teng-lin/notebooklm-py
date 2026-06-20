"""Suggest-prompts (``otmP3b`` / ``GeneratePromptSuggestions``) VCR cassette.

Locks the on-wire shape of ``NotebooksAPI.suggest_prompts`` and exercises the
decode->``PromptSuggestion`` path end-to-end through the real RPC decoder
(golden decoded-row coverage for ``otmP3b``).

The cassette captures exactly one ``otmP3b`` POST plus the auth handshake.
Unlike the live-recorded chat cassettes, this fixture is **replay-only**:
``GeneratePromptSuggestions`` only returns rows once a notebook has indexed
sources, and the rows are LLM-nondeterministic, so a byte-stable recording is
impractical. The fixture's request body / response payload are scrubbed
synthetic values that the production encoder/decoder accept; the matcher keys on
``rpcids`` + the decoded ``f.req`` shape (notebook id + source-id wrappers).

The notebook id and source id below are the (scrubbed, non-real) values baked
into the cassette's recorded request; they are passed into ``suggest_prompts``
so the replayed request matches the recording at the matcher's slots.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import yaml

from notebooklm import NotebookLMClient, PromptSuggestion
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "notebooks_suggest_prompts.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

# Scrubbed, non-real ids baked into the cassette's recorded ``otmP3b`` request.
# Passed back into ``suggest_prompts`` so the replay matches the recording.
NOTEBOOK_ID = "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"
SOURCE_IDS = ["d1e2f3a4-0000-4000-8000-000000000001"]


def _find_suggest_interaction(cassette: dict[str, Any]) -> dict[str, Any]:
    """Locate the single ``otmP3b`` POST inside the cassette."""
    matches = [
        interaction
        for interaction in cassette.get("interactions", [])
        if "rpcids=otmP3b" in interaction.get("request", {}).get("uri", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one rpcids=otmP3b interaction in {CASSETTE_NAME}, found {len(matches)}"
    )
    return matches[0]


def _decode_freq_params(body: str | bytes) -> list[Any]:
    """Decode the form-encoded ``f.req`` body into its param list."""
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    qs = parse_qs(body)
    f_req_values = qs.get("f.req", [])
    assert f_req_values, f"f.req not found in body: {body[:200]!r}"
    outer = json.loads(f_req_values[0])
    assert isinstance(outer, list) and outer and isinstance(outer[0], list), (
        "f.req envelope malformed"
    )
    inner = outer[0][0][1]
    assert isinstance(inner, str), "f.req inner JSON missing"
    params = json.loads(inner)
    assert isinstance(params, list), "f.req params not a list"
    return params


class TestSuggestPromptsVCR:
    """``client.notebooks.suggest_prompts`` replay against the recorded ``otmP3b`` POST."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_suggest_prompts.yaml")
    async def test_suggest_prompts_decoded_golden(self) -> None:
        """Replay decodes the wrapped envelope into typed ``PromptSuggestion`` rows.

        Pins the decoded contract: three suggestions, each a
        :class:`PromptSuggestion` with a non-empty title + ready-to-send prompt.
        """
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            suggestions = await client.notebooks.suggest_prompts(
                NOTEBOOK_ID, source_ids=SOURCE_IDS, mode=4
            )

        # Decoded-row golden pin: the synthetic cassette carries three rows.
        assert len(suggestions) == 3
        assert all(isinstance(s, PromptSuggestion) for s in suggestions)
        assert all(s.title and s.prompt for s in suggestions)
        # Each prompt is a ready-to-send multi-line instruction (leading newline
        # + bullet, matching the live wire shape).
        assert suggestions[0].prompt.startswith("\n- ")

    def test_cassette_carries_expected_wire_shape(self) -> None:
        """The recorded otmP3b body pins the six-slot request shape."""
        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        interaction = _find_suggest_interaction(cassette)
        params = _decode_freq_params(interaction["request"]["body"])

        assert len(params) == 6, f"otmP3b param count drift: expected 6, got {len(params)}"
        # Slot 0: the 4-element client context (no field-5 projection).
        assert isinstance(params[0], list) and len(params[0]) == 4, (
            f"slot 0 (client context) drift: {params[0]!r}"
        )
        assert params[1] == NOTEBOOK_ID, f"slot 1 (notebook_id) drift: {params[1]!r}"
        # Slot 2: source-id wrappers, each [sid].
        assert params[2] == [[sid] for sid in SOURCE_IDS], (
            f"slot 2 source-id wrappers drift: {params[2]!r}"
        )
        assert params[3] in range(1, 10), f"slot 3 (mode) must be 1..9, got {params[3]!r}"
        assert params[4] is None, f"slot 4 must be null, got {params[4]!r}"
