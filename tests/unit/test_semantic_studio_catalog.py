"""Focused service-level tests for the neutral P5.1 Studio catalog."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._semantic.operations import CallPolicy, Operation
from notebooklm._semantic.records import (
    ARTIFACT_CATALOG_DEF,
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    MIND_MAP_LIST_DEF,
    ArtifactCatalogInput,
    ArtifactCatalogResult,
    ArtifactGetInput,
    ArtifactGetResult,
    ArtifactListInput,
    ArtifactListResult,
    ArtifactMediaRecord,
    ArtifactRecord,
    MindMapListInput,
    MindMapListResult,
    MindMapRecord,
)
from notebooklm._studio import StudioCatalog
from tests._fixtures.recording_backend import RecordingBackend


def test_artifact_records_are_frozen_slotted_and_definitions_are_closed() -> None:
    record = ArtifactRecord(
        id="artifact-id",
        title="Artifact",
        family="audio",
        status="completed",
        media_urls=(ArtifactMediaRecord("https://example.invalid/audio", "progressive"),),
    )
    values = (
        record,
        ArtifactListInput("notebook-id", "audio"),
        ArtifactListResult((record,)),
        ArtifactGetInput("notebook-id", "artifact-id"),
        ArtifactGetResult(record),
    )

    assert all(not hasattr(value, "__dict__") for value in values)
    assert all(value == replace(value) for value in values)
    assert ARTIFACT_LIST_DEF.key is Operation.ARTIFACT_LIST
    assert ARTIFACT_GET_DEF.key is Operation.ARTIFACT_GET
    assert ARTIFACT_LIST_DEF.policy is ARTIFACT_GET_DEF.policy is CallPolicy.READ
    with pytest.raises(FrozenInstanceError):
        record.__setattr__("title", "changed")
    assert "example.invalid" not in repr(record)


def _merge_backend(
    *,
    artifacts: tuple[ArtifactRecord, ...] = (),
    mind_maps: tuple[MindMapRecord, ...] = (),
) -> RecordingBackend:
    backend = RecordingBackend()
    backend.set_result(ARTIFACT_CATALOG_DEF, ArtifactCatalogResult(artifacts))
    backend.set_result(MIND_MAP_LIST_DEF, MindMapListResult(mind_maps))
    return backend


@pytest.mark.asyncio
async def test_catalog_filters_neutral_records_and_skips_the_merge_for_one_family() -> None:
    """``list_records`` stays record-only (P10 I1); public projection is the
    facade's job — see ``ArtifactsAPI.list`` in the VCR/integration suite
    (e.g. ``tests/integration/test_artifacts_integration.py``) for the
    ``ArtifactType`` assertion this test previously carried."""
    audio = ArtifactRecord("audio-id", "Audio", "audio", "completed")
    report = ArtifactRecord("report-id", "Report", "report", "completed")
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    backend = _merge_backend(artifacts=(audio, report))
    catalog = StudioCatalog(backend)

    records = await catalog.list_records("notebook-id", "audio", deadline=deadline)

    assert [item.id for item in records] == ["audio-id"]
    assert [invocation.operation for invocation in backend.invocations] == [
        Operation.ARTIFACT_CATALOG
    ]
    assert backend.invocations[0].value == ArtifactCatalogInput("notebook-id")
    assert backend.invocations[0].deadline is deadline


@pytest.mark.asyncio
async def test_catalog_get_record_selects_a_note_backed_identity_from_the_merge() -> None:
    """``get_record`` stays record-only (P10 I1); public projection is the
    facade's job — see ``ArtifactsAPI.get_or_none`` in the VCR/integration
    suite for the ``ArtifactType`` assertion this test previously carried."""
    backend = _merge_backend(
        mind_maps=(MindMapRecord("mind-map-id", "notebook-id", "Map", "note_backed"),)
    )
    catalog = StudioCatalog(backend)

    artifact = await catalog.get_record("notebook-id", "mind-map-id")

    assert artifact is not None
    assert artifact.id == "mind-map-id"
    assert artifact.family == "mind_map"
    assert [invocation.operation for invocation in backend.invocations] == [
        Operation.ARTIFACT_CATALOG,
        Operation.MIND_MAP_LIST,
    ]
    assert backend.invocations[1].value == MindMapListInput("notebook-id", supplemental=True)
