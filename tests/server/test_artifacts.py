"""U7: /v1/notebooks/{id}/artifacts generate / poll / download / list."""

from __future__ import annotations

from fastapi.testclient import TestClient

from notebooklm._types.artifacts import GenerationState
from notebooklm.server.routes.artifacts import DOWNLOAD_SPECS, GENERATE_TYPES

from .fakes import FakeClient, make_artifact


def _generate_audio(authed_client: TestClient) -> str:
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts", json={"type": "audio"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["status"] == "pending"
    return body["task_id"]


def test_generate_audio_returns_202_and_task_id(authed_client: TestClient) -> None:
    task_id = _generate_audio(authed_client)
    assert task_id


def test_poll_known_task_not_found_is_200_pending(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    task_id = _generate_audio(authed_client)
    # Simulate the post-generate lag: poller returns NOT_FOUND for a known task.
    fake_client.poll_states[("nb-1", task_id)] = GenerationState.NOT_FOUND
    resp = authed_client.get(f"/v1/notebooks/nb-1/artifacts/{task_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


def test_poll_transitions_to_completed(authed_client: TestClient, fake_client: FakeClient) -> None:
    task_id = _generate_audio(authed_client)
    fake_client.poll_states[("nb-1", task_id)] = GenerationState.IN_PROGRESS
    assert (
        authed_client.get(f"/v1/notebooks/nb-1/artifacts/{task_id}").json()["status"]
        == "in_progress"
    )
    fake_client.poll_states[("nb-1", task_id)] = GenerationState.COMPLETED
    done = authed_client.get(f"/v1/notebooks/nb-1/artifacts/{task_id}")
    assert done.status_code == 200
    assert done.json()["status"] == "completed"


def test_poll_removed_is_410(authed_client: TestClient, fake_client: FakeClient) -> None:
    task_id = _generate_audio(authed_client)
    fake_client.poll_states[("nb-1", task_id)] = GenerationState.REMOVED
    resp = authed_client.get(f"/v1/notebooks/nb-1/artifacts/{task_id}")
    assert resp.status_code == 410


def test_poll_failed_is_409(authed_client: TestClient, fake_client: FakeClient) -> None:
    task_id = _generate_audio(authed_client)
    fake_client.poll_states[("nb-1", task_id)] = GenerationState.FAILED
    resp = authed_client.get(f"/v1/notebooks/nb-1/artifacts/{task_id}")
    assert resp.status_code == 409


def test_poll_unknown_task_is_404(authed_client: TestClient) -> None:
    resp = authed_client.get("/v1/notebooks/nb-1/artifacts/never-generated")
    assert resp.status_code == 404


def test_download_completed_artifact_streams_bytes(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio")}
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/download", json={"type": "audio"})
    assert resp.status_code == 200
    assert resp.content == fake_client.download_bytes


def test_download_not_ready_is_409(authed_client: TestClient) -> None:
    # No artifacts exist → NO_ARTIFACTS → 409, not 500.
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/download", json={"type": "audio"})
    assert resp.status_code == 409


def test_download_caller_path_field_is_ignored(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio")}
    # An attacker-supplied path-like field is not in the schema and is ignored.
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts/download",
        json={"type": "audio", "output_path": "/etc/passwd"},
    )
    assert resp.status_code == 200


def test_list_artifacts(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio", title="Pod")}
    resp = authed_client.get("/v1/notebooks/nb-1/artifacts")
    assert resp.status_code == 200
    assert resp.json()["artifacts"][0]["title"] == "Pod"


def test_download_spec_exhaustiveness() -> None:
    """Every studio download kind the client supports has a server spec.

    The generate types that produce a downloadable artifact must each have a
    matching ``DownloadTypeSpec`` (cinematic-video downloads as video; mind-map
    has both generate + download).
    """
    downloadable_generate = set(GENERATE_TYPES) - {"cinematic-video"}
    assert downloadable_generate <= set(DOWNLOAD_SPECS)
    # Every download spec is also a real ArtifactType-backed row.
    for name, spec in DOWNLOAD_SPECS.items():
        assert spec.name == name
        assert spec.download_attr.startswith("download_")
