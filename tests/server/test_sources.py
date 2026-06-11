"""U5: /v1/notebooks/{id}/sources add (url·text·file) / list / get / delete."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from notebooklm._types.sources import Source
from notebooklm.rpc.types import SourceStatus

from .fakes import FakeClient


def test_add_url_returns_non_ready_source(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/sources/url", json={"url": "https://example.com"})
    assert resp.status_code == 201
    body = resp.json()
    # The serialized status is the SourceStatus int (PROCESSING == 1, not READY).
    assert body["status"] == int(SourceStatus.PROCESSING)
    assert body["status"] != int(SourceStatus.READY)


def test_add_text_returns_source(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/text", json={"text": "hello", "title": "Note"}
    )
    assert resp.status_code == 201
    assert resp.json()["title"] == "Note"


def test_add_private_url_is_4xx_not_500(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/url", json={"url": "http://127.0.0.1:9/secret"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "validation"


def test_add_file_spools_and_cleans_up(authed_client: TestClient, fake_client: FakeClient) -> None:
    files = {"file": ("doc.txt", io.BytesIO(b"file-bytes"), "text/plain")}
    resp = authed_client.post("/v1/notebooks/nb-1/sources/file", files=files)
    assert resp.status_code == 201
    # add_file received a server-generated temp path that no longer exists.
    assert len(fake_client.uploaded_paths) == 1
    import os

    assert not os.path.exists(fake_client.uploaded_paths[0])


def test_upload_over_limit_is_413(authed_client: TestClient, monkeypatch: object) -> None:
    import pytest

    from notebooklm.server.routes import sources as sources_route

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    monkeypatch.setattr(sources_route, "MAX_UPLOAD_BYTES", 4)
    files = {"file": ("big.bin", io.BytesIO(b"way too many bytes"), "application/octet-stream")}
    resp = authed_client.post("/v1/notebooks/nb-1/sources/file", files=files)
    assert resp.status_code == 413


def test_poll_known_source_returns_200_pending_then_ready(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # Create via add_url so the registry knows the id; then hide it (not-yet-listable).
    created = authed_client.post(
        "/v1/notebooks/nb-1/sources/url", json={"url": "https://example.com"}
    ).json()
    source_id = created["id"]
    # Drop it from the listable store to simulate the lag window.
    fake_client.sources_store["nb-1"].pop(source_id)

    pending = authed_client.get(f"/v1/notebooks/nb-1/sources/{source_id}")
    assert pending.status_code == 200
    assert pending.json()["status"] == "pending"

    # Now it becomes listable and READY.
    fake_client.sources_store.setdefault("nb-1", {})[source_id] = Source(
        id=source_id, title="x", status=SourceStatus.READY
    )
    ready = authed_client.get(f"/v1/notebooks/nb-1/sources/{source_id}")
    assert ready.status_code == 200
    assert ready.json()["id"] == source_id


def test_poll_unknown_source_is_404(authed_client: TestClient) -> None:
    resp = authed_client.get("/v1/notebooks/nb-1/sources/never-created")
    assert resp.status_code == 404


def test_list_and_delete(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.sources_store["nb-1"] = {
        "src-7": Source(id="src-7", title="S", status=SourceStatus.READY)
    }
    listed = authed_client.get("/v1/notebooks/nb-1/sources")
    assert listed.status_code == 200
    assert listed.json()["sources"][0]["id"] == "src-7"

    deleted = authed_client.delete("/v1/notebooks/nb-1/sources/src-7")
    assert deleted.status_code == 204
    # Idempotent re-delete.
    assert authed_client.delete("/v1/notebooks/nb-1/sources/src-7").status_code == 204
