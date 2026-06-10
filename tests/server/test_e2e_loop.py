"""U8: full create→add-source→poll→chat→generate→poll→download loop.

Drives the whole REST surface through the FastAPI TestClient against an injected
fake client, with auth on every hop.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from notebooklm._types.artifacts import GenerationState
from notebooklm._types.sources import Source
from notebooklm.rpc.types import SourceStatus

from .fakes import FakeClient, make_artifact


def test_full_loop(authed_client: TestClient, fake_client: FakeClient) -> None:
    # 1. Create a notebook.
    nb = authed_client.post("/v1/notebooks", json={"title": "Loop"}).json()
    nb_id = nb["id"]

    # 2. Add a source (non-ready), recorded in the pending registry.
    src = authed_client.post(
        f"/v1/notebooks/{nb_id}/sources/url", json={"url": "https://example.com/doc"}
    ).json()
    src_id = src["id"]
    assert src["status"] == int(SourceStatus.PROCESSING)

    # 3. Poll the source to READY.
    fake_client.sources_store[nb_id][src_id] = Source(
        id=src_id, title="doc", status=SourceStatus.READY
    )
    ready = authed_client.get(f"/v1/notebooks/{nb_id}/sources/{src_id}")
    assert ready.status_code == 200
    assert ready.json()["id"] == src_id

    # 4. Blocking chat question.
    chat = authed_client.post(f"/v1/notebooks/{nb_id}/chat", json={"question": "Summarize the doc"})
    assert chat.status_code == 200
    assert chat.json()["answer"].startswith("answer to:")

    # 5. Generate an artifact (non-blocking → task_id).
    gen = authed_client.post(f"/v1/notebooks/{nb_id}/artifacts", json={"type": "audio"})
    assert gen.status_code == 202
    task_id = gen.json()["task_id"]

    # 6. Poll the artifact to COMPLETED.
    fake_client.poll_states[(nb_id, task_id)] = GenerationState.IN_PROGRESS
    assert (
        authed_client.get(f"/v1/notebooks/{nb_id}/artifacts/{task_id}").json()["status"]
        == "in_progress"
    )
    fake_client.poll_states[(nb_id, task_id)] = GenerationState.COMPLETED
    done = authed_client.get(f"/v1/notebooks/{nb_id}/artifacts/{task_id}")
    assert done.json()["status"] == "completed"

    # 7. Download the completed artifact.
    fake_client.artifacts_store[nb_id] = {task_id: make_artifact(task_id, "audio")}
    dl = authed_client.post(f"/v1/notebooks/{nb_id}/artifacts/download", json={"type": "audio"})
    assert dl.status_code == 200
    assert dl.content == fake_client.download_bytes


def test_full_loop_requires_auth_on_every_hop(raw_client: TestClient) -> None:
    h = {"Host": "127.0.0.1"}
    assert raw_client.post("/v1/notebooks", json={"title": "x"}, headers=h).status_code == 401
    assert (
        raw_client.post(
            "/v1/notebooks/nb-1/sources/url", json={"url": "https://x.com"}, headers=h
        ).status_code
        == 401
    )
    assert (
        raw_client.post("/v1/notebooks/nb-1/chat", json={"question": "q"}, headers=h).status_code
        == 401
    )
    assert (
        raw_client.post(
            "/v1/notebooks/nb-1/artifacts", json={"type": "audio"}, headers=h
        ).status_code
        == 401
    )
