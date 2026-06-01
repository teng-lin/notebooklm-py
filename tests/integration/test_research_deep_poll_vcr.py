"""VCR replay of the Deep Research polling loop (scoped-down).

This module captures the Deep Research polling loop — START_DEEP_RESEARCH plus
a handful of in-progress POLL_RESEARCH calls — and replays it with
``asyncio.sleep`` monkey-patched to a no-op so the test runs in milliseconds.

Scope note
----------
The original recording goal was the full Deep Research lifecycle
(START → 30+ polls → ``completed`` terminal state). In practice the local
httpx connection pool can't sustain the multi-minute idle waits between
polls — two consecutive recording attempts on 2026-05-15 both failed with
``httpx.PoolTimeout`` mid-poll (after ~22 min / 31 polls captured the first
time; after ~14 min the second). Per the plan's documented fallback option,
this test scopes the recording down to :data:`RECORD_POLL_COUNT` in-progress
polls, which is enough to exercise the polling loop's iteration path without
requiring the cassette to reach a ``completed`` terminal state. The recording
runs in ~1–2 minutes of wall-clock instead of ~30, well under the
PoolTimeout threshold.

Source query
------------
``"Compare the key themes across the sources"`` against a scratch notebook
seeded with three substantive Wikipedia paragraphs (well-known public
encyclopaedia content — no PII, no proprietary text). The exact source titles
and bodies are stored in :data:`_SCRATCH_SOURCES`. The query is intentionally
broad so Deep Research actually does the multi-step web-research walk rather
than short-circuiting on a trivial answer.

Sleep-mock pattern (reused from ``test_polling_vcr``)
-----------------------------------------------------
The poll loop here calls ``await asyncio.sleep(...)`` between polls to space
requests out. During cassette replay those sleeps add nothing — the cassette
already encodes the server's progression — so we patch ``asyncio.sleep`` to an
immediate no-op via the ``fast_sleep`` fixture. The fixture is intentionally
narrow: only ``asyncio.sleep`` is replaced; anything else that legitimately
needs to wait is unaffected.

Recording
---------
Recording captures (in a single cassette) the scratch-notebook lifecycle:

1. ``CREATE_NOTEBOOK`` — fresh scratch notebook.
2. Three ``ADD_TEXT_SOURCE`` calls — substantive Wikipedia paragraphs.
3. ``START_DEEP_RESEARCH`` — kicks off Deep Research on the seeded notebook.
4. :data:`RECORD_POLL_COUNT` ``POLL_RESEARCH`` interactions — exercises the
   iteration path without waiting for completion.
5. ``DELETE_NOTEBOOK`` — scratch notebook cleanup.

To re-record::

    export NOTEBOOKLM_VCR_RECORD=1
    uv run pytest tests/integration/test_research_deep_poll_vcr.py -v -s

The recording runs in ~1–2 minutes of real wall-clock time (no completion
wait), so the default pytest timeout is plenty.

Replay
------
``@notebooklm_vcr.use_cassette`` plus ``fast_sleep`` makes the full flow run
in <10 seconds. The default VCR matcher uses ``rpcids`` so the
create / add_text / start / poll / delete interactions are disambiguated by
query string; the repeated ``POLL_RESEARCH`` interactions match by play-count
order (VCR's default for same-key requests), which is exactly the sequential
consumption the poll loop performs.
"""

from __future__ import annotations

import asyncio
import os
import sys
import uuid
from pathlib import Path

import pytest
import yaml

# Add tests directory to path for vcr_config import (parity with the rest of
# tests/integration/test_vcr_*.py — these files are imported by pytest with
# the repo root NOT on sys.path).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import get_vcr_auth, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm.rpc import RPCMethod  # noqa: E402
from vcr_config import notebooklm_vcr  # noqa: E402

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "research_deep_poll_long.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

# Number of POLL_RESEARCH interactions to drive during recording. Picked to
# (a) exercise the loop's iteration path (multiple polls, not just one),
# (b) keep the cassette well under the 5 MB cap (~600 KB per poll body
# observed empirically → 6 polls ≈ 3.6 MB), and (c) keep the recording
# under ~2 minutes wall-clock so it doesn't trigger the httpx PoolTimeout
# that aborted previous full-lifecycle recordings.
RECORD_POLL_COUNT = 6

# Minimum POLL_RESEARCH interactions the cassette must contain to be
# meaningful. Matches RECORD_POLL_COUNT so a regression in the recording
# script (e.g. accidentally only making 1 poll) trips this assertion.
MIN_POLL_INTERACTIONS = RECORD_POLL_COUNT

# Source content for the scratch notebook. Three substantive Wikipedia
# paragraphs on distinct topics so Deep Research has something thematic to
# compare. Content is public-domain encyclopaedia text — no PII.
_SCRATCH_SOURCES: tuple[tuple[str, str], ...] = (
    (
        "Photosynthesis (Wikipedia excerpt)",
        (
            "Photosynthesis is a biological process used by plants, algae, and "
            "certain bacteria to convert light energy, typically from the Sun, "
            "into chemical energy stored in organic compounds such as sugars. "
            "Most photosynthetic organisms also produce oxygen as a byproduct, "
            "and the oxygen released into the atmosphere maintains the aerobic "
            "respiration that most of Earth's life depends on. Photosynthetic "
            "organisms are called photoautotrophs because they produce their "
            "own food using light. In plants, algae, and cyanobacteria, "
            "photosynthesis releases oxygen, in what is called oxygenic "
            "photosynthesis. The light-dependent reactions take place on the "
            "thylakoid membranes of the chloroplasts; the light-independent "
            "reactions (the Calvin cycle) take place in the stroma."
        ),
    ),
    (
        "Industrial Revolution (Wikipedia excerpt)",
        (
            "The Industrial Revolution, sometimes divided into the First "
            "Industrial Revolution and Second Industrial Revolution, was a "
            "period of global transition of the human economy towards more "
            "efficient and stable manufacturing processes that succeeded the "
            "Agricultural Revolution, starting from Great Britain and "
            "continental Europe and the United States, that occurred during "
            "the period from around 1760 to about 1820–1840. This transition "
            "included going from hand production methods to machines; new "
            "chemical manufacturing and iron production processes; the "
            "increasing use of water power and steam power; the development "
            "of machine tools; and the rise of the mechanised factory system."
        ),
    ),
    (
        "Quantum mechanics (Wikipedia excerpt)",
        (
            "Quantum mechanics is a fundamental theory in physics that "
            "describes the behavior of nature at and below the scale of atoms. "
            "It is the foundation of all quantum physics including quantum "
            "chemistry, quantum field theory, quantum technology, and quantum "
            "information science. Classical physics, the collection of "
            "theories that existed before the advent of quantum mechanics, "
            "describes many aspects of nature at an ordinary (macroscopic) "
            "scale, but is not sufficient for describing them at small "
            "(atomic and subatomic) scales. Most theories in classical "
            "physics can be derived from quantum mechanics as an "
            "approximation valid at large (macroscopic) scale. Quantum "
            "mechanics differs from classical physics in that energy, "
            "momentum, angular momentum, and other quantities of a bound "
            "system are restricted to discrete values (quantization)."
        ),
    ),
)

_RESEARCH_QUERY = "Compare the key themes across the sources"

# Poll-loop tuning. During replay the sleeps are mocked out, so this only
# affects the live recording: 5-second intervals between the
# :data:`RECORD_POLL_COUNT` polls.
_POLL_INTERVAL_SECONDS = 5.0


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch ``asyncio.sleep`` to an immediate no-op during REPLAY.

    The poll loop interleaves ``POLL_RESEARCH`` RPCs with
    ``await asyncio.sleep(interval)`` for backoff. During cassette replay the
    wait adds nothing — the cassette already encodes server progression — so
    we replace ``asyncio.sleep`` with an immediate no-op.

    During RECORDING (``NOTEBOOKLM_VCR_RECORD=1``) the patch is a no-op so the
    live poll cadence is preserved — Deep Research is a multi-minute
    server-side operation and we want real spacing between polls so we don't
    hammer the API with thousands of duplicate POLL_RESEARCH calls. Without
    this guard, recording would behave like a tight spin-loop and likely
    trigger rate limiting before the research completed.

    The fixture is narrow on purpose: only ``asyncio.sleep`` itself is
    patched, so anything else that genuinely needs to wait (test setup,
    library-internal awaits that don't go through ``asyncio.sleep``) is
    untouched.
    """
    if os.environ.get("NOTEBOOKLM_VCR_RECORD", "").lower() in ("1", "true", "yes"):
        # Record mode — preserve real cadence so the live API isn't spammed.
        return

    async def instant_sleep(_seconds: float, result: object | None = None) -> object | None:
        # Preserve ``asyncio.sleep``'s full signature: it accepts an optional
        # ``result`` value to return after the sleep. Nothing in this repo
        # currently uses it, but matching the stdlib signature keeps the
        # monkey-patch drop-in for any future caller.
        return result

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)


async def _poll_n_times(
    client: NotebookLMClient,
    notebook_id: str,
    task_id: str,
    n: int,
) -> list[dict]:
    """Poll Deep Research ``n`` times and return every result.

    This is the scoped-down replacement for the original
    ``_poll_until_complete``. The recording was rescoped to exercise the polling
    *iteration path* rather than the full lifecycle, because the full
    lifecycle wait reliably trips ``httpx.PoolTimeout`` after ~15–20 min of
    idle polling on the maintainer's connection.

    The result list is returned in poll order. Status values can be
    ``in_progress``, ``completed``, or anything else the API uses — callers
    must not constrain on a terminal state since the recording deliberately
    stops short of completion.
    """
    results: list[dict] = []
    for _ in range(n):
        result = await client.research.poll(notebook_id, task_id=task_id)
        results.append(result)
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return results


class TestDeepResearchPollReplay:
    """Replays the deep-research polling-loop iteration path in <10 seconds."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(CASSETTE_NAME)
    async def test_deep_research_polling_loop(self, fast_sleep: None) -> None:
        """Create scratch notebook → add sources → deep research → N polls → cleanup.

        Scoped-down recording: drives :data:`RECORD_POLL_COUNT` polls instead
        of waiting for completion. The cassette captures the polling loop's
        iteration path; replay validates that the client correctly threads
        ``task_id`` through each poll and returns shaped results, not that
        Deep Research terminates with a particular final status.
        """
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            # 1. Fresh scratch notebook. The UUID suffix keeps re-records
            #    distinct even if a previous run leaked an undeleted notebook
            #    into the account.
            scratch_title = f"T8.E7 deep-research scratch {uuid.uuid4().hex[:8]}"
            notebook = await client.notebooks.create(scratch_title)
            assert notebook is not None
            notebook_id = notebook.id
            assert notebook_id, "create() must return a notebook with an id"

            try:
                # 2. Seed the notebook with three substantive text sources so
                #    Deep Research has thematic material to compare.
                for title, content in _SCRATCH_SOURCES:
                    source = await client.sources.add_text(
                        notebook_id, title=title, content=content
                    )
                    assert source is not None
                    assert source.id, "add_text() must return a source with an id"

                # 3. Kick off Deep Research.
                start_result = await client.research.start(
                    notebook_id,
                    query=_RESEARCH_QUERY,
                    source="web",
                    mode="deep",
                )
                assert start_result is not None
                task_id = start_result.get("task_id")
                assert task_id, "research.start must return a task_id"
                assert start_result.get("mode") == "deep"

                # 4. Polling iteration path. Drives RECORD_POLL_COUNT polls
                #    in record mode; during replay the cassette plays each
                #    recorded response in order. We do NOT wait for
                #    completion — see module docstring "Scope note".
                polls = await _poll_n_times(client, notebook_id, task_id, RECORD_POLL_COUNT)
                assert len(polls) == RECORD_POLL_COUNT
                for poll in polls:
                    # Every poll must return a status-shaped dict. We don't
                    # pin status to a specific value because the recording
                    # deliberately stops short of completion and the API
                    # emits several states across the loop:
                    #  * ``no_research`` — early polls before Deep Research
                    #    has registered the task in the poll endpoint (the
                    #    response is ``{"status": "no_research",
                    #    "tasks": []}`` with no ``task_id``).
                    #  * ``in_progress`` / ``pending`` / ``running`` — once
                    #    the task is visible the poll echoes ``task_id``
                    #    back so callers can correlate.
                    assert "status" in poll
                    # When ``task_id`` is present it must round-trip the
                    # value returned by ``start()``. When absent the poll
                    # is in the early ``no_research`` window before Deep
                    # Research has populated the task entry.
                    poll_task_id = poll.get("task_id")
                    if poll_task_id is not None:
                        assert poll_task_id == task_id
            finally:
                # 5. Cleanup — runs in record AND replay (the cassette has a
                #    DELETE_NOTEBOOK interaction for the replay to consume).
                #    Using ``finally`` so a mid-flow failure during recording
                #    still drops the scratch notebook.
                await client.notebooks.delete(notebook_id)

    def test_cassette_has_polling_sequence(self) -> None:
        """The cassette must capture the polling-iteration path.

        Asserts that the cassette contains at least
        :data:`MIN_POLL_INTERACTIONS` ``POLL_RESEARCH`` (``e3bVqc``)
        interactions plus the bookend RPCs (CREATE_NOTEBOOK, three
        ADD_TEXT_SOURCE, START_DEEP_RESEARCH, DELETE_NOTEBOOK). A poll count
        below the floor would indicate the recording was truncated before
        the loop got to exercise its iteration path.

        The cassette is the source of truth — we parse it directly rather
        than relying on the replay test's side effects so the assertion
        is independent of the client implementation.
        """
        assert CASSETTE_PATH.exists(), (
            f"cassette missing: {CASSETTE_PATH}. "
            "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
        )

        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        # Extract the rpcids query param from every batchexecute interaction
        # in the order they were recorded.
        from urllib.parse import parse_qs, urlparse

        rpcids_sequence: list[str] = []
        for interaction in cassette.get("interactions", []):
            uri = interaction.get("request", {}).get("uri", "")
            if "/batchexecute" not in uri:
                continue
            qs = parse_qs(urlparse(uri).query)
            for rpc_id in qs.get("rpcids", []):
                rpcids_sequence.append(rpc_id)

        poll_count = rpcids_sequence.count(RPCMethod.POLL_RESEARCH.value)
        assert poll_count >= MIN_POLL_INTERACTIONS, (
            f"Cassette only has {poll_count} POLL_RESEARCH interactions; "
            f"need at least {MIN_POLL_INTERACTIONS} to exercise the polling "
            "loop. Re-record with NOTEBOOKLM_VCR_RECORD=1."
        )

        # Sanity: the cassette MUST include at least one START_DEEP_RESEARCH
        # so the lifecycle starts where we expect it to. We use ``>= 1`` rather
        # than ``== 1`` because the live API occasionally returns ReadTimeout
        # on the initial kickoff and the core's transient-error retry loop
        # records each attempt as its own interaction.
        assert rpcids_sequence.count(RPCMethod.START_DEEP_RESEARCH.value) >= 1, (
            f"Expected at least 1 START_DEEP_RESEARCH, found "
            f"{rpcids_sequence.count(RPCMethod.START_DEEP_RESEARCH.value)}"
        )


@pytest.mark.allow_no_vcr
def test_cassette_under_size_cap() -> None:
    """The cassette must stay under the 5 MB cap.

    The cassette is explicitly capped at 5 MB. If a recording grows past
    that, the original plan documented three options in order: (1) try
    lossless body compression, (2) scope down to a 20-poll subset
    (documented in the PR description), (3) ship as-is with a
    ``defer-followup: cassette-size`` note in the PR. Whichever route is
    taken, this assertion MUST pass once mitigations are applied —
    keeping the test green here is the gate that enforces the cap on
    future re-records.
    """
    if not CASSETTE_PATH.exists():
        pytest.skip(f"Cassette not present at {CASSETTE_PATH}; nothing to size-check.")
    size_bytes = CASSETTE_PATH.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    # Use ``< 5.0`` rather than ``<= 5.0`` so a re-record that creeps to
    # exactly 5 MB also fails — the cap is intentionally a hard ceiling.
    assert size_mb < 5.0, (
        f"Cassette {CASSETTE_PATH.name} is {size_mb:.2f} MB, over the 5 MB "
        "cap. Apply size mitigations: lossless body compression, "
        "scope-down to 20 polls (document in PR), or call out the breach "
        "with a defer-followup note."
    )
