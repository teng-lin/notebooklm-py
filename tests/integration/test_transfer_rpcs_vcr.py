"""Live-recorded regressions for the #2283 RPC family.

``NextStepSuggestions`` (``OcvKNc``), ``GetArtifactCustomizationChoices``
(``sqTeoe``), ``AddSourcesAsync`` (``X1snv``), ``AppendSource`` (``QsNTEd``),
``CopySourcesAsync`` (``R27wvc``) and ``CopyArtifactsAsync`` (``mKDdke``).

The two reads run against the maintainer's read-only notebook; the mutation
lifecycle creates its own scratch notebook, copies one source out of the
read-only notebook and one completed artifact out of the generation notebook
into it, and deletes the scratch notebook afterwards — the two donors are only
read. Record with::

    NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID=<id> NOTEBOOKLM_GENERATION_NOTEBOOK_ID=<id> \\
      NOTEBOOKLM_VCR_RECORD=1 uv run pytest tests/integration/test_transfer_rpcs_vcr.py -v

Every UUID is scrubbed to a reserved placeholder (``ResourceIdCassetteScrubber``),
so the fallbacks below are the placeholders baked into the recordings. Assertions
are re-record-safe: they pin decoded *shape* (ids present, distinct, typed
fields), never recorded text.
"""

from __future__ import annotations

import asyncio
import os

import pytest

from notebooklm.types import (
    ArtifactCustomizationChoices,
    CopiedArtifact,
    CopiedSource,
    CustomizationChoice,
    NextStepSuggestion,
    ReportPreset,
)
from tests.integration.conftest import skip_no_cassettes
from tests.vcr_config import ResourceIdCassetteScrubber, notebooklm_vcr

from ._vcr_helpers import vcr_client

pytestmark = [pytest.mark.vcr, skip_no_cassettes]

# Placeholders assigned in first-seen order by the scrubber during recording.
READ_ONLY_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID", "00000000-0000-4000-8000-000000000001"
)
GENERATION_NOTEBOOK_ID = os.environ.get(
    "NOTEBOOKLM_GENERATION_NOTEBOOK_ID", "00000000-0000-4000-8000-000000000002"
)
_RECORDING = os.environ.get("NOTEBOOKLM_VCR_RECORD", "").casefold() in ("1", "true", "yes")
_RESOURCE_IDS = ResourceIdCassetteScrubber()
_URL = "https://example.com/"


async def _settle(seconds: float) -> None:
    """Give the live backend time while recording; a no-op on replay."""
    if _RECORDING:
        await asyncio.sleep(seconds)


@notebooklm_vcr.use_cassette(
    "notebooks_suggest_next_steps.yaml",
    before_record_request=_RESOURCE_IDS.scrub_request,
    before_record_response=_RESOURCE_IDS.scrub_response,
)
@pytest.mark.asyncio
async def test_live_suggest_next_steps_returns_grounded_questions() -> None:
    """``OcvKNc`` returns ``[question, MagicArtifactType]`` rows for a populated notebook."""
    async with vcr_client() as client:
        suggestions = await client.notebooks.suggest_next_steps(READ_ONLY_NOTEBOOK_ID)

    assert suggestions
    assert all(isinstance(step, NextStepSuggestion) for step in suggestions)
    assert all(step.question and type(step.type_code) is int for step in suggestions)


@notebooklm_vcr.use_cassette(
    "artifacts_customization_choices.yaml",
    before_record_request=_RESOURCE_IDS.scrub_request,
    before_record_response=_RESOURCE_IDS.scrub_response,
)
@pytest.mark.asyncio
async def test_live_customization_choices_decode_all_four_families() -> None:
    """``sqTeoe`` serves the audio / video / slide-deck / report tables (account-level)."""
    async with vcr_client() as client:
        choices = await client.artifacts.get_customization_choices(READ_ONLY_NOTEBOOK_ID)

    assert isinstance(choices, ArtifactCustomizationChoices)
    for family in (choices.audio, choices.video, choices.slide_deck):
        assert family
        assert all(isinstance(item, CustomizationChoice) for item in family)
        assert all(item.code > 0 and item.title for item in family)
        assert len({item.code for item in family}) == len(family)
    assert choices.reports
    assert all(isinstance(preset, ReportPreset) for preset in choices.reports)
    assert all(preset.report_type and preset.directive for preset in choices.reports)


@notebooklm_vcr.use_cassette(
    "sources_transfer_lifecycle.yaml",
    before_record_request=_RESOURCE_IDS.scrub_request,
    before_record_response=_RESOURCE_IDS.scrub_response,
)
@pytest.mark.timeout(600)
@pytest.mark.asyncio
async def test_live_transfer_lifecycle_on_a_scratch_notebook() -> None:
    """AddSourcesAsync → AppendSource → CopySourcesAsync → CopyArtifactsAsync, then clean up.

    The scratch notebook exists only for the duration of the test, including
    during live recording; the read-only and generation donors are only read.
    """
    async with vcr_client() as client:
        scratch = await client.notebooks.create("VCR Transfer Regression")
        try:
            # AddSourcesAsync: one non-blocking call returns the queued stub rows.
            queued = await client.sources.add_urls_async(scratch.id, [_URL])
            assert len(queued) == 1
            assert queued[0].id
            await _settle(8)
            ready = await client.sources.wait_until_ready(
                scratch.id, queued[0].id, timeout=120, initial_interval=0.5
            )
            assert ready.id == queued[0].id

            # AppendSource: empty reply on success; the block lands in the fulltext.
            before = await client.sources.get_fulltext(scratch.id, queued[0].id)
            await client.sources.append_text(
                scratch.id, queued[0].id, "\n\nVCR APPEND MARKER", header="VCR"
            )
            await _settle(3)
            after = await client.sources.get_fulltext(scratch.id, queued[0].id)
            assert len(after.content) > len(before.content)

            # CopySourcesAsync: first donor source -> the scratch notebook.
            donor_sources = await client.sources.list(READ_ONLY_NOTEBOOK_ID)
            assert donor_sources
            copied_sources = await client.sources.copy(
                READ_ONLY_NOTEBOOK_ID, [donor_sources[0].id], scratch.id
            )
            assert len(copied_sources) == 1
            assert isinstance(copied_sources[0], CopiedSource)
            assert copied_sources[0].original_id == donor_sources[0].id
            assert copied_sources[0].source.id
            assert copied_sources[0].source.id != donor_sources[0].id

            # CopyArtifactsAsync: first completed donor artifact -> the scratch notebook.
            donor_artifacts = [
                artifact
                for artifact in await client.artifacts.list(GENERATION_NOTEBOOK_ID)
                if artifact.is_completed
            ]
            assert donor_artifacts
            copied_artifacts = await client.artifacts.copy(
                GENERATION_NOTEBOOK_ID, [donor_artifacts[0].id], scratch.id
            )
            assert len(copied_artifacts) == 1
            assert isinstance(copied_artifacts[0], CopiedArtifact)
            assert copied_artifacts[0].original_id == donor_artifacts[0].id
            assert copied_artifacts[0].artifact.id
            assert copied_artifacts[0].artifact.id != donor_artifacts[0].id
            assert copied_artifacts[0].artifact.kind == donor_artifacts[0].kind
        finally:
            await client.notebooks.delete(scratch.id)
