"""Live deferred audio creation with independent read-back and owned cleanup."""

import os

import pytest

from notebooklm import NotebookLMClient
from notebooklm.options import AndroidBackendConfig, ClientConfig, WebBackendConfig
from notebooklm.types import ArtifactStatus, AudioFormat, AudioLength, GenerationState

from .conftest import requires_auth

pytestmark = [
    requires_auth,
    pytest.mark.skipif(
        os.environ.get("NOTEBOOKLM_DEFERRED_AUDIO_CONFORMANCE") != "1",
        reason="manual deferred audio conformance requires NOTEBOOKLM_DEFERRED_AUDIO_CONFORMANCE=1",
    ),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("backend", ["web", "android"])
async def test_deferred_audio_is_scheduled_and_persisted(backend):
    """Exercise real CreateArtifact field 7 on both front doors (#2505)."""
    config = ClientConfig(
        backend=AndroidBackendConfig() if backend == "android" else WebBackendConfig()
    )
    async with NotebookLMClient.from_storage(config=config) as client:
        notebook = await client.notebooks.create("E2E #2505 disposable deferred audio")
        try:
            source = await client.sources.add_text(
                notebook.id,
                "Deferred generation test source",
                "NotebookLM creates Audio Overviews from notebook sources. "
                "Deferred generation queues an overview for a later time chosen by the server. "
                "This disposable notebook verifies that the queued artifact is persisted and "
                "can be read back before execution. The notebook is deleted after the check.",
                wait=True,
            )
            result = await client.artifacts.generate_audio(
                notebook.id,
                source_ids=[source.id],
                audio_format=AudioFormat.BRIEF,
                audio_length=AudioLength.SHORT,
                instructions="Explain how deferred generation queues an Audio Overview.",
                later=True,
            )
            assert result.task_id
            assert result.status is GenerationState.SCHEDULED
            assert result.is_scheduled and not result.is_terminal
            artifact = await client.artifacts.get(notebook.id, result.task_id)
            assert artifact.status == ArtifactStatus.SCHEDULED
            assert artifact.is_scheduled
            assert artifact.status_str == "scheduled"
            assert artifact.id == result.task_id
            polled = await client.artifacts.poll_status(notebook.id, result.task_id)
            assert polled.status is GenerationState.SCHEDULED
            await client.artifacts.delete(notebook.id, result.task_id)
            assert await client.artifacts.get_or_none(notebook.id, result.task_id) is None
        finally:
            await client.notebooks.delete(notebook.id)
