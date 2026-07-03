"""U7: /v1/notebooks/{id}/artifacts generate / poll / download / list."""

from __future__ import annotations

import os
import tempfile

import pytest
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
    # Default (no limit) stays unbounded, no meta block.
    assert "meta" not in resp.json()


def test_list_artifacts_pagination_slices_and_adds_meta(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.artifacts_store["nb-1"] = {
        f"a{i}": make_artifact(f"a{i}", "audio", title=f"Pod{i}") for i in range(5)
    }
    body = authed_client.get(
        "/v1/notebooks/nb-1/artifacts", params={"limit": 2, "offset": 1}
    ).json()
    assert body["notebook_id"] == "nb-1"
    assert len(body["artifacts"]) == 2
    assert body["meta"] == {"total": 5, "has_more": True, "limit": 2, "offset": 1}


def test_list_artifacts_bad_limit_is_422(authed_client: TestClient) -> None:
    assert authed_client.get("/v1/notebooks/nb-1/artifacts", params={"limit": 0}).status_code == 422


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


def test_download_temp_dir_cleaned_on_normal_completion(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: object
) -> None:
    import pytest

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    made: list[str] = []
    real_mkdtemp = tempfile.mkdtemp

    def _tracking_mkdtemp(*args: object, **kwargs: object) -> str:
        d = real_mkdtemp(*args, **kwargs)
        made.append(d)
        return d

    monkeypatch.setattr(artifacts_route.tempfile, "mkdtemp", _tracking_mkdtemp)
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio")}
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/download", json={"type": "audio"})
    assert resp.status_code == 200
    assert resp.content == fake_client.download_bytes
    # After the TestClient fully consumed the streamed body, the temp dir the
    # download spooled into is gone (the _CleanupFileResponse finally ran).
    assert made and not os.path.exists(made[0])


async def test_cleanup_file_response_cleans_on_disconnect(tmp_path: object) -> None:
    # Simulate a client disconnect mid-stream: super().__call__ raises, and the
    # subclass's finally must still remove the temp dir (a BackgroundTask would be
    # dropped on disconnect and leak it).
    temp_dir = tempfile.mkdtemp(prefix="nblm-disconnect-", dir=str(tmp_path))
    served = os.path.join(temp_dir, "artifact.mp3")
    with open(served, "wb") as fh:
        fh.write(b"bytes")

    resp = artifacts_route._CleanupFileResponse(served, temp_dir=temp_dir)

    class _Boom(Exception):
        pass

    async def _receive() -> dict[str, object]:
        return {"type": "http.disconnect"}

    async def _send(message: dict[str, object]) -> None:
        raise _Boom  # abort mid-stream, as a disconnect would

    scope = {"type": "http", "method": "GET", "headers": []}
    # Catch the SPECIFIC simulated-abort exception so an unrelated failure in the
    # cleanup path is not silently swallowed — the test only passes for the abort
    # it staged.
    with pytest.raises(_Boom):
        await resp(scope, _receive, _send)
    assert not os.path.exists(temp_dir)


# --- Phase 4: studio lifecycle (delete / retry) ------------------------------


def test_delete_artifact_is_204(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio")}
    resp = authed_client.delete("/v1/notebooks/nb-1/artifacts/a1")
    assert resp.status_code == 204
    assert ("nb-1", "a1") in fake_client.deleted_artifacts
    assert "a1" not in fake_client.artifacts_store["nb-1"]


def test_delete_absent_artifact_is_204_idempotent(authed_client: TestClient) -> None:
    # Idempotent-on-missing, like the notebook/source/note DELETE routes.
    assert authed_client.delete("/v1/notebooks/nb-1/artifacts/ghost").status_code == 204


def test_retry_artifact_returns_task_id_and_status(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio")}
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/a1/retry")
    assert resp.status_code == 200
    body = resp.json()
    # Retry kicks off a task whose id equals the artifact id (mirrors MCP).
    assert body["artifact_id"] == "a1"
    assert body["task_id"] == "a1"
    assert body["status"] == "pending"
    # The kicked-off task is now pollable.
    assert authed_client.get("/v1/notebooks/nb-1/artifacts/a1").json()["status"] == "pending"


def test_retry_records_task_in_pending_registry(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # retry records its task_id (like generate), so a poll that briefly races ahead
    # of the artifact listing resolves to 200 pending instead of a spurious 404.
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/ghost/retry")
    assert resp.status_code == 200
    # Transient window: the retried task polls NOT_FOUND before it appears in listings.
    fake_client.poll_states[("nb-1", "ghost")] = GenerationState.NOT_FOUND
    poll = authed_client.get("/v1/notebooks/nb-1/artifacts/ghost")
    assert poll.status_code == 200  # recorded on retry → 200 (would be 404 unrecorded)
    assert poll.json()["status"] == "not_found"


def test_retry_refusal_propagates_as_error(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    from notebooklm.exceptions import RateLimitError

    fake_client.retry_error = RateLimitError("slow down")
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/a1/retry")
    assert resp.status_code == 429
    assert resp.json()["error"]["retriable"] is True


def test_retry_plain_str_status_does_not_crash(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # GenerationStatus.status is raw-string-permissive: a plain-str status must
    # NOT crash the route (``.value`` would raise AttributeError). The route
    # projects it enum-or-str-safely.
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio")}
    fake_client.retry_status = "pending"  # a plain str, not a GenerationState enum
    resp = authed_client.post("/v1/notebooks/nb-1/artifacts/a1/retry")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"


# --- Phase 4: artifact rename (PATCH) ----------------------------------------


def test_rename_artifact(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "audio", title="Old")}
    resp = authed_client.patch("/v1/notebooks/nb-1/artifacts/a1", json={"title": "New"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "renamed"
    assert body["new_title"] == "New"
    assert body["is_mind_map"] is False
    assert fake_client.artifacts_store["nb-1"]["a1"].title == "New"


def test_rename_artifact_missing_title_is_422(authed_client: TestClient) -> None:
    assert authed_client.patch("/v1/notebooks/nb-1/artifacts/a1", json={}).status_code == 422


# --- Phase 4: kind-aware mind-map rename/delete + full-UUID case ---------------

# A canonical (lowercase) full UUID and its uppercase spelling. The backend id is
# canonically lowercase; a caller may send the uppercase form.
_MM_ID = "abcd1234-5678-4abc-def0-1234567890ab"
_MM_ID_UPPER = _MM_ID.upper()


def _seed_note_backed_mind_map(fake_client: FakeClient, mm_id: str) -> None:
    """Seed a note-backed mind map (the ``delete_artifact`` probe list) plus a
    backing note with the same id, so a note-path delete is observable."""
    from types import SimpleNamespace

    from notebooklm._types.notes import Note

    fake_client.note_backed_mind_maps["nb-1"] = [SimpleNamespace(id=mm_id)]
    fake_client.notes_store["nb-1"] = {
        mm_id: Note(id=mm_id, notebook_id="nb-1", title="Map", content="")
    }


def _seed_interactive_mind_map(fake_client: FakeClient, mm_id: str) -> None:
    """Seed an interactive mind map into the ``rename_artifact`` probe list."""
    from notebooklm._types.mind_maps import MindMap, MindMapKind

    fake_client.mind_maps_store["nb-1"] = [
        MindMap(id=mm_id, notebook_id="nb-1", title="Map", kind=MindMapKind.INTERACTIVE)
    ]


def test_rename_mind_map_is_mind_map_branch(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # A resolved mind map routes through the kind-aware mind_maps.rename, not the
    # plain artifacts.rename, and is flagged is_mind_map=True.
    from notebooklm._types.mind_maps import MindMapKind

    _seed_interactive_mind_map(fake_client, "mm-1")
    resp = authed_client.patch("/v1/notebooks/nb-1/artifacts/mm-1", json={"title": "New"})
    assert resp.status_code == 200
    assert resp.json()["is_mind_map"] is True
    nb, mm_id, title, kind = fake_client.renamed_mind_maps[0]
    assert (nb, mm_id, title) == ("nb-1", "mm-1", "New")
    assert kind == MindMapKind.INTERACTIVE
    # The plain artifact rename path was NOT taken.
    assert fake_client.renamed_artifacts == []


def test_rename_uppercase_full_uuid_mind_map_routes_mind_map_path(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # An UPPERCASE full UUID for a note-backed/interactive mind map must be
    # normalized to canonical lowercase before the case-sensitive core probe, so
    # it still routes to the mind-map path (not a false plain-artifact success).
    _seed_interactive_mind_map(fake_client, _MM_ID)
    # URL built in a local, then passed to the HTTP PATCH helper — keeping an
    # f-string out of the client call so the ADR-0007 monkeypatch guardrail does
    # not false-positive an ``authed_client`` HTTP PATCH as a computed mock target.
    url = f"/v1/notebooks/nb-1/artifacts/{_MM_ID_UPPER}"
    resp = authed_client.patch(url, json={"title": "New"})
    assert resp.status_code == 200
    assert resp.json()["is_mind_map"] is True
    # Renamed via the mind-map path with the canonical lowercase id.
    assert fake_client.renamed_mind_maps
    assert fake_client.renamed_mind_maps[0][1] == _MM_ID
    assert fake_client.renamed_artifacts == []


def test_delete_note_backed_mind_map_routes_notes_delete(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # A note-backed mind map is CLEARED via notes.delete, never artifacts.delete.
    _seed_note_backed_mind_map(fake_client, "mm-note")
    resp = authed_client.delete("/v1/notebooks/nb-1/artifacts/mm-note")
    assert resp.status_code == 204
    # notes.delete removed the backing note; the artifact delete path was skipped.
    assert "mm-note" not in fake_client.notes_store["nb-1"]
    assert fake_client.deleted_artifacts == []


def test_delete_uppercase_full_uuid_note_backed_mind_map_routes_notes_delete(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # The full-UUID case-normalization fix: an uppercase UUID for a note-backed
    # mind map still routes to the note path instead of a false artifact-delete
    # "success" that leaves the map intact.
    _seed_note_backed_mind_map(fake_client, _MM_ID)
    resp = authed_client.delete(f"/v1/notebooks/nb-1/artifacts/{_MM_ID_UPPER}")
    assert resp.status_code == 204
    assert _MM_ID not in fake_client.notes_store["nb-1"]
    assert fake_client.deleted_artifacts == []


# --- Phase 4: artifact prompt read -------------------------------------------


def test_get_prompt_returns_stored_prompt(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "report")}
    fake_client.prompts_store[("nb-1", "a1")] = "Summarize the sources"
    resp = authed_client.get("/v1/notebooks/nb-1/artifacts/a1/prompt")
    assert resp.status_code == 200
    assert resp.json() == {
        "notebook_id": "nb-1",
        "artifact_id": "a1",
        "prompt": "Summarize the sources",
    }


def test_get_prompt_null_is_200_not_404(authed_client: TestClient, fake_client: FakeClient) -> None:
    # A known artifact that records no prompt → prompt=null at 200, not a 404.
    fake_client.artifacts_store["nb-1"] = {"a1": make_artifact("a1", "mind-map")}
    resp = authed_client.get("/v1/notebooks/nb-1/artifacts/a1/prompt")
    assert resp.status_code == 200
    assert resp.json()["prompt"] is None


def test_get_prompt_unknown_artifact_is_404(authed_client: TestClient) -> None:
    assert authed_client.get("/v1/notebooks/nb-1/artifacts/ghost/prompt").status_code == 404


# --- Phase 4: generate option hygiene ----------------------------------------


def test_generate_wrong_kind_option_is_400(authed_client: TestClient) -> None:
    # ``orientation`` belongs to infographic, not audio → rejected (not silent no-op).
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts", json={"type": "audio", "orientation": "landscape"}
    )
    assert resp.status_code == 400


def test_generate_optionless_kind_rejects_any_option(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts", json={"type": "data-table", "difficulty": "easy"}
    )
    assert resp.status_code == 400


def test_generate_bad_new_option_value_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts", json={"type": "video", "style": "not-a-style"}
    )
    assert resp.status_code == 400


def test_generate_forwards_instructions(authed_client: TestClient, fake_client: FakeClient) -> None:
    # Both ``description`` and ``instructions`` are set in raw_args; for audio the
    # generate core forwards the instruction text to the client generator.
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts",
        json={"type": "audio", "instructions": "focus on chapter 3"},
    )
    assert resp.status_code == 202
    assert fake_client.last_generate_kwargs is not None
    assert fake_client.last_generate_kwargs.get("instructions") == "focus on chapter 3"


def test_generate_blank_instructions_treated_as_absent(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # Whitespace-only instructions must not reach the client as a blank prompt slot
    # (keeps the default request shape byte-identical).
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts",
        json={"type": "audio", "instructions": "   "},
    )
    assert resp.status_code == 202
    assert fake_client.last_generate_kwargs is not None
    assert fake_client.last_generate_kwargs.get("instructions") is None


def test_generate_mind_map_forwards_instructions(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # mind-map reads raw_args["instructions"] (not ``description``): the default
    # interactive kind routes through client.mind_maps.generate, and the
    # instruction text must reach it.
    resp = authed_client.post(
        "/v1/notebooks/nb-1/artifacts",
        json={"type": "mind-map", "instructions": "group by theme"},
    )
    assert resp.status_code == 202
    assert fake_client.last_mind_map_generate is not None
    assert fake_client.last_mind_map_generate["instructions"] == "group by theme"


def test_kind_options_match_core_maps() -> None:
    """The REST per-kind option table is pinned to the neutral core's choice maps.

    Duplicated (the server layer must not import the core privates) but pinned so
    a core-map change surfaces here — the same guardrail the MCP ``_KIND_OPTIONS``
    table carries.
    """
    import notebooklm._app.generate_plans as gp
    from notebooklm.server.routes.artifacts import _KIND_OPTIONS

    assert _KIND_OPTIONS["audio"]["audio_format"] == tuple(gp._AUDIO_FORMAT_MAP)
    assert _KIND_OPTIONS["audio"]["audio_length"] == tuple(gp._AUDIO_LENGTH_MAP)
    assert _KIND_OPTIONS["video"]["video_format"] == tuple(gp._VIDEO_FORMAT_MAP)
    assert _KIND_OPTIONS["video"]["style"] == tuple(gp._VIDEO_STYLE_MAP)
    assert _KIND_OPTIONS["slide-deck"]["deck_format"] == tuple(gp._SLIDE_FORMAT_MAP)
    assert _KIND_OPTIONS["slide-deck"]["deck_length"] == tuple(gp._SLIDE_LENGTH_MAP)
    assert _KIND_OPTIONS["quiz"]["quantity"] == tuple(gp._QUIZ_QUANTITY_MAP)
    assert _KIND_OPTIONS["quiz"]["difficulty"] == tuple(gp._QUIZ_DIFFICULTY_MAP)
    assert _KIND_OPTIONS["flashcards"]["quantity"] == tuple(gp._QUIZ_QUANTITY_MAP)
    assert _KIND_OPTIONS["flashcards"]["difficulty"] == tuple(gp._QUIZ_DIFFICULTY_MAP)
    assert _KIND_OPTIONS["infographic"]["orientation"] == tuple(gp._INFOGRAPHIC_ORIENTATION_MAP)
    assert _KIND_OPTIONS["infographic"]["detail"] == tuple(gp._INFOGRAPHIC_DETAIL_MAP)
    assert _KIND_OPTIONS["infographic"]["style"] == tuple(gp._INFOGRAPHIC_STYLE_MAP)
    assert _KIND_OPTIONS["report"]["report_format"] == tuple(gp._REPORT_FORMAT_MAP)


def test_kind_options_exact_mcp_parity() -> None:
    """The REST and MCP ``_KIND_OPTIONS`` tables must be byte-for-byte identical.

    Both are duplicated from the neutral core (the CLI/MCP/server boundary forbids
    importing the core privates); pinning them EQUAL to each other guarantees the
    two agent surfaces validate the SAME per-kind options with the SAME choices.
    """
    import pytest

    pytest.importorskip("fastmcp")  # MCP tools need the ``mcp`` extra
    from notebooklm.mcp.tools.studio import _KIND_OPTIONS as mcp_options
    from notebooklm.server.routes.artifacts import _KIND_OPTIONS as rest_options

    assert rest_options == mcp_options


def test_kind_options_cover_every_generate_type() -> None:
    """Every ``GENERATE_TYPES`` kind has a ``_KIND_OPTIONS`` entry, and the
    per-kind-only keys (``map_kind``, ``style_prompt``) + optionless kinds are
    exactly as expected — so a new generate kind can't silently ship without its
    option contract."""
    from notebooklm.server.routes.artifacts import _KIND_OPTIONS

    # Exhaustive over the generate surface (no missing / extra kinds).
    assert set(_KIND_OPTIONS) == set(GENERATE_TYPES)
    # ``map_kind`` is validated at the boundary ONLY (no core map) — it must exist
    # on mind-map and nowhere else.
    assert "map_kind" in _KIND_OPTIONS["mind-map"]
    assert all("map_kind" not in opts for k, opts in _KIND_OPTIONS.items() if k != "mind-map")
    # ``style_prompt`` (free text, choices None) belongs to video only.
    assert _KIND_OPTIONS["video"]["style_prompt"] is None
    assert all("style_prompt" not in opts for k, opts in _KIND_OPTIONS.items() if k != "video")
    # Optionless kinds carry an explicit empty map (so a stray option is rejected).
    assert _KIND_OPTIONS["cinematic-video"] == {}
    assert _KIND_OPTIONS["data-table"] == {}


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
