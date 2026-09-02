"""Synchronous discovery (``Es3dTe DiscoverSources``) VCR cassette.

Locks the on-wire shape and the decoded result of :meth:`ResearchAPI.discover`
(the web "Discover sources" dialog call). The cassette captures exactly one
``Es3dTe`` POST plus the auth handshake.

Record with::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/test_research_discover_vcr.py -v -s

In record mode a scratch notebook is created OUTSIDE the cassette context (an
empty notebook is enough — discovery searches the web, not the sources), the
single ``discover`` call is recorded, and the notebook is torn down afterwards
(which also removes the completed job the call records). On replay the
recorded ``notebook_id`` is read back from the cassette's ``source-path`` so
the request matches at the matcher's chosen slots (``rpcids=Es3dTe`` + the
decoded ``f.req`` shape).

The decoded-row assertions double as this family's golden-decode pin (see
``tests/_guardrails/test_golden_decode_coverage.py``): they read the expected
values back out of the recorded response rather than hard-coding them, so a
re-recording stays green while a decoder slip (url/title/hint column swap,
lost job id, lost overview) still fails.
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
from notebooklm.types import DiscoveryMode, ResearchStatus
from tests.integration.conftest import _vcr_record_mode, get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "research_discover.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / "web" / CASSETTE_NAME

_DISCOVER_QUERY = "history of the transistor"


def _find_discover_interaction(cassette: dict[str, Any]) -> dict[str, Any]:
    """Locate the single ``Es3dTe`` POST inside the cassette."""
    matches = [
        interaction
        for interaction in cassette.get("interactions", [])
        if "rpcids=Es3dTe" in interaction.get("request", {}).get("uri", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one rpcids=Es3dTe interaction in {CASSETTE_NAME}, found {len(matches)}"
    )
    return matches[0]


def _decode_freq_params(body: str | bytes) -> list[Any]:
    """Decode the form-encoded ``f.req`` body into its param list."""
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    f_req_values = parse_qs(body).get("f.req", [])
    assert f_req_values, f"f.req not found in body: {body[:200]!r}"
    outer = json.loads(f_req_values[0])
    params = json.loads(outer[0][0][1])
    assert isinstance(params, list), "f.req params not a list"
    return params


def _decode_recorded_result(interaction: dict[str, Any]) -> list[Any]:
    """Return the raw ``[sources, overview, [job_id]]`` payload the cassette holds."""
    from notebooklm._web.wire.decoder import decode_response

    body = interaction["response"]["body"]["string"]
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    result = decode_response(body, "Es3dTe", allow_null=False)
    assert isinstance(result, list), f"recorded Es3dTe payload is not a list: {type(result)}"
    return result


def _load_cassette() -> dict[str, Any]:
    assert CASSETTE_PATH.exists(), (
        f"cassette missing: {CASSETTE_PATH}. "
        "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
    )
    with CASSETTE_PATH.open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def _load_cassette_notebook_id() -> str:
    """Return the ``notebook_id`` recorded into the cassette's ``source-path``."""
    interaction = _find_discover_interaction(_load_cassette())
    qs = parse_qs(interaction["request"]["uri"].split("?", 1)[1])
    source_path = qs.get("source-path", [""])[0]
    assert source_path.startswith("/notebook/"), (
        f"source-path did not name a notebook: {source_path!r}"
    )
    return source_path[len("/notebook/") :]


async def _teardown_scratch_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Delete the scratch notebook. Best-effort — failures are logged, not raised."""
    try:
        await client.notebooks.delete(notebook_id)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: failed to delete scratch notebook {notebook_id}: {exc}",
            file=sys.stderr,
        )


class TestResearchDiscoverVCR:
    """``client.research.discover`` recording + replay."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    async def test_discover_round_trips_decoded_golden(self) -> None:
        """One ``Es3dTe`` call decodes to a completed task pinned against the recorded payload."""
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            if _vcr_record_mode:
                notebook = await client.notebooks.create(
                    f"research-discover scratch ({uuid.uuid4()})"
                )
                notebook_id = notebook.id
                try:
                    with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                        task = await client.research.discover(notebook_id, _DISCOVER_QUERY)
                finally:
                    await _teardown_scratch_notebook(client, notebook_id)
            else:
                notebook_id = _load_cassette_notebook_id()
                with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                    task = await client.research.discover(notebook_id, _DISCOVER_QUERY)

        _assert_decoded_against_cassette(task)

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @pytest.mark.skipif(_vcr_record_mode, reason="replay-only golden pin; recorded above")
    @notebooklm_vcr.use_cassette("research_discover.yaml")
    async def test_discover_decoded_golden(self) -> None:
        """Replay-only golden pin (``tests/_guardrails/test_golden_decode_coverage.py``)."""
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            task = await client.research.discover(_load_cassette_notebook_id(), _DISCOVER_QUERY)
        _assert_decoded_against_cassette(task)

    def test_cassette_carries_expected_wire_shape(self) -> None:
        """The recorded Es3dTe body pins ``[[query, 1], null, 1, notebook_id]``."""
        interaction = _find_discover_interaction(_load_cassette())
        params = _decode_freq_params(interaction["request"]["body"])
        assert len(params) == 4, f"Es3dTe param count drift: expected 4, got {params!r}"
        assert params[0] == [_DISCOVER_QUERY, 1], f"slot 0 must be [query, 1], got {params[0]!r}"
        assert params[1] is None, f"slot 1 (RequestContext) must be null, got {params[1]!r}"
        assert params[2] == 1, f"slot 2 (DiscoveryMode) must be 1, got {params[2]!r}"
        assert params[3] == _load_cassette_notebook_id()
        # The recorded response is the three-slot [sources, overview, [job_id]].
        result = _decode_recorded_result(interaction)
        assert len(result) == 3
        assert all(len(row) == 4 and row[3] == 1 for row in result[0])
        assert isinstance(result[1], str) and result[1]
        assert isinstance(result[2], list) and isinstance(result[2][0], str) and result[2][0]


def _assert_decoded_against_cassette(task: Any) -> None:
    """Pin every decoded field of ``task`` to one slot of the recorded payload."""
    recorded = _decode_recorded_result(_find_discover_interaction(_load_cassette()))
    recorded_rows, recorded_overview, recorded_key = recorded[0], recorded[1], recorded[2]

    assert task.status is ResearchStatus.COMPLETED
    assert task.status_code == 2
    assert task.query == _DISCOVER_QUERY
    assert task.source_type == 1
    assert task.discovery_mode is DiscoveryMode.DEFAULT_LLM_SEARCH
    assert task.tasks == () and task.report == ""
    # Golden pins: every decoded field traces to one recorded slot.
    assert task.task_id == recorded_key[0]
    assert task.summary == recorded_overview
    assert len(task.sources) == len(recorded_rows) > 0
    for source, row in zip(task.sources, recorded_rows, strict=True):
        assert (source.url, source.title, source.hint, source.result_type) == (
            row[0],
            row[1],
            row[2],
            row[3],
        )
        assert source.research_task_id == task.task_id
        assert source.report_markdown == "" and source.source_ordinal is None
