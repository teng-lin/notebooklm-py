"""/v1/notebooks/{id}/notes list / get / create / update / delete."""

from __future__ import annotations

from fastapi.testclient import TestClient

from notebooklm._types.notes import Note

from .fakes import FakeClient


def _seed(fake_client: FakeClient, note: Note) -> None:
    fake_client.notes_store.setdefault(note.notebook_id, {})[note.id] = note


def test_list_returns_notes(authed_client: TestClient, fake_client: FakeClient) -> None:
    _seed(fake_client, Note(id="n-1", notebook_id="nb-1", title="First", content="hi"))
    resp = authed_client.get("/v1/notebooks/nb-1/notes")
    assert resp.status_code == 200
    body = resp.json()
    assert body["notebook_id"] == "nb-1"
    assert [n["title"] for n in body["notes"]] == ["First"]


def test_list_pagination_slices_and_adds_meta(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    for i in range(5):
        _seed(fake_client, Note(id=f"n-{i}", notebook_id="nb-1", title=f"N{i}", content="x"))
    default = authed_client.get("/v1/notebooks/nb-1/notes").json()
    assert len(default["notes"]) == 5
    assert "meta" not in default

    body = authed_client.get("/v1/notebooks/nb-1/notes", params={"limit": 2, "offset": 1}).json()
    assert body["notebook_id"] == "nb-1"
    assert len(body["notes"]) == 2
    assert body["meta"] == {"total": 5, "has_more": True, "limit": 2, "offset": 1}


def test_list_bad_bounds_is_422(authed_client: TestClient) -> None:
    assert authed_client.get("/v1/notebooks/nb-1/notes", params={"limit": 0}).status_code == 422


def test_create_returns_201_with_new_note(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/notes", json={"title": "T", "content": "C"})
    assert resp.status_code == 201
    assert resp.json()["title"] == "T"
    assert resp.json()["content"] == "C"


def test_create_defaults(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/notes", json={})
    assert resp.status_code == 201
    assert resp.json()["title"] == "New Note"


def test_get_existing_note(authed_client: TestClient, fake_client: FakeClient) -> None:
    _seed(fake_client, Note(id="n-9", notebook_id="nb-1", title="Nine", content="x"))
    resp = authed_client.get("/v1/notebooks/nb-1/notes/n-9")
    assert resp.status_code == 200
    assert resp.json()["id"] == "n-9"


def test_get_missing_note_is_404(authed_client: TestClient) -> None:
    resp = authed_client.get("/v1/notebooks/nb-1/notes/nope")
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "not_found"


def test_update_replaces_and_returns_note(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    _seed(fake_client, Note(id="n-2", notebook_id="nb-1", title="Old", content="old"))
    resp = authed_client.put(
        "/v1/notebooks/nb-1/notes/n-2", json={"title": "New", "content": "new"}
    )
    assert resp.status_code == 200
    assert resp.json()["title"] == "New"
    assert resp.json()["content"] == "new"
    # The id is carried only by the re-fetched Note (the request body has none),
    # so asserting it makes the handler's post-update re-fetch load-bearing.
    assert resp.json()["id"] == "n-2"


def test_update_missing_note_is_404(authed_client: TestClient) -> None:
    resp = authed_client.put("/v1/notebooks/nb-1/notes/nope", json={"title": "x", "content": "y"})
    assert resp.status_code == 404


def test_update_requires_both_fields(authed_client: TestClient, fake_client: FakeClient) -> None:
    # PUT is a full replacement: NoteUpdate makes both fields required (no
    # defaults, unlike NoteCreate), so a partial body is a 422.
    _seed(fake_client, Note(id="n-4", notebook_id="nb-1", title="Old", content="old"))
    resp = authed_client.put("/v1/notebooks/nb-1/notes/n-4", json={"title": "x"})
    assert resp.status_code == 422


def test_delete_existing_is_204(authed_client: TestClient, fake_client: FakeClient) -> None:
    _seed(fake_client, Note(id="n-3", notebook_id="nb-1", title="Three", content="x"))
    resp = authed_client.delete("/v1/notebooks/nb-1/notes/n-3")
    assert resp.status_code == 204
    assert "n-3" not in fake_client.notes_store["nb-1"]


def test_delete_missing_is_idempotent_204(authed_client: TestClient) -> None:
    resp = authed_client.delete("/v1/notebooks/nb-1/notes/never-existed")
    assert resp.status_code == 204


def test_unauthorized_on_each_verb(raw_client: TestClient) -> None:
    h = {"Host": "127.0.0.1"}
    base = "/v1/notebooks/nb-1/notes"
    assert raw_client.get(base, headers=h).status_code == 401
    assert raw_client.get(f"{base}/n-1", headers=h).status_code == 401
    assert raw_client.post(base, json={"title": "x"}, headers=h).status_code == 401
    assert (
        raw_client.put(f"{base}/n-1", json={"title": "x", "content": "y"}, headers=h).status_code
        == 401
    )
    assert raw_client.delete(f"{base}/n-1", headers=h).status_code == 401
