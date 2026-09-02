"""Recorded Web contracts for chat session status and cancellation (#2303).

Record with::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \
        tests/integration/test_chat_session_control_vcr.py -v -s

Scratch setup and teardown stay outside the cassette so the recording contains
exactly the ``oXwmh`` status read and ``XgrPMd`` cancellation write.
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

CASSETTE_NAME = "chat_session_control.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / "web" / CASSETTE_NAME
STATUS_RPC_ID = "oXwmh"
CANCEL_RPC_ID = "XgrPMd"


def _find_interaction(cassette: dict[str, Any], rpc_id: str) -> dict[str, Any]:
    matches = [
        interaction
        for interaction in cassette.get("interactions", [])
        if f"rpcids={rpc_id}" in interaction.get("request", {}).get("uri", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one rpcids={rpc_id} interaction in {CASSETTE_NAME}, found {len(matches)}"
    )
    return matches[0]


def _decode_params(body: str | bytes) -> list[Any]:
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    values = parse_qs(body).get("f.req", [])
    assert values, "f.req missing from recorded request"
    outer = json.loads(values[0])
    rpc_entry = outer[0][0]
    params = json.loads(rpc_entry[1])
    assert isinstance(params, list)
    return params


def _load_cassette_inputs() -> tuple[str, str]:
    with CASSETTE_PATH.open(encoding="utf-8") as fh:
        cassette = yaml.safe_load(fh)
    interaction = _find_interaction(cassette, STATUS_RPC_ID)
    query = parse_qs(interaction["request"]["uri"].split("?", 1)[1])
    source_path = query.get("source-path", [""])[0]
    assert source_path.startswith("/notebook/")
    notebook_id = source_path.removeprefix("/notebook/")
    params = _decode_params(interaction["request"]["body"])
    assert len(params) == 2 and params[0] is None
    conversation_id = params[1]
    assert isinstance(conversation_id, str) and conversation_id
    return notebook_id, conversation_id


async def _seed_scratch_conversation(client: NotebookLMClient) -> tuple[str, str]:
    notebook = await client.notebooks.create(f"session-control scratch ({uuid.uuid4()})")
    source = await client.sources.add_text(
        notebook.id,
        title=f"session-control source ({uuid.uuid4()})",
        content=(
            "Bicycles are human-powered vehicles with two wheels. "
            "They are used for transportation and recreation."
        ),
    )
    await client.sources.wait_for_sources(notebook.id, [source.id], timeout=120.0)
    result = await client.chat.ask(notebook.id, "Summarize the source in one sentence.")
    assert result.conversation_id
    return notebook.id, result.conversation_id


async def _teardown_scratch_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    try:
        await client.notebooks.delete(notebook_id)
    except Exception as exc:  # noqa: BLE001 - best-effort live-record cleanup
        print(f"WARNING: failed to delete scratch notebook {notebook_id}: {exc}", file=sys.stderr)


class TestChatSessionControlVCR:
    @pytest.mark.asyncio
    @pytest.mark.skipif(_vcr_record_mode, reason="golden replay runs after cassette recording")
    @notebooklm_vcr.use_cassette("chat_session_control.yaml")
    async def test_status_decoded_golden(self) -> None:
        """Pin the decoded idle status fields from the recorded ``oXwmh`` row."""
        notebook_id, conversation_id = _load_cassette_inputs()
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            status = await client.chat.session_status(notebook_id, conversation_id)

        assert status.generating is False
        assert status.token is None

    @pytest.mark.asyncio
    async def test_status_and_cancel_round_trip(self) -> None:
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            if _vcr_record_mode:
                notebook_id, conversation_id = await _seed_scratch_conversation(client)
                try:
                    with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                        status = await client.chat.session_status(notebook_id, conversation_id)
                        assert await client.chat.cancel(notebook_id, conversation_id) is None
                finally:
                    await _teardown_scratch_notebook(client, notebook_id)
            else:
                notebook_id, conversation_id = _load_cassette_inputs()
                with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                    status = await client.chat.session_status(notebook_id, conversation_id)
                    assert await client.chat.cancel(notebook_id, conversation_id) is None

        assert status.generating is False
        assert status.token is None

    def test_cassette_pins_both_wire_shapes(self) -> None:
        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)
        notebook_id, conversation_id = _load_cassette_inputs()

        assert len(cassette["interactions"]) == 2
        for rpc_id in (STATUS_RPC_ID, CANCEL_RPC_ID):
            interaction = _find_interaction(cassette, rpc_id)
            params = _decode_params(interaction["request"]["body"])
            assert params == [None, conversation_id]
            assert f"source-path=%2Fnotebook%2F{notebook_id}" in interaction["request"]["uri"]
