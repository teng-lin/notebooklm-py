"""Multi-source streaming-chat VCR cassette.

This module records and replays a single ``chat.ask`` call against a
fresh scratch notebook that carries **five text sources**, with all five
source IDs passed explicitly to ``ask``. The point is to lock in the
on-wire shape of the **nested-list source-id encoder** for the chat
endpoint — the audit flagged this path as fragile because the
encoding has two distinct nesting depths (``[[id]]`` per source, then
collected into an outer list) and the per-method test coverage prior
to this cassette only exercised the zero-source and one-source cases via the
default-sources branch (``ask`` calls ``get_source_ids`` itself).

What this guards
----------------
``_chat._build_chat_request`` builds the params with::

    sources_array = nest_source_ids(source_ids, 2)
    params = [sources_array, question, conversation_history, ...]

``nest_source_ids(ids, 2)`` wraps each id with two inner lists, giving
``[[[id1]], [[id2]], [[id3]], [[id4]], [[id5]]]``. A regression that
silently flattens to depth 1 (``[[id1], [id2], ...]``) or depth 3
(``[[[[id1]]], ...]``) would still type-check and still POST, but the
server would return an error or — worse — silently drop sources. The
cassette captures the recorded ``f.req`` body so the unit-level
``test_chat_request_carries_five_source_ids_nested`` assertion has a
real on-wire reference to compare against.

Recording
---------
::

    NOTEBOOKLM_VCR_RECORD=1 uv run pytest \\
        tests/integration/test_chat_multi_source_vcr.py -v -s

The recording flow uses **its own scratch notebook**, NOT the shared
``NOTEBOOKLM_GENERATION_NOTEBOOK_ID``, so parallel cassette-recording
agents don't trip over one another. The scratch notebook and its
sources are deleted at the end of the recording run; only the chat
``ask`` interaction is captured inside the cassette context.

Replay
------
Replay opts into the ``freq`` body matcher on top of the
default ``method/scheme/host/port/path`` matchers. The streaming-chat
endpoint at ``/GenerateFreeFormStreamed`` does not carry ``rpcids``, so without
``freq`` two chat POSTs would be indistinguishable. ``freq`` decodes
``f.req`` and disambiguates by param count + ``notebook_id`` at slot 7,
which is exactly what we need: the cassette's recorded ``notebook_id``
(a now-deleted scratch UUID) is read out of the cassette and passed
back into ``ask`` so the replay matches the recording byte-for-byte at
the matcher's chosen slots.
"""

from __future__ import annotations

import json
import re
import sys
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import pytest
import yaml

# Match the rest of ``tests/integration/test_vcr_*.py`` — these files are
# imported by pytest with the repo root NOT on sys.path.
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))

from conftest import _vcr_record_mode, get_vcr_auth, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "chat_ask_multi_source.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

# Number of sources recorded into the cassette. Five is the smallest value
# that comfortably exceeds the "two or three sources" gut-check most chat
# call sites use during dev and still keeps the cassette small (text
# sources, one short paragraph each). The ``acceptance`` clause in the
# plan requires 5+; we record exactly 5.
SOURCE_COUNT = 5

# Question text. Kept short, generic, and PII-free — the question text
# is NOT scrubbed by ``cassette_patterns.py``, so we deliberately avoid
# anything that could resemble an account-linked phrase.
QUESTION = "Summarize all sources in one short paragraph."


# =============================================================================
# Cassette parsing helpers (used by both recording-time and replay tests)
# =============================================================================


def _find_chat_interaction(cassette: dict[str, Any]) -> dict[str, Any]:
    """Locate the single ``GenerateFreeFormStreamed`` interaction inside the cassette.

    The cassette context wraps exactly one chat POST; the helper raises a
    clear AssertionError if a future recording accidentally captures more
    or fewer interactions.
    """
    chat_interactions = [
        interaction
        for interaction in cassette.get("interactions", [])
        if "GenerateFreeFormStreamed" in interaction.get("request", {}).get("uri", "")
    ]
    assert len(chat_interactions) == 1, (
        f"expected exactly one GenerateFreeFormStreamed interaction in {CASSETTE_NAME}, "
        f"found {len(chat_interactions)}"
    )
    return chat_interactions[0]


def _decode_freq_params(body: str | bytes) -> list[Any]:
    """Decode the form-encoded ``f.req`` body into its param list.

    Mirrors the logic in ``vcr_config._freq_body_matcher._extract_freq``;
    duplicated here so the replay test does not depend on vcr_config's
    private helper.
    """
    if isinstance(body, bytes):
        body = body.decode("utf-8")
    qs = parse_qs(body)
    f_req_values = qs.get("f.req", [])
    assert f_req_values, f"f.req not found in body: {body[:200]!r}"
    outer = json.loads(f_req_values[0])
    assert isinstance(outer, list) and len(outer) >= 2, "f.req envelope malformed"
    inner = outer[1]
    assert isinstance(inner, str), "f.req inner JSON missing"
    params = json.loads(inner)
    assert isinstance(params, list), "f.req params not a list"
    return params


def _load_cassette_params() -> tuple[list[Any], str]:
    """Return ``(decoded_params, notebook_id)`` from the recorded chat POST.

    Reads the cassette from disk so the replay test stays decoupled from
    any state the recording test left behind.
    """
    assert CASSETTE_PATH.exists(), (
        f"cassette missing: {CASSETTE_PATH}. "
        "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
    )
    with CASSETTE_PATH.open(encoding="utf-8") as fh:
        cassette = yaml.safe_load(fh)

    interaction = _find_chat_interaction(cassette)
    body = interaction["request"]["body"]
    params = _decode_freq_params(body)
    # Bounds-guard ``params[7]`` so a malformed/short cassette fails with a
    # message that identifies the helper rather than a bare ``IndexError``.
    # The caller's ``len(params) == 9`` assertion would otherwise fire too
    # late (after this helper has already crashed).
    assert len(params) > 7, (
        f"cassette params too short: expected at least 8 slots, got {len(params)}. "
        f"Re-record with NOTEBOOKLM_VCR_RECORD=1."
    )
    notebook_id = params[7]
    assert isinstance(notebook_id, str), f"slot 7 (notebook_id) not a string: {notebook_id!r}"
    return params, notebook_id


def _extract_source_ids_from_nested(sources_array: list[Any]) -> list[str]:
    """Flatten the depth-2 nested ``[[[id]], ...]`` structure back to ids.

    ``nest_source_ids(ids, 2)`` wraps each id into ``[[id]]`` and collects
    them into an outer list. This helper undoes that exact shape and
    raises a clear AssertionError if any slot deviates — making the
    replay test fail loudly on an encoder regression rather than
    silently returning an empty list.
    """
    extracted: list[str] = []
    for index, slot in enumerate(sources_array):
        assert isinstance(slot, list) and len(slot) == 1, (
            f"sources_array slot {index} is not a single-element list: {slot!r}"
        )
        inner = slot[0]
        assert isinstance(inner, list) and len(inner) == 1, (
            f"sources_array slot {index} inner is not a single-element list: {inner!r}"
        )
        source_id = inner[0]
        assert isinstance(source_id, str) and source_id, (
            f"sources_array slot {index} id is not a non-empty string: {source_id!r}"
        )
        extracted.append(source_id)
    return extracted


# =============================================================================
# Recording-time scratch notebook setup
# =============================================================================
#
# Setup and teardown live OUTSIDE the cassette context. The cassette
# context wraps only the ``ask`` call, so the cassette captures exactly
# one streaming-chat POST and nothing else — no notebook-create, no
# source-add, no notebook-delete clutter to mismatch on replay.


async def _seed_scratch_notebook(
    client: NotebookLMClient,
) -> tuple[str, list[str]]:
    """Create a fresh notebook with ``SOURCE_COUNT`` text sources.

    Returns ``(notebook_id, source_ids)``. Each source carries a short,
    PII-free paragraph; the title embeds a UUID so a re-run never
    collides with a leftover source from a prior aborted recording (the
    UUID also rules out title-based dedupe heuristics).
    """
    title = f"T8.E6 scratch multi-source chat ({uuid.uuid4()})"
    notebook = await client.notebooks.create(title)
    notebook_id = notebook.id

    # Five short, generic paragraphs. Distinct enough that the answer
    # has something to summarize, but boring enough that no scrubbing
    # corner case fires on the recorded request bodies.
    contents = [
        (
            "Bicycles are human-powered, pedal-driven vehicles with two wheels "
            "attached to a frame. They are widely used for transport, recreation, "
            "and exercise across many countries."
        ),
        (
            "Lighthouses are towers built near coastlines to emit light and warn "
            "ships of dangerous shoals. Their construction varies with terrain "
            "but the navigational role is consistent worldwide."
        ),
        (
            "Tea is a beverage prepared by pouring hot water over cured leaves "
            "of the Camellia sinensis plant. Different processing methods yield "
            "green, black, oolong, and white tea varieties."
        ),
        (
            "Sourdough bread is leavened by naturally occurring yeasts and "
            "lactobacilli in a flour-and-water starter. The long fermentation "
            "produces the characteristic tangy flavor and chewy crumb."
        ),
        (
            "Origami is the Japanese art of paper folding. Starting from a "
            "single sheet, a sequence of folds produces shapes ranging from "
            "simple cranes to complex modular geometric forms."
        ),
    ]
    assert len(contents) == SOURCE_COUNT, "contents and SOURCE_COUNT must agree"

    source_ids: list[str] = []
    for index, content in enumerate(contents, start=1):
        source = await client.sources.add_text(
            notebook_id,
            title=f"Source {index} ({uuid.uuid4()})",
            content=content,
        )
        source_ids.append(source.id)

    # Wait until all sources are ready so the chat call actually has
    # something to retrieve. The text-source ingestion is normally fast
    # but not instant; ``wait_for_sources`` polls server-side state.
    await client.sources.wait_for_sources(notebook_id, source_ids, timeout=120.0)
    return notebook_id, source_ids


async def _teardown_scratch_notebook(client: NotebookLMClient, notebook_id: str) -> None:
    """Delete the scratch notebook so the recording leaves no residue.

    Failures here are logged but not raised — a leaked scratch notebook
    is a maintainer hygiene issue, not a correctness failure for the
    cassette we just recorded. The maintainer can prune leftovers
    manually via ``notebooklm list``.
    """
    try:
        await client.notebooks.delete(notebook_id)
    except Exception as exc:  # noqa: BLE001 — best-effort cleanup
        print(
            f"WARNING: failed to delete scratch notebook {notebook_id}: {exc}",
            file=sys.stderr,
        )


# =============================================================================
# Replay test — runs from the recorded cassette under normal CI
# =============================================================================


class TestChatMultiSource:
    """Multi-source streaming-chat ask: recording + replay."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    async def test_ask_with_five_explicit_sources(self) -> None:
        """A chat ask against five explicit sources round-trips successfully.

        Recording-mode behavior: create scratch notebook + five sources
        outside the cassette context, enter the cassette context for
        exactly the ``ask`` call, then exit and delete the notebook.

        Replay-mode behavior: read the recorded ``notebook_id`` and
        ``source_ids`` out of the cassette, pass them back into ``ask``
        so the request body matches the cassette's recorded payload at
        every slot the ``freq`` matcher inspects (param count + slot 7
        notebook_id), then assert the recorded answer is non-empty.
        """
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            if _vcr_record_mode:
                notebook_id, source_ids = await _seed_scratch_notebook(client)
                try:
                    with notebooklm_vcr.use_cassette(
                        CASSETTE_NAME,
                        # Opt-in to the ``freq`` body matcher. The
                        # streaming-chat POST has no ``rpcids`` query
                        # param so the body-aware matcher is the only way
                        # to disambiguate it from any future chat
                        # cassette that hits the same path.
                        match_on=[
                            "method",
                            "scheme",
                            "host",
                            "port",
                            "path",
                            "freq",
                        ],
                    ):
                        result = await client.chat.ask(
                            notebook_id,
                            QUESTION,
                            source_ids=source_ids,
                        )
                finally:
                    await _teardown_scratch_notebook(client, notebook_id)
            else:
                # Replay path: discover the recorded inputs from the
                # cassette so the request the client builds matches the
                # request the cassette holds. Single helper call —
                # ``params`` carries the sources_array at slot 0 and
                # ``recorded_notebook_id`` is what slot 7 already
                # carries (validated by the helper).
                params, recorded_notebook_id = _load_cassette_params()
                recorded_source_ids = _extract_source_ids_from_nested(params[0])
                with notebooklm_vcr.use_cassette(
                    CASSETTE_NAME,
                    match_on=[
                        "method",
                        "scheme",
                        "host",
                        "port",
                        "path",
                        "freq",
                    ],
                ):
                    result = await client.chat.ask(
                        recorded_notebook_id,
                        QUESTION,
                        source_ids=recorded_source_ids,
                    )

        # The streaming-chat endpoint always returns a non-empty answer
        # for a well-formed multi-source ask. Empty would indicate a
        # server-side error path leaked through ``AskResult``.
        assert result is not None
        assert result.answer is not None
        assert isinstance(result.answer, str)
        assert result.answer.strip(), "chat answer should be non-empty"
        assert result.conversation_id is not None

    def test_cassette_carries_all_five_source_ids_with_correct_nesting(
        self,
    ) -> None:
        """The recorded ``f.req`` body has all five source-ids at depth 2.

        This is the regression guard for the fragile nested-list encoder
        (audit C-class finding on ``_build_chat_request``). Failure
        modes the test catches:

        1. ``sources_array`` collapsed to depth 1 — slot[0] would NOT be
           a single-element list-of-list; the
           ``_extract_source_ids_from_nested`` parser raises.
        2. Source list truncated — the recorded length deviates from
           ``SOURCE_COUNT``.
        3. Source IDs lost their string shape — the parser raises
           because a slot's terminal value is not a non-empty ``str``.

        The cassette is the source of truth; we parse it directly
        rather than relying on the live client's behavior so the
        assertion is independent of the implementation under test.
        """
        params, _notebook_id = _load_cassette_params()

        # Param count must match the 9-slot shape ``_build_chat_request``
        # documents. A drift here is itself a regression worth shouting
        # about (audit C3-class stale-cassette issue).
        assert len(params) == 9, (
            f"chat f.req param count drift: expected 9, got {len(params)}. params={params!r}"
        )

        sources_array = params[0]
        assert isinstance(sources_array, list), (
            f"slot 0 (sources_array) must be a list, got {type(sources_array).__name__}"
        )
        assert len(sources_array) == SOURCE_COUNT, (
            f"recorded sources_array has {len(sources_array)} entries, expected {SOURCE_COUNT}"
        )

        # Round-trip through the strict parser — raises with a clear
        # message if any slot deviates from ``[[id]]``.
        extracted_ids = _extract_source_ids_from_nested(sources_array)
        assert len(extracted_ids) == SOURCE_COUNT

        # Source-ids should look like UUIDs (Google-issued source IDs
        # are UUIDs). The exact value of each id is not asserted —
        # rerecording yields fresh ids and the test must stay green.
        uuid_pat = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
        for sid in extracted_ids:
            assert uuid_pat.match(sid), f"recorded source id is not a UUID: {sid!r}"

        # Slot positions that ``_build_chat_request`` documents — assert
        # them so any reshuffle of the 9-param shape fails this test
        # immediately rather than silently changing wire format.
        assert params[1] == QUESTION, (
            f"slot 1 (question) drift: expected {QUESTION!r}, got {params[1]!r}"
        )
        assert params[3] == [2, None, [1], [1]], f"slot 3 (mode tuple) drift: got {params[3]!r}"
        assert params[8] == 1, f"slot 8 (always-1) drift: got {params[8]!r}"
