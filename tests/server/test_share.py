"""Sharing routes under /v1/notebooks/{id}/share."""

from __future__ import annotations

from fastapi.testclient import TestClient

from notebooklm._types.sharing import SharedUser
from notebooklm.rpc.types import SharePermission

from .fakes import FakeClient


def test_share_status_returns_current_state(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.public_shares["nb-1"] = True
    fake_client.shared_users["nb-1"] = {
        "reader@example.com": SharedUser(
            email="reader@example.com",
            permission=SharePermission.VIEWER,
        )
    }

    resp = authed_client.get("/v1/notebooks/nb-1/share")

    assert resp.status_code == 200
    body = resp.json()
    assert body["notebook_id"] == "nb-1"
    assert body["is_public"] is True
    assert body["share_url"].endswith("/nb-1")
    assert body["shared_users"][0]["email"] == "reader@example.com"


def test_set_public_toggles_link(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/share/public", json={"enable": True})

    assert resp.status_code == 200
    assert resp.json()["is_public"] is True
    assert resp.json()["access"] == 1

    resp = authed_client.post("/v1/notebooks/nb-1/share/public", json={"enable": False})

    assert resp.status_code == 200
    assert resp.json()["is_public"] is False
    assert resp.json()["share_url"] is None


def test_add_update_and_remove_user(authed_client: TestClient, fake_client: FakeClient) -> None:
    add = authed_client.post(
        "/v1/notebooks/nb-1/share/users",
        json={"email": "reader@example.com", "permission": "viewer", "notify": False},
    )
    assert add.status_code == 201
    assert add.json() == {
        "notebook_id": "nb-1",
        "email": "reader@example.com",
        "permission": "viewer",
        "notify": False,
    }
    assert fake_client.shared_users["nb-1"]["reader@example.com"].permission == (
        SharePermission.VIEWER
    )
    assert fake_client.last_share_notify is False

    fake_client.last_share_notify = True
    update = authed_client.patch(
        "/v1/notebooks/nb-1/share/users/reader@example.com",
        json={"permission": "editor"},
    )
    assert update.status_code == 200
    assert update.json()["permission"] == "editor"
    assert fake_client.shared_users["nb-1"]["reader@example.com"].permission == (
        SharePermission.EDITOR
    )
    assert fake_client.last_share_notify is False

    remove = authed_client.delete("/v1/notebooks/nb-1/share/users/reader@example.com")
    assert remove.status_code == 204
    assert "reader@example.com" not in fake_client.shared_users["nb-1"]


def test_set_view_level(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/share/view-level", json={"level": "chat"})

    assert resp.status_code == 200
    assert resp.json()["view_level"] == 1


def test_share_rejects_bad_permission(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/share/users",
        json={"email": "reader@example.com", "permission": "owner"},
    )

    assert resp.status_code == 422


def test_share_routes_require_auth(raw_client: TestClient) -> None:
    h = {"Host": "127.0.0.1"}
    assert raw_client.get("/v1/notebooks/nb-1/share", headers=h).status_code == 401
    assert (
        raw_client.post(
            "/v1/notebooks/nb-1/share/public", json={"enable": True}, headers=h
        ).status_code
        == 401
    )
