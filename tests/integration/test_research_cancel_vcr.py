"""Cancel-research (``Zbrupe``) VCR cassette.

Locks the on-wire shape of :meth:`ResearchAPI.cancel`
(``CancelDiscoverSourcesJob``). The cassette captures exactly one ``Zbrupe``
POST plus the auth handshake.

Record with::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/test_research_cancel_vcr.py -v -s

In record mode a scratch notebook is created, a fast research run is kicked
off, and its *poll-level* run id (``task.task_id`` from a first poll — for fast
research that is ``start().task_id``) is cancelled; the scratch notebook is torn
down outside the cassette context so only the cancel POST is recorded. On
replay, the recorded ``notebook_id`` and ``run_id`` are read back from the
cassette so the request matches at the matcher's chosen slots
(``rpcids=Zbrupe`` + the decoded ``f.req`` shape).

The cancel response is ``[]`` unconditionally (the server does not validate the
id), so the recording deliberately scopes to the fire-and-forget round-trip:
``cancel`` returns ``None`` and never raises. The stop-semantics (a cancelled
IN_PROGRESS run transitioning to ``FAILED``) and the no-raise-on-garbage-id
contract are UNIT-tested in ``tests/unit/test_research.py``
(``TestResearchCancel``); VCR replays the recorded ``[]`` verbatim and cannot
synthesize a second poll's terminal-state transition.
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

CASSETTE_NAME = "research_cancel.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

_RESEARCH_QUERY = "Background on renewable energy storage"

SCRATCH_SOURCE_TITLE_PREFIX = "research-cancel scratch source"
SCRATCH_SOURCE_CONTENT = (
    "Pumped-storage hydroelectricity stores energy in the form of "
    "gravitational potential energy of water, pumped from a lower elevation "
    "reservoir to a higher elevation. It is the largest-capacity form of grid "
    "energy storage available."
)


def _find_cancel_interaction(cassette: dict[str, Any]) -> dict[str, Any]:
    """Locate the single ``Zbrupe`` POST inside the cassette."""
    matches = [
        interaction
        for interaction in cassette.get("interactions", [])
        if "rpcids=Zbrupe" in interaction.get("request", {}).get("uri", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one rpcids=Zbrupe interaction in {CASSETTE_NAME}, found {len(matches)}"
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
    """Return ``(notebook_id, run_id)`` recorded into the cassette.

    Both must round-trip into the replay's ``cancel`` call so the cassette
    matches: notebook from the ``source-path`` query param, run id from param
    slot 2 of the decoded ``f.req`` (``[None, None, run_id]``).
    """
    assert CASSETTE_PATH.exists(), (
        f"cassette missing: {CASSETTE_PATH}. "
        "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
    )
    with CASSETTE_PATH.open(encoding="utf-8") as fh:
        cassette = yaml.safe_load(fh)

    interaction = _find_cancel_interaction(cassette)
    uri = interaction["request"]["uri"]
    qs = parse_qs(uri.split("?", 1)[1])
    source_path = qs.get("source-path", [""])[0]
    assert source_path.startswith("/notebook/"), (
        f"source-path did not name a notebook: {source_path!r}"
    )
    notebook_id = source_path[len("/notebook/") :]

    params = _decode_freq_params(interaction["request"]["body"])
    assert len(params) >= 3, f"Zbrupe params too short: {params!r}"
    run_id = params[2]
    assert isinstance(run_id, str) and run_id, (
        f"run_id (slot 2) is not a non-empty string: {run_id!r}"
    )
    return notebook_id, run_id


async def _seed_scratch_research(client: NotebookLMClient) -> tuple[str, str]:
    """Create a fresh notebook + source + fast research, returning ``(notebook_id, run_id)``.

    The caller must run this OUTSIDE the cassette context so only the cancel
    POST is captured. ``run_id`` is the poll-level id (``task.task_id`` from a
    first poll); for fast research that equals ``start().task_id``.
    """
    notebook = await client.notebooks.create(f"research-cancel scratch ({uuid.uuid4()})")
    source = await client.sources.add_text(
        notebook.id,
        title=f"{SCRATCH_SOURCE_TITLE_PREFIX} ({uuid.uuid4()})",
        content=SCRATCH_SOURCE_CONTENT,
    )
    await client.sources.wait_for_sources(notebook.id, [source.id], timeout=120.0)

    started = await client.research.start(notebook.id, _RESEARCH_QUERY, source="web", mode="fast")
    assert started.task_id, "research.start must return a task_id"
    # The poll-level run id is what cancel takes; poll once to read it back.
    task = await client.research.poll(notebook.id, started.task_id)
    run_id = task.task_id or started.task_id
    return notebook.id, run_id


async def _teardown_scratch_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Delete the scratch notebook. Best-effort — failures are logged, not raised."""
    try:
        await client.notebooks.delete(notebook_id)
    except Exception as exc:  # noqa: BLE001
        print(
            f"WARNING: failed to delete scratch notebook {notebook_id}: {exc}",
            file=sys.stderr,
        )


class TestResearchCancelVCR:
    """``client.research.cancel`` recording + replay."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    async def test_cancel_round_trips(self) -> None:
        """``cancel`` returns None and produces no error envelope (fire-and-forget)."""
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            if _vcr_record_mode:
                notebook_id, run_id = await _seed_scratch_research(client)
                try:
                    with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                        assert await client.research.cancel(notebook_id, run_id) is None
                finally:
                    await _teardown_scratch_notebook(client, notebook_id)
            else:
                notebook_id, run_id = _load_cassette_inputs()
                with notebooklm_vcr.use_cassette(CASSETTE_NAME):
                    assert await client.research.cancel(notebook_id, run_id) is None

    def test_cassette_carries_expected_wire_shape(self) -> None:
        """The recorded Zbrupe body pins the three-slot ``[None, None, run_id]`` shape."""
        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        interaction = _find_cancel_interaction(cassette)
        params = _decode_freq_params(interaction["request"]["body"])

        assert len(params) == 3, (
            f"Zbrupe param count drift: expected 3, got {len(params)}. params={params!r}"
        )
        assert params[0] is None, f"slot 0 must be null, got {params[0]!r}"
        assert params[1] is None, f"slot 1 must be null, got {params[1]!r}"
        assert isinstance(params[2], str) and params[2], (
            f"slot 2 (run_id) is not a non-empty string: {params[2]!r}"
        )
