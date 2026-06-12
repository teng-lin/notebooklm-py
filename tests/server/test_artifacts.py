"""U7: /v1/notebooks/{id}/artifacts generate / poll / download / list."""

from __future__ import annotations

import os

from fastapi.testclient import TestClient

from notebooklm._app.generate import GenerationExecutionResult
from notebooklm._app.generate_retry import GenerationOutcome
from notebooklm._types.artifacts import GenerationState
from notebooklm.server._pending import PendingRegistry
from notebooklm.server.routes import artifacts as artifacts_route
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


# --- generate: input validation (400s) --------------------------------------


def test_generate_unknown_type_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts", json={"type": "bogus"})
    assert resp.status_code == 400


def test_generate_unsupported_language_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts", json={"type": "audio", "language": "zz-bogus"}
    )
    assert resp.status_code == 400


def test_generate_invalid_option_choice_is_400(authed_client: TestClient) -> None:
    # A provided per-kind option is validated up front (clean 400, not a raw
    # KeyError deeper in generate-core).
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts", json={"type": "audio", "audio_format": "bogus"}
    )
    assert resp.status_code == 400


def test_generate_explicit_valid_option_is_202(authed_client: TestClient) -> None:
    # An explicit valid option flows through to the generation plan.
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts", json={"type": "audio", "audio_format": "brief"}
    )
    assert resp.status_code == 202


# --- download: input validation + format axis --------------------------------


def test_download_unknown_type_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/download", json={"type": "bogus"})
    assert resp.status_code == 400


def test_download_output_format_on_unsupported_type_is_400(authed_client: TestClient) -> None:
    # audio has no format axis, so an output_format is a clean 400.
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts/download",
        json={"type": "audio", "output_format": "mp3"},
    )
    assert resp.status_code == 400


def test_download_with_output_format_streams(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.artifacts_store["nb-1"] = {"d1": make_artifact("d1", "slide-deck")}
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts/download",
        json={"type": "slide-deck", "output_format": "pdf"},
    )
    assert resp.status_code == 200
    assert resp.content == fake_client.download_bytes


def test_download_unexpected_output_path_is_rejected(
    authed_client: TestClient, fake_client: FakeClient, tmp_path: object
) -> None:
    # If the core resolves a served path OUTSIDE the server's private temp dir,
    # the route refuses to stream it (path-traversal safety guard). tmp_path is a
    # distinct tree from the server's mkdtemp dir, so the guard fires.
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio")}
    fake_client.download_return_path = os.path.join(str(tmp_path), "nblm-outside-artifact.mp3")
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/download", json={"type": "audio"})
    assert resp.status_code == 400


# --- helpers: _cleanup + _generation_payload ---------------------------------


def test_cleanup_unlinks_a_file(tmp_path: object) -> None:
    target = os.path.join(str(tmp_path), "leftover.bin")
    with open(target, "wb") as fh:
        fh.write(b"x")
    artifacts_route._cleanup(target)
    assert not os.path.exists(target)


def test_cleanup_missing_path_is_noop(tmp_path: object) -> None:
    # Already-gone path must not raise. tmp_path is unique per test, so the path
    # is guaranteed absent (no risk of deleting unrelated state).
    artifacts_route._cleanup(os.path.join(str(tmp_path), "nblm-does-not-exist-xyz"))


def test_generation_payload_mind_map_returns_inline() -> None:
    # A mind-map renders synchronously: no task_id, the map is inlined.
    result = GenerationExecutionResult(
        kind="mind-map", display_name="Mind map", mind_map={"root": 1}
    )
    payload = artifacts_route._generation_payload("nb-1", result, PendingRegistry())
    assert payload["mind_map"] == {"root": 1}
    assert "task_id" not in payload


def test_generation_payload_without_outcome() -> None:
    # No generation outcome and no mind map → bare {notebook_id, kind}.
    result = GenerationExecutionResult(kind="audio", display_name="Audio", generation=None)
    payload = artifacts_route._generation_payload("nb-1", result, PendingRegistry())
    assert payload == {"notebook_id": "nb-1", "kind": "audio"}


def test_generation_payload_outcome_without_task_id_is_not_recorded() -> None:
    # A falsy task_id is projected but never recorded in the pending registry.
    pending = PendingRegistry()
    outcome = GenerationOutcome(status="ok", artifact_type="audio", task_id="")
    result = GenerationExecutionResult(kind="audio", display_name="Audio", generation=outcome)
    payload = artifacts_route._generation_payload("nb-1", result, pending)
    assert payload["task_id"] == ""
    assert not pending.knows("nb-1", "")


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
