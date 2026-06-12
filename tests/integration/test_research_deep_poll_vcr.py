"""VCR replay of the full-lifecycle Deep Research polling loop.

This module captures the Deep Research polling loop —
``START_DEEP_RESEARCH`` plus a poll-until-terminal sequence of
``POLL_RESEARCH`` calls that reaches the ``completed`` terminal state — and
replays it with ``asyncio.sleep`` monkey-patched to a no-op so the test runs
in milliseconds.

Lifecycle scope
---------------
The recording goal is the **full** Deep Research lifecycle:
``START → N polls → completed``. Earlier attempts to record this scoped the
cassette down to a handful of in-progress polls because two consecutive
recording attempts on 2026-05-15 both failed with ``httpx.PoolTimeout``
mid-poll (after ~22 min / 31 polls the first time, ~14 min the second). The
root cause was stale-connection reuse: the default 30 s keepalive expiry plus
the multi-minute idle gaps between polls left the pool handing back a
half-dead connection on a later poll, which then timed out acquiring from the
pool.

This recording dodges that with PoolTimeout-resilient client config applied
**only in record mode** (see :func:`_recording_client_kwargs`): a generous
pool-acquire timeout plus a short ``keepalive_expiry`` so a long-idle
connection is closed proactively and a fresh one is opened on the next poll.
Both knobs are reached through the **public** ``NotebookLMClient``
constructor (``timeout`` maps to httpx's ``pool`` acquire timeout;
``limits=ConnectionLimits(keepalive_expiry=...)`` controls idle-connection
expiry), so no private seam is needed. Replay is unaffected — the resilient
kwargs are only applied when ``NOTEBOOKLM_VCR_RECORD`` is set.

Poll-until-terminal
-------------------
:func:`_poll_until_terminal` polls ``client.research.poll`` every
:data:`_POLL_INTERVAL_SECONDS` until ``poll.status`` is a TERMINAL
:class:`~notebooklm._types.research.ResearchStatus` (``COMPLETED`` or
``FAILED``), with a hard cap of :data:`_MAX_POLLS` so a hung run can't spin
forever. Task-id pinning mirrors
:meth:`ResearchAPI.wait_for_completion`: the first poll is unfiltered, and the
``task_id`` the POLL endpoint reports is captured and threaded through later
polls. This is load-bearing for Deep Research — the id from
:meth:`research.start` does NOT equal the poll-reported id (verified live), so
threading start's id would make every poll return ``NOT_FOUND``. During replay
the cassette plays each recorded response back in order, so the replay loop
performs exactly the recorded number of polls and lands on the recorded
terminal status.

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
The poll loop calls ``await asyncio.sleep(...)`` between polls to space
requests out. During cassette replay those sleeps add nothing — the cassette
already encodes the server's progression — so we patch ``asyncio.sleep`` to an
immediate no-op via the ``fast_sleep`` fixture. The fixture is intentionally
narrow: only ``asyncio.sleep`` is replaced; anything else that legitimately
needs to wait is unaffected. During RECORDING the patch is a no-op so the real
poll cadence is preserved.

Recording
---------
Recording captures (in a single cassette) the scratch-notebook lifecycle:

1. ``CREATE_NOTEBOOK`` — fresh scratch notebook.
2. Three ``ADD_TEXT_SOURCE`` calls — substantive Wikipedia paragraphs.
3. ``START_DEEP_RESEARCH`` — kicks off Deep Research on the seeded notebook.
4. ``POLL_RESEARCH`` interactions — polled until the task reaches
   ``completed`` (no_research → in_progress → completed progression).
5. ``DELETE_NOTEBOOK`` — scratch notebook cleanup.

To re-record::

    export NOTEBOOKLM_VCR_RECORD=1
    uv run pytest tests/integration/test_research_deep_poll_vcr.py -v -s

Deep Research is a multi-minute server-side operation, so the recording can
take 20–40 minutes of real wall-clock time. Use ``-s`` to watch the per-poll
progress logging.

Replay
------
``@notebooklm_vcr.use_cassette`` plus ``fast_sleep`` makes the full flow run
in <30 seconds. The default VCR matcher uses ``rpcids`` so the
create / add_text / start / poll / delete interactions are disambiguated by
query string; the repeated ``POLL_RESEARCH`` interactions match by play-count
order (VCR's default for same-key requests), which is exactly the sequential
consumption the poll loop performs.

Trimming
--------
If a full recording exceeds the 5 MB size cap (see
:func:`test_cassette_under_size_cap`), redundant *middle* ``in_progress``
polls may be removed by hand from the cassette, keeping
``START_DEEP_RESEARCH`` + the first couple ``no_research`` / ``in_progress``
polls + the ``completed`` poll + the notebook create / add_text / delete
lifecycle. The trimmed sequence still replays a faithful
no_research → in_progress → completed progression because the poll loop
consumes exactly the polls present and stops on the terminal status.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod
from notebooklm.types import ConnectionLimits, ResearchStatus
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "research_deep_poll_long.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

# Minimum POLL_RESEARCH interactions the cassette must contain to be
# meaningful. A full-lifecycle recording reaches ``completed`` after several
# polls; this floor catches a recording-script regression that captured only
# one or two polls before terminal. Kept low so legitimate hand-trimming of
# redundant middle in_progress polls (down to a no_research → in_progress →
# completed skeleton) stays above the floor.
MIN_POLL_INTERACTIONS = 3

# Terminal research states — the poll-until-terminal loop stops on either.
_TERMINAL_STATUSES: frozenset[ResearchStatus] = frozenset(
    {ResearchStatus.COMPLETED, ResearchStatus.FAILED}
)

# Hard cap on the number of polls during recording so a hung Deep Research run
# can't spin forever. At :data:`_POLL_INTERVAL_SECONDS` (30 s) this is roughly
# 40 minutes of wall-clock — comfortably longer than a typical deep run but
# bounded.
_MAX_POLLS = 80

# Per-test timeout override. The suite sets a global 60 s ``pytest-timeout``
# (CI hang safety net), which is far too short for the live recording — the
# poll-until-terminal loop can run ~40 min against the live API. We override
# with a wall-clock ceiling that covers the worst case
# (``_MAX_POLLS`` polls × ``_POLL_INTERVAL_SECONDS`` + per-poll RPC time +
# notebook setup) with headroom. During REPLAY the loop finishes in <30 s, so
# the high ceiling is inert; it only matters when recording.
_RECORD_TEST_TIMEOUT_SECONDS = 3600

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
# affects the live recording: 30-second intervals between polls so we don't
# hammer the API across the multi-minute deep-research walk.
_POLL_INTERVAL_SECONDS = 30.0

# Generous pool-acquire / read timeout for the recording client. The public
# ``NotebookLMClient(timeout=...)`` float maps to httpx's ``pool`` (connection-
# acquire) timeout AND the read/write timeouts (``Kernel.open`` sets
# ``read=write=pool=timeout``), so a single value covers both "don't raise
# PoolTimeout on a brief pool-contention spike" and "tolerate a slow poll
# response".
_RECORD_TIMEOUT_SECONDS = 60.0

# PoolTimeout-resilient connection limits for the recording client: a SHORT
# ``keepalive_expiry`` (10 s) so a connection left idle across the multi-minute
# gap between polls is closed proactively and the next poll opens a fresh one.
# The previous full-lifecycle recordings died with ``httpx.PoolTimeout`` from
# stale-connection reuse — the default 30 s keepalive let the pool hand back a
# half-dead connection on a later poll.
_RECORD_LIMITS = ConnectionLimits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=10.0,
)


def _is_record_mode() -> bool:
    """True when ``NOTEBOOKLM_VCR_RECORD`` enables record mode."""
    return os.environ.get("NOTEBOOKLM_VCR_RECORD", "").lower() in ("1", "true", "yes")


def _recording_client_kwargs() -> dict[str, Any]:
    """PoolTimeout-resilient ``NotebookLMClient`` kwargs for RECORD mode.

    Returns the public-constructor kwargs that harden the recording client
    against the long idle poll window that aborted the previous full-lifecycle
    recordings:

    * ``timeout`` — a generous pool-acquire / read timeout
      (:data:`_RECORD_TIMEOUT_SECONDS`) so a brief pool-contention spike or a
      slow poll response doesn't raise ``PoolTimeout`` / ``ReadTimeout``.
    * ``limits`` — :data:`_RECORD_LIMITS` with a SHORT ``keepalive_expiry`` so
      a connection left idle across the multi-minute gap between polls is
      closed proactively and the next poll opens a fresh one.

    Returns an empty dict outside record mode, so replay uses the default
    client config and the cassette plays back unchanged.
    """
    if not _is_record_mode():
        return {}
    return {"timeout": _RECORD_TIMEOUT_SECONDS, "limits": _RECORD_LIMITS}


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
    hammer the API with hundreds of duplicate POLL_RESEARCH calls. Without
    this guard, recording would behave like a tight spin-loop and likely
    trigger rate limiting before the research completed.

    The fixture is narrow on purpose: only ``asyncio.sleep`` itself is
    patched, so anything else that genuinely needs to wait (test setup,
    library-internal awaits that don't go through ``asyncio.sleep``) is
    untouched.
    """
    if _is_record_mode():
        # Record mode — preserve real cadence so the live API isn't spammed.
        return

    async def instant_sleep(_seconds: float, result: object | None = None) -> object | None:
        # Preserve ``asyncio.sleep``'s full signature: it accepts an optional
        # ``result`` value to return after the sleep. Nothing in this repo
        # currently uses it, but matching the stdlib signature keeps the
        # monkey-patch drop-in for any future caller.
        return result

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)


async def _poll_until_terminal(
    client: NotebookLMClient,
    notebook_id: str,
) -> list[Any]:
    """Poll Deep Research until a terminal status, returning every poll result.

    Polls ``client.research.poll`` every :data:`_POLL_INTERVAL_SECONDS` until
    ``poll.status`` is a TERMINAL :class:`ResearchStatus` (``COMPLETED`` or
    ``FAILED``). A hard cap of :data:`_MAX_POLLS` bounds the loop so a hung run
    can't spin forever.

    Task-id pinning mirrors :meth:`ResearchAPI.wait_for_completion`: the FIRST
    poll is unfiltered (``task_id=None``), and the ``task_id`` the POLL
    endpoint reports for the single in-flight task is captured and threaded
    through subsequent polls as the discriminator. This is load-bearing for
    Deep Research — the ``task_id`` returned by :meth:`research.start` does NOT
    equal the one the poll endpoint reports (verified live: start returns one
    UUID, poll reports a different UUID for the same task), so threading
    start's id would make every poll return ``NOT_FOUND``. The unfiltered
    first poll is unambiguous because the scratch notebook has exactly one
    research task in flight.

    Only ``COMPLETED`` and ``FAILED`` break the loop. ``NOT_FOUND`` /
    ``NO_RESEARCH`` are NOT treated as terminal — they continue polling
    (mirrors ``wait_for_completion``'s replication-lag policy: a pinned task
    temporarily absent from a poll is a transient condition, not an end
    state), bounded by :data:`_MAX_POLLS`.

    The result list is returned in poll order. During replay the cassette
    plays each recorded poll response back in order, so the loop performs
    exactly the recorded number of polls and lands on the recorded terminal
    status.
    """
    results: list[Any] = []
    pinned_task_id: str | None = None
    for poll_index in range(_MAX_POLLS):
        poll = await client.research.poll(notebook_id, task_id=pinned_task_id)
        # Defensive narrowing: poll() returns a ResearchTask (never None), but
        # asserting keeps the fail-fast invariant explicit and guards the
        # attribute access below against an unexpected empty response.
        assert poll is not None, "research.poll must return a ResearchTask, not None"
        results.append(poll)
        # Capture the POLL-reported task id the first time the task surfaces,
        # then pin it for every later poll (mirrors wait_for_completion).
        if pinned_task_id is None and poll.task_id:
            pinned_task_id = poll.task_id
        if _is_record_mode():
            print(  # noqa: T201 — record-mode progress, visible under pytest -s
                f"[deep-research record] poll {poll_index + 1}/{_MAX_POLLS}: "
                f"status={poll.status} task_id={poll.task_id or '<none>'}"
            )
        if poll.status in _TERMINAL_STATUSES:
            break
        await asyncio.sleep(_POLL_INTERVAL_SECONDS)
    return results


class TestDeepResearchPollReplay:
    """Replays the full deep-research lifecycle to ``completed`` in <30 seconds."""

    # ``pytest.mark.vcr`` is applied module-wide via ``pytestmark`` — no need
    # to repeat it here.
    @pytest.mark.timeout(_RECORD_TEST_TIMEOUT_SECONDS)
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(CASSETTE_NAME)
    async def test_deep_research_polling_loop(self, fast_sleep: None) -> None:
        """Create scratch notebook → add sources → deep research → poll to completed → cleanup.

        Drives the poll-until-terminal loop: polls until the task reaches a
        terminal :class:`ResearchStatus`, then asserts the final status is
        ``COMPLETED``. Replay validates that the client correctly threads
        ``task_id`` through each poll AND that the recorded lifecycle reaches
        the ``completed`` terminal state (not just exercises the iteration
        path).
        """
        auth = await get_vcr_auth()
        # PoolTimeout-resilient client config ONLY in record mode (empty kwargs
        # in replay, so the cassette plays back against the default config).
        async with NotebookLMClient(auth, **_recording_client_kwargs()) as client:
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
                start_task_id = start_result.task_id
                assert start_task_id, "research.start must return a task_id"
                assert start_result.mode == "deep"

                # 4. Poll until terminal. Drives polls every
                #    _POLL_INTERVAL_SECONDS in record mode; during replay the
                #    cassette plays each recorded response in order until the
                #    terminal status is consumed. The poll loop pins the
                #    POLL-reported task id (NOT start's id — they differ for
                #    Deep Research; see _poll_until_terminal).
                polls = await _poll_until_terminal(client, notebook_id)
                assert len(polls) >= 1, "poll loop must make at least one poll"

                # Every poll must return a status-shaped result. We don't pin
                # the intermediate statuses because the API emits several
                # states across the loop:
                #  * ``no_research`` — early polls before Deep Research has
                #    registered the task in the poll endpoint (empty task_id).
                #  * ``in_progress`` — once the task is visible the poll echoes
                #    a task_id back so callers can correlate.
                #  * ``completed`` — terminal.
                #
                # Once the task surfaces, its POLL-reported task_id must be
                # stable across the rest of the loop (the same task, not a
                # sibling). We assert internal consistency of the poll stream
                # rather than equality with start's id, because Deep Research's
                # start id and poll id differ by design.
                seen_poll_task_id: str | None = None
                polls_with_task_id = 0
                for poll in polls:
                    assert poll.status
                    poll_task_id = poll.task_id
                    if poll_task_id:
                        polls_with_task_id += 1
                        if seen_poll_task_id is None:
                            seen_poll_task_id = poll_task_id
                        else:
                            assert poll_task_id == seen_poll_task_id, (
                                "poll-reported task_id changed mid-loop: "
                                f"{seen_poll_task_id!r} -> {poll_task_id!r}"
                            )

                # The cassette must actually EXERCISE the task-id pinning path
                # this PR introduces: at least two polls must surface the same
                # non-empty task_id, so a later filtered poll reuses the id the
                # first sighting captured. A cassette where task_id only appears
                # on the final terminal poll would pass the consistency check
                # above without the pinned-filter path ever running.
                assert polls_with_task_id >= 2, (
                    "expected the cassette to include at least two polls with "
                    "the same non-empty task_id so replay exercises task-id "
                    f"pinning; saw {polls_with_task_id}."
                )

                # The recorded lifecycle MUST reach a terminal state, and that
                # terminal state MUST be ``COMPLETED`` (enum membership, not a
                # brittle string). This is the whole point of the full-
                # lifecycle recording: the prior scoped cassette never reached
                # a terminal poll.
                final = polls[-1]
                assert final.status in _TERMINAL_STATUSES, (
                    f"poll loop ended on non-terminal status {final.status!r}; "
                    "the cassette must record a poll reaching a terminal state."
                )
                assert final.status == ResearchStatus.COMPLETED, (
                    f"expected the recorded lifecycle to reach COMPLETED, got {final.status!r}."
                )
            finally:
                # 5. Cleanup — runs in record AND replay (the cassette has a
                #    DELETE_NOTEBOOK interaction for the replay to consume).
                #    Using ``finally`` so a mid-flow failure during recording
                #    still drops the scratch notebook.
                await client.notebooks.delete(notebook_id)

    def test_cassette_reaches_completed(self) -> None:
        """The cassette must capture a poll-to-completion lifecycle.

        Asserts that the cassette contains at least
        :data:`MIN_POLL_INTERACTIONS` ``POLL_RESEARCH`` (``e3bVqc``)
        interactions plus the bookend RPCs (CREATE_NOTEBOOK, three
        ADD_TEXT_SOURCE, START_DEEP_RESEARCH, DELETE_NOTEBOOK), and that the
        final POLL_RESEARCH interaction decodes to a ``COMPLETED`` research
        task.

        The terminal-status check decodes the last poll body through the
        project's own ``decode_response`` + ``parse_research_task_models``
        pipeline rather than grepping the raw body for the literal string
        ``"completed"`` — the wire payload encodes the status numerically, so
        a substring grep would never find the word. Decoding through the real
        parser keeps the assertion faithful to what the client sees while
        still parsing the cassette directly (independent of the live API).
        """
        # Imported here (not at module top) to keep the decode/parse
        # dependency local to this cassette-inspection helper.
        from notebooklm._research_task_parser import parse_research_task_models
        from notebooklm.rpc import decode_response

        assert CASSETTE_PATH.exists(), (
            f"cassette missing: {CASSETTE_PATH}. "
            "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
        )

        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        # Extract the rpcids query param from every batchexecute interaction
        # in the order they were recorded, tracking the last POLL_RESEARCH
        # interaction so we can decode its response body.
        from urllib.parse import parse_qs, urlparse

        rpcids_sequence: list[str] = []
        last_poll_body: str = ""
        for interaction in cassette.get("interactions", []):
            uri = interaction.get("request", {}).get("uri", "")
            if "/batchexecute" not in uri:
                continue
            qs = parse_qs(urlparse(uri).query)
            for rpc_id in qs.get("rpcids", []):
                rpcids_sequence.append(rpc_id)
                if rpc_id == RPCMethod.POLL_RESEARCH.value:
                    body = interaction.get("response", {}).get("body", {})
                    string = body.get("string", "")
                    if isinstance(string, bytes):
                        string = string.decode("utf-8", errors="replace")
                    last_poll_body = string

        poll_count = rpcids_sequence.count(RPCMethod.POLL_RESEARCH.value)
        assert poll_count >= MIN_POLL_INTERACTIONS, (
            f"Cassette only has {poll_count} POLL_RESEARCH interactions; "
            f"need at least {MIN_POLL_INTERACTIONS} to capture the poll-to-"
            "completion lifecycle. Re-record with NOTEBOOKLM_VCR_RECORD=1."
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

        # The final POLL_RESEARCH response must decode to a COMPLETED research
        # task — the terminal state the prior scoped-down cassette could never
        # reach. Decode through the real pipeline and assert on the parsed
        # ResearchStatus (enum membership, not a brittle string grep).
        assert last_poll_body, "no POLL_RESEARCH interaction found in cassette"
        result = decode_response(last_poll_body, RPCMethod.POLL_RESEARCH.value)
        tasks = parse_research_task_models(result)
        assert tasks, "final POLL_RESEARCH decoded to zero research tasks"
        final_status = tasks[0].status
        assert final_status == ResearchStatus.COMPLETED, (
            f"final POLL_RESEARCH decodes to status {final_status!r}, not "
            f"COMPLETED; the cassette does not reach the completed terminal "
            "state. Re-record with NOTEBOOKLM_VCR_RECORD=1."
        )


@pytest.mark.allow_no_vcr
def test_cassette_under_size_cap() -> None:
    """The cassette must stay under the 5 MB cap.

    The cassette is explicitly capped at 5 MB. If a full-lifecycle recording
    grows past that, trim redundant middle ``in_progress`` polls by hand —
    keep START_DEEP_RESEARCH + the first couple no_research / in_progress
    polls + the completed poll + the create / add_text / delete lifecycle (see
    the module docstring "Trimming" section). This assertion MUST pass once
    mitigations are applied — keeping it green is the gate that enforces the
    cap on future re-records.
    """
    if not CASSETTE_PATH.exists():
        pytest.skip(f"Cassette not present at {CASSETTE_PATH}; nothing to size-check.")
    size_bytes = CASSETTE_PATH.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    # Use ``< 5.0`` rather than ``<= 5.0`` so a re-record that creeps to
    # exactly 5 MB also fails — the cap is intentionally a hard ceiling.
    assert size_mb < 5.0, (
        f"Cassette {CASSETTE_PATH.name} is {size_mb:.2f} MB, over the 5 MB "
        "cap. Trim redundant middle in_progress polls (see module docstring "
        "'Trimming') and re-verify replay."
    )
