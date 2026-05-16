"""Tracer-bullet full-workflow VCR cassette (T8.E3).

End-to-end VCR cassette covering the most common user journey through the
client API: **create-notebook → add-source → ask → generate → download**.
This is the integration-tier regression test for the golden path — if a
refactor breaks any of the five public-API entry points the test exercises,
the cassette replay will surface the regression before it ships.

The five phases captured in the cassette:

1. ``client.notebooks.create`` — fresh scratch notebook for this recording.
2. ``client.sources.add_url`` + ``client.sources.wait_until_ready`` — add a
   URL source and block until it is processed.
3. ``client.chat.ask`` — single chat turn against the new notebook.
4. ``client.artifacts.generate_report`` + ``client.artifacts.wait_for_completion``
   — generate a Briefing Doc and poll until completion.
5. ``client.artifacts.download_report`` — write the markdown to a tempfile
   and assert the bytes were produced.

A final ``client.notebooks.delete`` is also recorded so the cassette captures
the full lifecycle — including teardown — and replay does not leave any
orphan state assertions.

Recording
---------
Pre-condition: ``notebooklm login`` succeeds against the default Chromium
profile. The recorder creates its own scratch notebook so it cannot
collide with parallel agents that may be using the shared
``NOTEBOOKLM_GENERATION_NOTEBOOK_ID`` notebook. The scratch notebook is
deleted as the final recorded RPC.

To re-record this cassette::

    export NOTEBOOKLM_VCR_RECORD=1
    uv run pytest tests/integration/test_workflow_tracer_vcr.py -v -s

The ``-s`` flag lets the recorder log progress between phases (useful when
the report-generation poll takes a minute or two).

Replay
------
The default VCR matcher includes ``rpcids`` (T8.A1). This cassette also
opts in to the ``freq`` body matcher (T8.A2) because the chat-ask phase
hits the streaming endpoint, whose disambiguator lives in the
form-encoded ``f.req`` body rather than the URL query string. ``rpcids``
is a no-op for the streaming endpoint and ``freq`` is a no-op for
batchexecute endpoints, so the two matchers compose cleanly.

Size budget
-----------
The cassette is the regression test for the user journey, not a media
fixture — keep it ≤ 5 MB. If a future re-record blows past the budget
because the report response body has grown, the right fix is usually to
ask a shorter question or pick a smaller report format (e.g. swap
``BRIEFING_DOC`` for a custom prompt that targets a tight word count).
Do NOT skip any of the five phases to shrink the cassette — the full
chain is the test.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest
import yaml

# Add tests/ to sys.path so vcr_config + integration/conftest resolve when
# pytest imports this module without the repo root on sys.path (parity with
# every other ``tests/integration/test_*_vcr.py`` in the repo).
sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent))
from conftest import get_vcr_auth, skip_no_cassettes  # noqa: E402
from notebooklm import NotebookLMClient  # noqa: E402
from notebooklm.rpc import RPCMethod  # noqa: E402
from notebooklm.types import ReportFormat  # noqa: E402
from vcr_config import _is_vcr_record_mode, notebooklm_vcr  # noqa: E402

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

CASSETTE_NAME = "workflow_tracer_bullet.yaml"
CASSETTE_PATH = Path(__file__).parent.parent / "cassettes" / CASSETTE_NAME

# Wikipedia is stable, fast to process, and produces a short-but-non-trivial
# source. Picked because the page is text-only (no PDFs/embeds) and the title
# is easily asserted on.
_TRACER_URL = "https://en.wikipedia.org/wiki/Tracer_bullet"
_TRACER_QUESTION = "In one sentence, what is a tracer bullet?"


@pytest.fixture
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip polling backoff during replay while preserving live record cadence."""
    if _is_vcr_record_mode():
        return

    async def instant_sleep(_seconds: float, result: object | None = None) -> object | None:
        return result

    monkeypatch.setattr(asyncio, "sleep", instant_sleep)


class TestWorkflowTracerBullet:
    """Records and replays the full create→add→ask→generate→download journey."""

    @pytest.mark.vcr
    @pytest.mark.asyncio
    @notebooklm_vcr.use_cassette(
        CASSETTE_NAME,
        # The chat-ask phase hits the streaming endpoint; ``freq`` decodes the
        # ``f.req`` body so the streaming POST is disambiguated by its param
        # shape rather than replay-order. ``rpcids`` already covers the
        # batchexecute phases via the default matcher list — re-listing it
        # here keeps the per-cassette ``match_on`` self-contained.
        match_on=["method", "scheme", "host", "port", "path", "rpcids", "freq"],
    )
    async def test_full_workflow(self, tmp_path: Path, fast_sleep: None) -> None:
        """End-to-end user journey produces a downloadable report.

        Asserts each phase's intermediate output:

        - Phase 1: ``notebooks.create`` returns a notebook with a non-empty
          UUID id.
        - Phase 2: ``sources.add_url`` returns a source with the same URL,
          and ``wait_until_ready`` produces a READY source.
        - Phase 3: ``chat.ask`` returns a non-empty answer and a
          conversation id.
        - Phase 4: ``artifacts.generate_report`` returns a task id and the
          subsequent ``wait_for_completion`` reports status=completed.
        - Phase 5: ``artifacts.download_report`` writes a non-empty file to
          the requested path.

        The final ``notebooks.delete`` call ensures the cassette captures
        the full lifecycle; we do not assert on its return value beyond
        "did not raise" since deletion semantics are exercised by other
        cassettes (``test_vcr_comprehensive``).
        """
        # Use a UUID-suffixed title so a hypothetical retry in record mode
        # cannot collide with a pre-existing notebook with the same title
        # (the ``idempotent_create`` probe would otherwise be ambiguous).
        # During replay, the cassette drives the response regardless of the
        # title we pass, so the UUID is purely a record-mode safety hatch.
        title = f"T8.E3 tracer-bullet {uuid.uuid4().hex[:8]}"

        auth = await get_vcr_auth()
        async with NotebookLMClient(auth) as client:
            # ---------------------------------------------------------------
            # Phase 1: create-notebook
            # ---------------------------------------------------------------
            notebook = await client.notebooks.create(title)
            assert notebook.id, "create-notebook must return a non-empty id"
            assert isinstance(notebook.id, str)
            notebook_id = notebook.id

            try:
                # -----------------------------------------------------------
                # Phase 2: add-source
                # -----------------------------------------------------------
                source = await client.sources.add_url(
                    notebook_id, _TRACER_URL, wait=True, wait_timeout=120.0
                )
                assert source.id, "add_url must return a source with a non-empty id"
                # ``url`` round-trip — what we sent comes back on the source.
                # (Replay drives the URL from the cassette body, so this is a
                #  replay-time invariant too.)
                assert source.url == _TRACER_URL
                # ``wait=True`` should leave the source ready by the time
                # ``add_url`` returns.
                assert source.is_ready, f"source not ready after wait: status={source.status}"

                # -----------------------------------------------------------
                # Phase 3: ask
                # -----------------------------------------------------------
                ask_result = await client.chat.ask(notebook_id, _TRACER_QUESTION)
                assert ask_result.answer, "ask must return a non-empty answer"
                assert isinstance(ask_result.answer, str)
                assert ask_result.conversation_id, "ask must return a conversation id"

                # -----------------------------------------------------------
                # Phase 4: generate (Briefing Doc — fastest, text-only artifact)
                # -----------------------------------------------------------
                gen = await client.artifacts.generate_report(
                    notebook_id, report_format=ReportFormat.BRIEFING_DOC
                )
                assert gen.task_id, "generate_report must return a task_id"
                assert gen.status != "failed", f"generation failed: {gen.error}"

                # Poll for completion. The report endpoint typically resolves
                # within 30–60 s; cap at 5 minutes to leave headroom for slow
                # backends during record-mode without bloating the cassette.
                final = await client.artifacts.wait_for_completion(
                    notebook_id,
                    gen.task_id,
                    initial_interval=2.0,
                    max_interval=10.0,
                    timeout=300.0,
                )
                assert final.status == "completed", (
                    f"report did not complete: status={final.status} error={final.error}"
                )

                # -----------------------------------------------------------
                # Phase 5: download
                # -----------------------------------------------------------
                output_path = tmp_path / "tracer_report.md"
                written_path = await client.artifacts.download_report(
                    notebook_id, str(output_path), artifact_id=gen.task_id
                )
                assert Path(written_path) == output_path
                assert output_path.exists(), "download_report must write the markdown file"
                content = output_path.read_text(encoding="utf-8")
                assert content, "downloaded report must be non-empty"
                # Briefing docs always have *some* markdown structure; a stray
                # binary-blob download would not produce any hash mark anywhere.
                assert "#" in content, (
                    "downloaded report should contain at least one markdown heading"
                )
            finally:
                # Teardown: delete the scratch notebook so the recording does
                # not leave orphan state in the user's account even if an
                # earlier assertion fails. Captured in the cassette so replay
                # also drives the delete RPC.
                deleted = await client.notebooks.delete(notebook_id)
                assert deleted is True

    def test_cassette_size_under_budget(self) -> None:
        """Cassette stays under the 5 MB budget documented in the module docstring.

        A re-record that blows past the budget is a signal to (a) re-ask a
        shorter question or (b) re-record with the recorder's
        ``before_record_response`` hook stripped, not to silently accept a
        ballooning fixture. Failing the check loudly in CI surfaces the
        issue at PR time.
        """
        assert CASSETTE_PATH.exists(), (
            f"cassette missing: {CASSETTE_PATH}. "
            "Re-record with NOTEBOOKLM_VCR_RECORD=1 — see module docstring."
        )
        size_bytes = CASSETTE_PATH.stat().st_size
        size_mb = size_bytes / (1024 * 1024)
        assert size_mb <= 5.0, (
            f"cassette exceeds 5 MB budget: {size_mb:.2f} MB. "
            "Tighten the recording or reduce response body sizes."
        )

    def test_cassette_captures_all_five_phases(self) -> None:
        """Cassette includes batchexecute RPCs for each of the five phases.

        Asserts the recorded batchexecute interactions contain the rpcids
        that prove each phase fired. We do not pin a strict ordered
        sequence (unlike ``test_mind_map_chain_vcr.py``) because the chat
        phase mixes a streaming POST with several batchexecute support
        RPCs whose order is implementation-defined — but the set of
        required rpcids is stable.

        Required RPC IDs (any order, may appear more than once):

        - ``CREATE_NOTEBOOK`` — phase 1
        - ``ADD_SOURCE`` — phase 2
        - ``GET_NOTEBOOK`` — phase 2 wait-until-ready polls (and chat
          ``get_source_ids``)
        - ``CREATE_ARTIFACT`` — phase 4
        - ``LIST_ARTIFACTS`` — phase 4 polling + phase 5 download lookup
        - ``DELETE_NOTEBOOK`` — teardown

        The streaming chat call (phase 3) does not appear in batchexecute
        rpcids; we assert its presence via the URI check below.
        """
        assert CASSETTE_PATH.exists(), f"cassette missing: {CASSETTE_PATH}"

        with CASSETTE_PATH.open(encoding="utf-8") as fh:
            cassette = yaml.safe_load(fh)

        interactions = cassette.get("interactions", [])
        assert len(interactions) >= 5, (
            f"cassette must capture at least 5 interactions; got {len(interactions)}"
        )

        from urllib.parse import parse_qs, urlparse

        recorded_rpcids: set[str] = set()
        streaming_chat_seen = False
        for interaction in interactions:
            uri = interaction.get("request", {}).get("uri", "")
            parsed = urlparse(uri)
            if "/batchexecute" in parsed.path:
                qs = parse_qs(parsed.query)
                for rpc_id in qs.get("rpcids", []):
                    recorded_rpcids.add(rpc_id)
            # Streaming chat lives on a non-batchexecute path under
            # ``/_/LabsTailwindUi/data/`` — its URI contains the service
            # name ``LabsTailwindOrchestrationService/GenerateFreeFormStreamed``
            # as a path component rather than a query param. Matching on the
            # method name (``GenerateFreeFormStreamed``) is sufficient and
            # keeps the assertion tolerant of host changes.
            if "GenerateFreeFormStreamed" in uri:
                streaming_chat_seen = True

        required = {
            RPCMethod.CREATE_NOTEBOOK.value,  # phase 1
            RPCMethod.ADD_SOURCE.value,  # phase 2
            RPCMethod.GET_NOTEBOOK.value,  # phase 2 polls + chat source-id lookup
            RPCMethod.CREATE_ARTIFACT.value,  # phase 4
            RPCMethod.LIST_ARTIFACTS.value,  # phase 4 polls + phase 5 download
            RPCMethod.DELETE_NOTEBOOK.value,  # teardown
        }
        missing = required - recorded_rpcids
        assert not missing, (
            f"cassette missing required rpcids: {sorted(missing)}. "
            f"Recorded: {sorted(recorded_rpcids)}"
        )

        assert streaming_chat_seen, (
            "cassette must include the streaming chat interaction (phase 3); "
            "no URI matched 'GenerateFreeFormStreamed'"
        )


# Skip the on-disk assertions when we are actively recording — the recorder
# writes the cassette after the test body returns, so the file does not yet
# exist at the time the shape tests run if collected in the same session.
# In replay mode these tests run normally.
if _is_vcr_record_mode():  # pragma: no cover — record-time only
    TestWorkflowTracerBullet.test_cassette_size_under_budget = pytest.mark.skip(  # type: ignore[method-assign]
        reason="Cassette not yet written during record run; replay verifies."
    )(TestWorkflowTracerBullet.test_cassette_size_under_budget)
    TestWorkflowTracerBullet.test_cassette_captures_all_five_phases = pytest.mark.skip(  # type: ignore[method-assign]
        reason="Cassette not yet written during record run; replay verifies."
    )(TestWorkflowTracerBullet.test_cassette_captures_all_five_phases)
