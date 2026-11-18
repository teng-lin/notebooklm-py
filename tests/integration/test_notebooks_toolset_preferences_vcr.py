"""Toolset-preferences (``EEr8Nb`` / ``ListNotebookToolsetPreferences``) VCR cassette.

Locks the on-wire shape of ``NotebooksAPI.get_toolset_preferences`` and exercises
the decode -> :class:`ToolsetPreference` path end-to-end through the real RPC
decoder (golden decoded-row coverage for ``EEr8Nb``).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import yaml

from notebooklm import NotebookLMClient, ToolsetPreference
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "notebooks_toolset_preferences.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / "web" / CASSETTE_NAME

# Scrubbed, non-real id baked into the cassette's recorded ``EEr8Nb`` request.
NOTEBOOK_ID = "c3f6285f-1709-44c4-9cd6-e95cf0ea4f5e"


def _find_toolset_interaction(cassette: dict[str, Any]) -> dict[str, Any]:
    """Locate the single ``EEr8Nb`` POST inside the cassette."""
    matches = [
        interaction
        for interaction in cassette.get("interactions", [])
        if "rpcids=EEr8Nb" in interaction.get("request", {}).get("uri", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one rpcids=EEr8Nb interaction in {CASSETTE_NAME}, found {len(matches)}"
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


class TestToolsetPreferencesVCR:
    """``client.notebooks.get_toolset_preferences`` replay against ``EEr8Nb``."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette("notebooks_toolset_preferences.yaml")
    async def test_get_toolset_preferences_decoded_golden(self) -> None:
        """Replay decodes the wrapped envelope into typed ``ToolsetPreference`` rows."""
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            preferences = await client.notebooks.get_toolset_preferences(NOTEBOOK_ID)

        assert preferences == [
            ToolsetPreference(
                toolset_id="google-drive",
                display_name="SCRUBBED_TITLE",
                enabled=True,
                tools=("Search drive", "Read drive file"),
            )
        ]

    def test_cassette_carries_expected_wire_shape(self) -> None:
        """The recorded ``EEr8Nb`` body pins the single-slot ``[notebook_id]`` request."""
        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        interaction = _find_toolset_interaction(cassette)
        params = _decode_freq_params(interaction["request"]["body"])

        assert params == [NOTEBOOK_ID]
