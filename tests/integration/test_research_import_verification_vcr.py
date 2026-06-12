"""VCR replay of the ``import_sources_with_verification`` happy path.

This module captures and replays the **happy path** of
:meth:`ResearchAPI.import_sources_with_verification` — the timeout-tolerant
deep-research import method (issue #315). The recording drives a *fast*
research against a scratch notebook, waits for completion, then imports the
discovered sources through the verification wrapper.

What the cassette locks
-----------------------
``import_sources_with_verification`` snapshots the notebook's baseline source
list before the import (``GET_NOTEBOOK``) and then calls ``IMPORT_RESEARCH``.
On the happy path (no ``RPCTimeoutError``) it returns the imported entries
directly from the single ``IMPORT_RESEARCH`` response — the baseline snapshot
is taken but the post-timeout probe/retry branch is never entered. The
recording therefore locks the actual wire for:

* ``GET_NOTEBOOK`` — the pre-import baseline ``sources.list`` snapshot.
* ``IMPORT_RESEARCH`` — the import itself, returning the imported entries.

Both rpcids are asserted on replay via :func:`_cassette_request_rpcids` so the
assertion is **rotation-proof**: it pins the :class:`~notebooklm.rpc.RPCMethod`
constants (``rpc/types.py`` is the single source of truth), not the obfuscated
literals.

Fast vs deep
------------
The ``IMPORT_RESEARCH`` wire is identical for fast and deep research, so a
*fast* research is sufficient and cheap to record (a fast run completes in
~1-3 minutes, versus 20-40 for a deep run). The deep-only
report-markdown-source import path stays unit-tested.

Timeout / verify / retry branch is UNIT-tested
----------------------------------------------
The timeout-driven verify-and-retry branch of
``import_sources_with_verification`` cannot be exercised through VCR — VCR
replays the recorded ``IMPORT_RESEARCH`` response verbatim and cannot
synthesize the client-side :class:`~notebooklm.exceptions.RPCTimeoutError` that
triggers the probe/retry logic. That branch is covered exhaustively by
``tests/unit/test_research_import_with_verification.py`` (baseline snapshot
failure, partial-commit retry filtering, report-entry dropping, duplicate
suppression, cancellation propagation, …). This VCR test deliberately scopes
to the no-timeout happy path so replay stays deterministic and fast.

Recording
---------
In record mode (``NOTEBOOKLM_VCR_RECORD=1``) the test drives, against the live
API, on a fresh scratch notebook (UUID-suffixed title, deleted in ``finally``):

1. ``CREATE_NOTEBOOK`` — fresh scratch notebook.
2. Three ``ADD_SOURCE`` calls — substantive public Wikipedia paragraphs.
3. ``START_FAST_RESEARCH`` — kicks off fast research on the seeded notebook.
4. ``POLL_RESEARCH`` until completed (via
   :meth:`ResearchAPI.wait_for_completion`, which pins the POLL-reported task
   id — start's id differs from the poll-reported id, so this is load-bearing).
5. ``GET_NOTEBOOK`` + ``IMPORT_RESEARCH`` — the method under test imports the
   discovered web sources (capped at 3).
6. ``DELETE_NOTEBOOK`` — scratch-notebook cleanup (runs in record AND replay).

To re-record::

    export NOTEBOOKLM_VCR_RECORD=1
    uv run pytest tests/integration/test_research_import_verification_vcr.py -v -s

Fast research completes in ~1-3 minutes; use ``-s`` to watch the per-poll
progress logging.

Replay
------
``@notebooklm_vcr.use_cassette`` plus the ``fast_sleep`` fixture (no-op
``asyncio.sleep`` during replay) makes the full flow run in <30 seconds. The
default VCR matcher uses ``rpcids`` so the create / add_source / start / poll /
get_notebook / import / delete interactions are disambiguated by query string;
the repeated ``POLL_RESEARCH`` interactions match by play-count order (VCR's
default for same-key requests), which is exactly the sequential consumption the
wait loop performs.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import yaml

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod
from notebooklm.types import ConnectionLimits, ResearchStatus
from tests.integration.conftest import get_vcr_auth, skip_no_cassettes
from tests.vcr_config import _is_vcr_record_mode, notebooklm_vcr

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "research_import_verification.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

_RESEARCH_QUERY = "Compare the key themes across the sources"

# Per-test timeout override. The suite sets a global 60 s ``pytest-timeout``
# (CI hang safety net), which is too short for the live recording — fast
# research plus notebook setup can run a couple of minutes against the live
# API. We override with a generous wall-clock ceiling. During REPLAY the flow
# finishes in <30 s, so the high ceiling is inert; it only matters when
# recording.
_RECORD_TEST_TIMEOUT_SECONDS = 1800

# Maximum seconds to wait for the fast research to reach a terminal status
# during recording. Fast research is typically a 1-3 minute server-side
# operation; this bound is generous headroom.
_RESEARCH_WAIT_TIMEOUT_SECONDS = 600

# Cap on how many discovered sources we import — keeps the IMPORT_RESEARCH
# payload (and cassette) small while still exercising a multi-source import.
_MAX_IMPORT_SOURCES = 3

# PoolTimeout-resilient client config applied ONLY in record mode. Fast
# research is quick so PoolTimeout is unlikely, but the deep-research recipe
# (docs/development.md) applies these defensively: a generous pool-acquire /
# read timeout plus a SHORT ``keepalive_expiry`` so a connection left idle
# across the gap between polls is closed proactively rather than handed back
# half-dead. Reached through the PUBLIC ``NotebookLMClient`` constructor so no
# private seam is needed; replay uses the default config (empty kwargs).
_RECORD_TIMEOUT_SECONDS = 60.0
_RECORD_LIMITS = ConnectionLimits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=10.0,
)

# Source content for the scratch notebook. Three substantive Wikipedia
# paragraphs on distinct topics so the research has something thematic to
# compare and discover web sources for. Content is public-domain encyclopaedia
# text — no PII. Mirrors the deep-poll recording's ``_SCRATCH_SOURCES``.
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
            "the period from around 1760 to about 1820-1840. This transition "
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


def _is_record_mode() -> bool:
    """True when ``NOTEBOOKLM_VCR_RECORD`` enables record mode.

    Thin alias for :func:`tests.vcr_config._is_vcr_record_mode` so the
    ``NOTEBOOKLM_VCR_RECORD`` parsing has a single source of truth (it is the
    same env var and same truthy set the VCR record-mode selection uses).
    Kept as a local name so the two call sites read clearly.
    """
    return _is_vcr_record_mode()


def _recording_client_kwargs() -> dict[str, Any]:
    """PoolTimeout-resilient ``NotebookLMClient`` kwargs for RECORD mode.

    Returns the public-constructor kwargs (``timeout`` + ``limits``) that
    harden the recording client against idle-connection reuse across the
    poll gap. Returns an empty dict outside record mode so replay uses the
    default client config and the cassette plays back unchanged.
    """
    if not _is_record_mode():
        return {}
    return {"timeout": _RECORD_TIMEOUT_SECONDS, "limits": _RECORD_LIMITS}


def _cassette_request_rpcids(cassette_name: str) -> set[str]:
    """Return the set of ``rpcids`` query values across a cassette's requests.

    Reads the recorded request URIs (``...?rpcids=<id>&...``) so a test can
    assert the interaction it replays targeted a specific
    :class:`~notebooklm.rpc.RPCMethod` *by its constant* rather than re-pinning
    the obfuscated literal. When Google rotates an ID, ``rpc/types.py`` and the
    cassette rotate together and this assertion keeps holding with no edit.

    Mirrors the helper in ``test_rpc_gap_backfill_vcr.py`` (kept local so this
    module has no cross-test import dependency).
    """
    text = CASSETTE_PATH.with_name(cassette_name).read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    rpcids: set[str] = set()
    for interaction in data.get("interactions", []):
        uri = interaction.get("request", {}).get("uri", "")
        query = urlsplit(uri).query
        rpcids.update(parse_qs(query).get("rpcids", []))
    return rpcids


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch ``asyncio.sleep`` to an immediate no-op during REPLAY.

    The wait-for-completion loop interleaves ``POLL_RESEARCH`` RPCs with
    ``await asyncio.sleep(interval)`` for backoff. During cassette replay the
    wait adds nothing — the cassette already encodes server progression — so we
    replace ``asyncio.sleep`` with an immediate no-op.

    During RECORDING (``NOTEBOOKLM_VCR_RECORD=1``) the patch is a no-op so the
    live poll cadence is preserved (we don't want to hammer the API with a
    tight spin-loop). The fixture is narrow on purpose: only ``asyncio.sleep``
    itself is patched.
    """
    if _is_record_mode():
        # Record mode — preserve real cadence so the live API isn't spammed.
        return

    async def instant_sleep(_seconds: float, result: object | None = None) -> object | None:
        # Preserve ``asyncio.sleep``'s full signature (optional ``result``).
        return result

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)


class TestImportSourcesWithVerificationReplay:
    """Replays the import_sources_with_verification happy path in <30 seconds."""

    @pytest.mark.timeout(_RECORD_TEST_TIMEOUT_SECONDS)
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(CASSETTE_NAME)
    async def test_import_sources_with_verification_happy_path(self, fast_sleep: None) -> None:
        """Create scratch → seed sources → fast research → import-with-verify → cleanup.

        Drives :meth:`ResearchAPI.import_sources_with_verification` against the
        discovered web sources and asserts it returns a non-empty
        ``list[dict]`` of imported entries (each ``{"id": ..., "title": ...}``).
        Replay validates that the method snapshots the baseline (``GET_NOTEBOOK``)
        and imports (``IMPORT_RESEARCH``) on the no-timeout happy path. The
        timeout/verify/retry branch is UNIT-tested
        (``tests/unit/test_research_import_with_verification.py``) because VCR
        cannot synthesize the client-side ``RPCTimeoutError`` that triggers it.
        """
        auth = await get_vcr_auth()
        # PoolTimeout-resilient client config ONLY in record mode (empty kwargs
        # in replay, so the cassette plays back against the default config).
        async with NotebookLMClient(auth, **_recording_client_kwargs()) as client:
            # 1. Fresh scratch notebook. The UUID suffix keeps re-records
            #    distinct even if a previous run leaked an undeleted notebook.
            scratch_title = f"research-import-verify scratch {uuid.uuid4().hex[:8]}"
            notebook = await client.notebooks.create(scratch_title)
            assert notebook is not None
            notebook_id = notebook.id
            assert notebook_id, "create() must return a notebook with an id"

            try:
                # 2. Seed the notebook with three substantive text sources.
                for title, content in _SCRATCH_SOURCES:
                    source = await client.sources.add_text(
                        notebook_id, title=title, content=content
                    )
                    assert source is not None
                    assert source.id, "add_text() must return a source with an id"

                # 3. Kick off FAST research (web). The IMPORT_RESEARCH wire is
                #    identical fast-vs-deep, so fast is sufficient and cheap.
                start_result = await client.research.start(
                    notebook_id,
                    query=_RESEARCH_QUERY,
                    source="web",
                    mode="fast",
                )
                assert start_result is not None
                assert start_result.task_id, "research.start must return a task_id"
                assert start_result.mode == "fast"

                # 4. Wait for completion. ``wait_for_completion`` pins the
                #    POLL-reported task id correctly (start's id differs from
                #    the poll-reported id — see the deep-poll module). The
                #    returned task carries the pinned ``task_id`` and the
                #    discovered web ``sources``.
                task = await client.research.wait_for_completion(
                    notebook_id,
                    start_result.task_id,
                    timeout=_RESEARCH_WAIT_TIMEOUT_SECONDS,
                )
                assert task.status == ResearchStatus.COMPLETED, (
                    f"fast research did not complete: status={task.status!r}"
                )
                assert task.task_id, "completed task must carry a task_id"
                discovered = list(task.sources)[:_MAX_IMPORT_SOURCES]
                assert discovered, (
                    "fast research completed with zero discovered sources; "
                    "cannot exercise the import path. Re-record with a query "
                    "that surfaces web sources."
                )

                # 5. THE method under test. Snapshots baseline (GET_NOTEBOOK)
                #    then IMPORT_RESEARCH; returns the imported entries.
                imported = await client.research.import_sources_with_verification(
                    notebook_id,
                    task.task_id,
                    discovered,
                )

                # Replay assertion: a non-empty list of {id, title} entries.
                assert isinstance(imported, list)
                assert imported, "import_sources_with_verification returned no entries"
                for entry in imported:
                    assert isinstance(entry, dict)
                    assert entry.get("id"), f"imported entry missing id: {entry!r}"
                    assert "title" in entry, f"imported entry missing title: {entry!r}"
            finally:
                # 6. Cleanup — runs in record AND replay (the cassette has a
                #    DELETE_NOTEBOOK interaction for the replay to consume).
                await client.notebooks.delete(notebook_id)

    def test_cassette_records_baseline_snapshot_and_import(self) -> None:
        """The cassette must capture BOTH the baseline snapshot and the import.

        Asserts the recorded request rpcids include ``GET_NOTEBOOK`` (the
        pre-import ``sources.list`` baseline snapshot) AND ``IMPORT_RESEARCH``
        (the import call). Both are asserted by their
        :class:`~notebooklm.rpc.RPCMethod` constant — rotation-proof through
        ``rpc/types.py``, not string literals.
        """
        assert CASSETTE_PATH.exists(), (
            f"cassette missing: {CASSETTE_PATH}. "
            "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
        )
        rpcids = _cassette_request_rpcids(CASSETTE_NAME)
        assert RPCMethod.GET_NOTEBOOK.value in rpcids, (
            "cassette is missing the GET_NOTEBOOK baseline snapshot that "
            "import_sources_with_verification takes before importing."
        )
        assert RPCMethod.IMPORT_RESEARCH.value in rpcids, (
            "cassette is missing the IMPORT_RESEARCH interaction (the import call under test)."
        )


@pytest.mark.allow_no_vcr
def test_cassette_under_size_cap() -> None:
    """The cassette must stay under the 5 MB cap.

    If a re-record grows past 5 MB, trim redundant middle ``in_progress``
    polls by hand with a byte-exact YAML text slice (NOT ``yaml.safe_dump`` —
    it re-wraps long scalars and breaks Windows parsing). Keep
    START_FAST_RESEARCH + a couple of polls + the completed poll + the
    create / add_source / get_notebook / import / delete lifecycle.
    """
    if not CASSETTE_PATH.exists():
        pytest.skip(f"Cassette not present at {CASSETTE_PATH}; nothing to size-check.")
    size_bytes = CASSETTE_PATH.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    assert size_mb < 5.0, (
        f"Cassette {CASSETTE_PATH.name} is {size_mb:.2f} MB, over the 5 MB "
        "cap. Trim redundant middle in_progress polls (see module docstring)."
    )
