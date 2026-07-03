"""U4: /v1/notebooks list / get / create / delete."""

from __future__ import annotations

from fastapi.testclient import TestClient

from notebooklm._types.notebooks import Notebook

from .fakes import FakeClient


def test_list_returns_notebooks(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.notebooks_store["nb-1"] = Notebook(id="nb-1", title="First")
    resp = authed_client.get("/v1/notebooks")
    assert resp.status_code == 200
    titles = [n["title"] for n in resp.json()["notebooks"]]
    assert titles == ["First"]


def test_list_default_is_unbounded_no_meta(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    """The default call (no ``limit``) returns the full list, shape unchanged."""
    for i in range(3):
        fake_client.notebooks_store[f"nb-{i}"] = Notebook(id=f"nb-{i}", title=f"N{i}")
    body = authed_client.get("/v1/notebooks").json()
    assert len(body["notebooks"]) == 3
    assert "meta" not in body


def test_list_pagination_slices_and_adds_meta(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    for i in range(5):
        fake_client.notebooks_store[f"nb-{i}"] = Notebook(id=f"nb-{i}", title=f"N{i}")
    body = authed_client.get("/v1/notebooks", params={"limit": 2, "offset": 1}).json()
    assert len(body["notebooks"]) == 2
    assert body["meta"] == {"total": 5, "has_more": True, "limit": 2, "offset": 1}


def test_list_bad_bounds_is_422(authed_client: TestClient) -> None:
    assert authed_client.get("/v1/notebooks", params={"limit": 0}).status_code == 422
    assert authed_client.get("/v1/notebooks", params={"offset": -1}).status_code == 422


def test_create_returns_201_with_new_notebook(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks", json={"title": "Fresh"})
    assert resp.status_code == 201
    assert resp.json()["title"] == "Fresh"


def test_get_existing_notebook(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.notebooks_store["nb-9"] = Notebook(id="nb-9", title="Nine")
    resp = authed_client.get("/v1/notebooks/nb-9")
    assert resp.status_code == 200
    assert resp.json()["id"] == "nb-9"


def test_get_missing_notebook_is_404(authed_client: TestClient) -> None:
    resp = authed_client.get("/v1/notebooks/does-not-exist")
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "not_found"


def test_delete_existing_is_204(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.notebooks_store["nb-3"] = Notebook(id="nb-3", title="Three")
    resp = authed_client.delete("/v1/notebooks/nb-3")
    assert resp.status_code == 204
    assert "nb-3" not in fake_client.notebooks_store


def test_delete_missing_is_idempotent_204(authed_client: TestClient) -> None:
    resp = authed_client.delete("/v1/notebooks/never-existed")
    assert resp.status_code == 204


def test_unauthorized_on_each_verb(raw_client: TestClient) -> None:
    h = {"Host": "127.0.0.1"}
    assert raw_client.get("/v1/notebooks", headers=h).status_code == 401
    assert raw_client.post("/v1/notebooks", json={"title": "x"}, headers=h).status_code == 401
    assert raw_client.delete("/v1/notebooks/nb-1", headers=h).status_code == 401
