"""Focused service-level tests for the neutral P5.1 Studio catalog."""

from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import CallPolicy, Operation
from notebooklm._records import (
    ARTIFACT_GET_DEF,
    ARTIFACT_LIST_DEF,
    ArtifactGetInput,
    ArtifactGetResult,
    ArtifactListInput,
    ArtifactListResult,
    ArtifactMediaRecord,
    ArtifactRecord,
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


@pytest.mark.asyncio
async def test_catalog_filters_neutral_records_and_records_one_typed_invocation() -> None:
    """``list_records`` stays record-only (P10 I1); public projection is the
    facade's job — see ``ArtifactsAPI.list`` in the VCR/integration suite
    (e.g. ``tests/integration/test_artifacts_integration.py``) for the
    ``ArtifactType`` assertion this test previously carried."""
    audio = ArtifactRecord("audio-id", "Audio", "audio", "completed")
    report = ArtifactRecord("report-id", "Report", "report", "completed")
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)
    backend = RecordingBackend()
    backend.set_result(ARTIFACT_LIST_DEF, ArtifactListResult((audio, report)))
    catalog = StudioCatalog(backend)

    records = await catalog.list_records("notebook-id", "audio", deadline=deadline)

    assert [item.id for item in records] == ["audio-id"]
    assert backend.invocations[0].value == ArtifactListInput("notebook-id", "audio")
    assert backend.invocations[0].deadline is deadline


@pytest.mark.asyncio
async def test_catalog_get_record_returns_the_neutral_row_without_a_second_invocation() -> None:
    """``get_record`` stays record-only (P10 I1); public projection is the
    facade's job — see ``ArtifactsAPI.get_or_none`` in the VCR/integration
    suite for the ``ArtifactType`` assertion this test previously carried."""
    record = ArtifactRecord("mind-map-id", "Map", "mind_map", "completed")
    backend = RecordingBackend()
    backend.set_result(ARTIFACT_GET_DEF, ArtifactGetResult(record))
    catalog = StudioCatalog(backend)

    artifact = await catalog.get_record("notebook-id", "mind-map-id")

    assert artifact is not None
    assert artifact.id == "mind-map-id"
    assert artifact.family == "mind_map"
    assert len(backend.invocations) == 1
