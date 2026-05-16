"""VCR-based replay of the artifact poll/rename/wait flow.

This module replaces the previously-skipped e2e test
``tests/e2e/test_artifacts.py::TestArtifactMutations::test_poll_rename_wait``
which had to be marked ``@pytest.mark.skip`` because the live
generate + ``wait_for_completion`` round trip routinely exceeded the 60 s
pytest timeout. The fix is the standard VCR trick: record once at real
wall-clock speed against the live API, then replay the cassette in tests
with ``asyncio.sleep`` monkey-patched to a no-op so the polling loop
completes in milliseconds.

Sleep-mock pattern (reusable for future long-poll tests)
--------------------------------------------------------
``wait_for_completion`` (and any other long-poll loop in
:mod:`notebooklm._artifacts`) interleaves real RPC calls with
``await asyncio.sleep(...)`` for backoff. During replay those sleeps add
nothing (the cassette already encodes the server's progression), so we
patch ``asyncio.sleep`` to an immediate no-op via the ``fast_sleep``
fixture below. The fixture is intentionally narrow — only ``asyncio.sleep``
is replaced; anything else that legitimately needs to wait (e.g. test
setup) is unaffected.

To reuse the pattern in a future test::

    async def test_my_long_poll(fast_sleep):
        async with vcr_client() as client:
            result = await client.artifacts.wait_for_completion(...)
        assert result.is_complete

Recording
---------
Recording captures the create + poll + rename chain as one cassette:

1. ``CREATE_ARTIFACT`` (``R7cb6c``) — kicks off flashcard generation.
2. Repeated ``LIST_ARTIFACTS`` (``gArtLc``) — the poll loop, one per
   ``poll_status`` call, walking the artifact from
   ``in_progress`` → ``completed``. The recorded cassette is then
   compressed (in the same recording session) to keep the first few
   PROCESSING responses plus the final COMPLETED response — enough to
   exercise the loop's backoff path without storing every intermediate
   poll the live API emits.
3. ``RENAME_ARTIFACT`` (``rc3d8d``) — exercises the rename leg of the
   original e2e flow.

To re-record::

    export NOTEBOOKLM_VCR_RECORD=1
    export NOTEBOOKLM_GENERATION_NOTEBOOK_ID=bb00c9e3-656c-4fd2-b890-2b71e1cf3814
    uv run pytest tests/integration/test_polling_vcr.py::TestPollingReplay::test_poll_rename_wait -v
    uv run python tests/scripts/compress_polling_cassette.py

The recording itself takes real wall-clock time (~30-60 s while the API
finishes generating) — that is the whole point of capturing a real
polling sequence rather than synthesising one. The follow-up compression
step trims the raw poll burst (typically 50-100+ identical PROCESSING
responses) down to a handful — enough to exercise the loop without
bloating the cassette to 5+ MB.

Replay
------
``@notebooklm_vcr.use_cassette`` plus ``fast_sleep`` makes the whole flow
run in <1 second. The default VCR matcher uses ``rpcids`` so the
``CREATE_ARTIFACT`` / ``LIST_ARTIFACTS`` / ``RENAME_ARTIFACT`` interactions
are disambiguated by query string; the repeated ``LIST_ARTIFACTS``
interactions match by play-count order (VCR's default for same-key
requests), which is exactly the sequential consumption the poll loop
performs.
"""

from __future__ import annotations

import asyncio
import os
import sys
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

# Canonical recording notebook (carries the Wikipedia "NotebookLM"
# page added during fixture seeding). The env var override is only
# consulted when recording;
# during replay the cassette drives the response regardless of notebook ID.
MUTABLE_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID",
    "bb00c9e3-656c-4fd2-b890-2b71e1cf3814",
)

# Source ID for the Wikipedia "NotebookLM" page attached to the generation
# notebook (matches ``tests/integration/test_mind_map_chain_vcr.py``).
# Passing this explicitly skips the implicit ``GET_NOTEBOOK`` call that
# ``generate_flashcards`` would otherwise issue to enumerate sources — keeping
# the cassette focused on the create/poll/rename chain.
_WIKIPEDIA_SOURCE_ID = "466b9ee3-c1ce-45ef-861c-1d4bfcd939ad"

CASSETTE_NAME = "artifacts_poll_rename_wait.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

# Minimum number of LIST_ARTIFACTS (oUz7Ic) interactions the cassette must
# carry. The whole point of this task is to capture a *real*
# polling progression — a cassette with a single immediate-completion
# response would not exercise the poll loop. Three is the minimum that
# proves the loop iterated at least twice between progression steps.
MIN_POLLING_INTERACTIONS = 3


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Monkey-patch ``asyncio.sleep`` to an immediate no-op.

    ``wait_for_completion`` interleaves ``LIST_ARTIFACTS`` polls with
    ``await asyncio.sleep(interval)`` for backoff. During cassette replay
    the wait adds nothing — the cassette already encodes server
    progression — so we replace ``asyncio.sleep`` with an immediate no-op.

    The fixture is narrow on purpose: only ``asyncio.sleep`` itself is
    patched, so anything else that genuinely needs to wait (test setup,
    library-internal awaits that don't go through ``asyncio.sleep``) is
    untouched.
    """

    async def instant_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)


class TestPollingReplay:
    """Replays the create → poll → rename → list chain in <1 second."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(CASSETTE_NAME)
    async def test_poll_rename_wait(self, fast_sleep: None) -> None:
        """Generate, wait-for-completion, rename, verify — all via cassette.

        Mirrors the original e2e test in spirit (generate one artifact,
        exercise poll_status / wait_for_completion / rename against it)
        but runs offline against a recorded cassette so it completes in
        milliseconds rather than the >60 s the live flow required.
        """
        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            # 1. Generate one flashcard artifact (CREATE_ARTIFACT).
            result = await client.artifacts.generate_flashcards(
                MUTABLE_NOTEBOOK_ID,
                source_ids=[_WIKIPEDIA_SOURCE_ID],
            )
            assert result is not None
            assert result.task_id, "generate_flashcards must return a task_id"
            task_id = result.task_id

            # 2. One explicit poll_status to exercise the single-shot path
            #    (one LIST_ARTIFACTS).
            status = await client.artifacts.poll_status(MUTABLE_NOTEBOOK_ID, task_id)
            assert status is not None
            assert status.task_id == task_id

            # 3. wait_for_completion drives the poll loop (multiple
            #    LIST_ARTIFACTS interactions, sleeps mocked).
            final_status = await client.artifacts.wait_for_completion(
                MUTABLE_NOTEBOOK_ID,
                task_id,
                initial_interval=1.0,
                max_interval=5.0,
                timeout=60.0,
            )
            assert final_status is not None
            assert final_status.is_complete or final_status.is_failed

            # 4. Rename leg — exercises RENAME_ARTIFACT regardless of whether
            #    the artifact ended ``completed`` or ``failed``. We don't
            #    re-fetch + assert title here because that would require an
            #    additional cassette interaction; the regression we care
            #    about is "the rename RPC fires without error".
            await client.artifacts.rename(
                MUTABLE_NOTEBOOK_ID,
                task_id,
                "Renamed VCR Test",
            )

    def test_cassette_has_multiple_polling_interactions(self) -> None:
        """The cassette must capture a real polling progression.

        Asserts that the cassette contains at least
        :data:`MIN_POLLING_INTERACTIONS` ``LIST_ARTIFACTS`` (``oUz7Ic``)
        interactions. A single LIST_ARTIFACTS would mean we captured an
        immediate-completion shortcut rather than a real poll loop — the
        cassette wouldn't actually exercise the
        ``wait_for_completion`` backoff / progression logic, defeating
        the purpose of this cassette.

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

        list_artifacts_count = rpcids_sequence.count(RPCMethod.LIST_ARTIFACTS.value)
        assert list_artifacts_count >= MIN_POLLING_INTERACTIONS, (
            f"Cassette only has {list_artifacts_count} LIST_ARTIFACTS interactions; "
            f"need at least {MIN_POLLING_INTERACTIONS} to exercise the polling loop. "
            "Re-record with NOTEBOOKLM_VCR_RECORD=1 against a fresh generation that "
            "doesn't complete on the first poll."
        )

        # Sanity: the cassette MUST include CREATE_ARTIFACT and
        # RENAME_ARTIFACT exactly once each so it covers the full
        # poll/rename/wait chain (not just polling in isolation).
        assert rpcids_sequence.count(RPCMethod.CREATE_ARTIFACT.value) == 1, (
            f"Expected exactly 1 CREATE_ARTIFACT, found "
            f"{rpcids_sequence.count(RPCMethod.CREATE_ARTIFACT.value)}: {rpcids_sequence}"
        )
        assert rpcids_sequence.count(RPCMethod.RENAME_ARTIFACT.value) == 1, (
            f"Expected exactly 1 RENAME_ARTIFACT, found "
            f"{rpcids_sequence.count(RPCMethod.RENAME_ARTIFACT.value)}: {rpcids_sequence}"
        )
