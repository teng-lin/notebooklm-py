"""Delete-conversation (``J7Gthc``) VCR cassette.

Locks the on-wire shape of ``ChatAPI.delete_conversation``. The cassette
captures exactly one ``J7Gthc`` POST plus the auth handshake.

Record with::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/test_chat_delete_conversation_vcr.py -v -s

A scratch notebook is created/torn down outside the cassette context so
only the delete POST is recorded. On replay, the recorded ``notebook_id``
and ``conversation_id`` are read back from the cassette so the request
matches byte-for-byte at the matcher's chosen slots.
"""

from __future__ import annotations

import json
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import yaml

from notebooklm import NotebookLMClient
from tests.integration.conftest import _vcr_record_mode, get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "chat_delete_conversation.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

QUESTION = "Summarize this source in one short sentence."

SCRATCH_SOURCE_TITLE_PREFIX = "delete-conv scratch source"
SCRATCH_SOURCE_CONTENT = (
    "Bicycles are human-powered, pedal-driven vehicles with two wheels "
    "attached to a frame. They are widely used for transport and "
    "recreation across many countries."
)


def _find_delete_interaction(cassette: dict[str, Any]) -> dict[str, Any]:
    """Locate the single ``J7Gthc`` POST inside the cassette."""
    matches = [
        interaction
        for interaction in cassette.get("interactions", [])
        if "rpcids=J7Gthc" in interaction.get("request", {}).get("uri", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one rpcids=J7Gthc interaction in {CASSETTE_NAME}, found {len(matches)}"
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
    rpc_entry = outer[0][0]
    inner = rpc_entry[1]
    assert isinstance(inner, str), "f.req inner JSON missing"
    params = json.loads(inner)
    assert isinstance(params, list), "f.req params not a list"
    return params


def _load_cassette_inputs() -> tuple[str, str]:
    """Return ``(notebook_id, conversation_id)`` recorded into the cassette.

    Both must round-trip into the replay's ``delete_conversation`` call so
    the cassette matches: notebook from the ``source-path`` query param,
    conversation from param slot 1 of the decoded ``f.req``.
    """
    assert CASSETTE_PATH.exists(), (
        f"cassette missing: {CASSETTE_PATH}. "
        "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
    )
    with CASSETTE_PATH.open(encoding="utf-8") as fh:
        cassette = yaml.safe_load(fh)

    interaction = _find_delete_interaction(cassette)
    uri = interaction["request"]["uri"]
    qs = parse_qs(uri.split("?", 1)[1])
    source_path = qs.get("source-path", [""])[0]
    assert source_path.startswith("/notebook/"), (
        f"source-path did not name a notebook: {source_path!r}"
    )
    notebook_id = source_path[len("/notebook/") :]

    params = _decode_freq_params(interaction["request"]["body"])
    assert len(params) >= 2, f"J7Gthc params too short: {params!r}"
    conversation_id = params[1]
    assert isinstance(conversation_id, str) and conversation_id, (
        f"conversation_id (slot 1) is not a non-empty string: {conversation_id!r}"
    )
    return notebook_id, conversation_id


async def _seed_scratch_conversation(
    client: NotebookLMClient,
) -> tuple[str, str]:
    """Create a fresh notebook + source + ask, returning ``(notebook_id, conversation_id)``.

    The caller must run this OUTSIDE the cassette context so only the
    delete POST is captured.
    """
    notebook = await client.notebooks.create(
        f"delete-conv scratch ({uuid.uuid4()})",
    )
    source = await client.sources.add_text(
        notebook.id,
        title=f"{SCRATCH_SOURCE_TITLE_PREFIX} ({uuid.uuid4()})",
        content=SCRATCH_SOURCE_CONTENT,
    )
    await client.sources.wait_for_sources(notebook.id, [source.id], timeout=120.0)

    result = await client.chat.ask(notebook.id, QUESTION)
    assert result.conversation_id, "scratch ask did not produce a conversation_id"
    return notebook.id, result.conversation_id


async def _teardown_scratch_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Delete the scratch notebook. Best-effort — failures are logged, not raised."""
    try:
        await client.notebooks.delete(notebook_id)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: failed to delete scratch notebook {notebook_id}: {exc}",
            file=sys.stderr,
        )


class TestDeleteConversationVCR:
    """``client.chat.delete_conversation`` recording + replay."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    async def test_delete_conversation_round_trips(self) -> None:
        """``delete_conversation`` returns None and produces no error envelope."""
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            if _vcr_record_mode:
                notebook_id, conversation_id = await _seed_scratch_conversation(client)
                try:
                    with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                        # v0.8.0 (#1290): delete_conversation returns None on success.
                        assert (
                            await client.chat.delete_conversation(notebook_id, conversation_id)
                            is None
                        )
                finally:
                    await _teardown_scratch_notebook(client, notebook_id)
            else:
                notebook_id, conversation_id = _load_cassette_inputs()
                with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                    # v0.8.0 (#1290): delete_conversation returns None on success.
                    assert (
                        await client.chat.delete_conversation(notebook_id, conversation_id) is None
                    )

    def test_cassette_carries_expected_wire_shape(self) -> None:
        """The recorded J7Gthc body pins the four-slot ``[[], conv_id, None, 1]`` shape."""
        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        interaction = _find_delete_interaction(cassette)
        params = _decode_freq_params(interaction["request"]["body"])

        assert len(params) == 4, (
            f"J7Gthc param count drift: expected 4, got {len(params)}. params={params!r}"
        )
        assert params[0] == [], f"slot 0 must be empty list, got {params[0]!r}"
        assert isinstance(params[1], str) and params[1], (
            f"slot 1 (conversation_id) is not a non-empty string: {params[1]!r}"
        )
        assert params[2] is None, f"slot 2 must be null, got {params[2]!r}"
        assert params[3] == 1, f"slot 3 (trailing flag) drift: got {params[3]!r}"
