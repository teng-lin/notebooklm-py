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
    # Shared view: access/permission are string labels; view_level is omitted on
    # the read path (the read RPC does not report it).
    assert body["access"] == "anyone_with_link"
    assert body["share_url"].endswith("/nb-1")
    assert body["shared_users"][0]["email"] == "reader@example.com"
    assert body["shared_users"][0]["permission"] == "viewer"
    assert "view_level" not in body


def test_set_public_toggles_link(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/share/public", json={"enable": True})

    assert resp.status_code == 200
    assert resp.json()["is_public"] is True
    # Shared view labels the access enum (not a bare int).
    assert resp.json()["access"] == "anyone_with_link"

    resp = authed_client.post("/v1/notebooks/nb-1/share/public", json={"enable": False})

    assert resp.status_code == 200
    assert resp.json()["is_public"] is False
    assert resp.json()["access"] == "restricted"
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


def test_add_user_notify_defaults_off(authed_client: TestClient, fake_client: FakeClient) -> None:
    """Omitting ``notify`` must NOT email the third party (default flipped to False)."""
    resp = authed_client.post(
        "/v1/notebooks/nb-1/share/users",
        json={"email": "reader@example.com", "permission": "viewer"},
    )
    assert resp.status_code == 201
    assert resp.json()["notify"] is False
    assert fake_client.last_share_notify is False


def test_add_user_notify_opt_in(authed_client: TestClient, fake_client: FakeClient) -> None:
    """An explicit ``notify=true`` still emails."""
    resp = authed_client.post(
        "/v1/notebooks/nb-1/share/users",
        json={"email": "reader@example.com", "permission": "viewer", "notify": True},
    )
    assert resp.status_code == 201
    assert resp.json()["notify"] is True
    assert fake_client.last_share_notify is True


def test_set_view_level(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/share/view-level", json={"level": "chat"})

    assert resp.status_code == 200
    # set_view_level's return is authoritative, so view_level is surfaced (labeled).
    assert resp.json()["view_level"] == "chat"


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
