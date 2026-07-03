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


def _seed_ready_source(fake_client: FakeClient, *, content: str) -> str:
    src_id = "src-ready"
    fake_client.sources_store["nb-1"] = {
        src_id: Source(id=src_id, title="Doc", status=SourceStatus.READY)
    }
    fake_client.fulltext_store[("nb-1", src_id)] = content
    return src_id


def test_content_ready_source_returns_body(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    src_id = _seed_ready_source(fake_client, content="hello world")
    resp = authed_client.get(f"/v1/notebooks/nb-1/sources/{src_id}/content")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] == "hello world"
    assert body["char_count"] == 11
    assert body["truncated"] is False


def test_content_windowing_max_chars_offset_truncated(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    src_id = _seed_ready_source(fake_client, content="abcdefghij")
    resp = authed_client.get(
        f"/v1/notebooks/nb-1/sources/{src_id}/content", params={"offset": 2, "max_chars": 3}
    )
    body = resp.json()
    assert body["content"] == "cde"
    assert body["char_count"] == 10  # full length, not the window
    assert body["truncated"] is True
    # A window covering the remainder is not truncated.
    resp2 = authed_client.get(
        f"/v1/notebooks/nb-1/sources/{src_id}/content", params={"offset": 7, "max_chars": 100}
    )
    assert resp2.json()["content"] == "hij"
    assert resp2.json()["truncated"] is False


def test_content_negative_max_chars_is_422(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    src_id = _seed_ready_source(fake_client, content="abc")
    resp = authed_client.get(
        f"/v1/notebooks/nb-1/sources/{src_id}/content", params={"max_chars": -1}
    )
    assert resp.status_code == 422


def test_content_ready_but_empty_fulltext_is_null(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # A READY source whose extracted text is empty → content None (not ""), char 0.
    src_id = _seed_ready_source(fake_client, content="")
    resp = authed_client.get(f"/v1/notebooks/nb-1/sources/{src_id}/content")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] is None
    assert body["char_count"] == 0
    assert body["truncated"] is False


def test_content_offset_past_end_is_null_not_truncated(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # An offset beyond the body yields an empty slice → normalized to None, and
    # nothing was omitted past the window, so truncated is False.
    src_id = _seed_ready_source(fake_client, content="abc")
    resp = authed_client.get(f"/v1/notebooks/nb-1/sources/{src_id}/content", params={"offset": 10})
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] is None
    assert body["char_count"] == 3  # full length preserved
    assert body["truncated"] is False


def test_content_output_format_markdown(authed_client: TestClient, fake_client: FakeClient) -> None:
    # output_format is propagated to the shared read core and echoed in the body
    # (parity with the MCP source_read tool).
    src_id = _seed_ready_source(fake_client, content="# Heading")
    resp = authed_client.get(
        f"/v1/notebooks/nb-1/sources/{src_id}/content", params={"output_format": "markdown"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["output_format"] == "markdown"
    assert body["content"] == "# Heading"
    # The default is text.
    default = authed_client.get(f"/v1/notebooks/nb-1/sources/{src_id}/content").json()
    assert default["output_format"] == "text"


def test_content_bad_output_format_is_422(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    src_id = _seed_ready_source(fake_client, content="abc")
    resp = authed_client.get(
        f"/v1/notebooks/nb-1/sources/{src_id}/content", params={"output_format": "html"}
    )
    assert resp.status_code == 422


def test_content_known_pending_source_is_404(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # The content route requires a LISTABLE source: a known-but-not-yet-listable
    # (pending) id — unlike the status-poll GET /{source_id} route — is a 404 here.
    created = authed_client.post(
        "/v1/notebooks/nb-1/sources/url", json={"url": "https://example.com"}
    ).json()
    source_id = created["id"]
    fake_client.sources_store["nb-1"].pop(source_id)
    resp = authed_client.get(f"/v1/notebooks/nb-1/sources/{source_id}/content")
    assert resp.status_code == 404


def test_content_not_ready_source_content_is_null(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.sources_store["nb-1"] = {
        "src-p": Source(id="src-p", title="Doc", status=SourceStatus.PROCESSING)
    }
    resp = authed_client.get("/v1/notebooks/nb-1/sources/src-p/content")
    assert resp.status_code == 200
    body = resp.json()
    assert body["content"] is None
    assert body["char_count"] == 0
    assert body["truncated"] is False


def test_content_missing_source_is_404(authed_client: TestClient) -> None:
    resp = authed_client.get("/v1/notebooks/nb-1/sources/nope/content")
    assert resp.status_code == 404


def test_content_summary_variant(authed_client: TestClient, fake_client: FakeClient) -> None:
    from notebooklm._types.research import SourceGuide

    src_id = _seed_ready_source(fake_client, content="body")
    fake_client.guide_store[("nb-1", src_id)] = SourceGuide(
        summary="A digest", keywords=("k1", "k2")
    )
    resp = authed_client.get(
        f"/v1/notebooks/nb-1/sources/{src_id}/content", params={"detail": "summary"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"] == "A digest"
    assert body["keywords"] == ["k1", "k2"]


def test_content_summary_missing_source_is_404(authed_client: TestClient) -> None:
    resp = authed_client.get(
        "/v1/notebooks/nb-1/sources/nope/content", params={"detail": "summary"}
    )
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
