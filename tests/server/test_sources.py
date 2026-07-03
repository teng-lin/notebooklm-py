"""U5: /v1/notebooks/{id}/sources add (url·text·file) / list / get / delete."""

from __future__ import annotations

import io

from fastapi.testclient import TestClient

from notebooklm._types.notebooks import Notebook
from notebooklm._types.sources import Source
from notebooklm.rpc.types import SourceStatus
from notebooklm.server._pagination import MAX_LIMIT

from .fakes import FakeClient


def _seed_notebook(fake_client: FakeClient, nid: str = "nb-1") -> None:
    """Seed the notebook so the batch route's shared-context preflight passes."""
    fake_client.notebooks_store[nid] = Notebook(id=nid, title="NB")


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
    row = listed.json()["sources"][0]
    assert row["id"] == "src-7"
    # Shared view: string kind + status_label alongside the raw status int.
    assert row["status_label"] == "ready"
    assert "kind" in row
    # Default (no limit) stays unbounded, no meta block.
    assert "meta" not in listed.json()

    deleted = authed_client.delete("/v1/notebooks/nb-1/sources/src-7")
    assert deleted.status_code == 204
    # Idempotent re-delete.
    assert authed_client.delete("/v1/notebooks/nb-1/sources/src-7").status_code == 204


def test_get_source_carries_kind_and_status_label(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.sources_store["nb-1"] = {
        "src-9": Source(id="src-9", title="Doc", status=SourceStatus.READY)
    }
    resp = authed_client.get("/v1/notebooks/nb-1/sources/src-9")
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == "src-9"
    assert body["status_label"] == "ready"
    assert "kind" in body


def test_source_list_pagination_slices_and_adds_meta(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.sources_store["nb-1"] = {
        f"src-{i}": Source(id=f"src-{i}", title=f"S{i}", status=SourceStatus.READY)
        for i in range(5)
    }
    resp = authed_client.get("/v1/notebooks/nb-1/sources", params={"limit": 2, "offset": 1})
    assert resp.status_code == 200
    body = resp.json()
    assert body["notebook_id"] == "nb-1"
    assert len(body["sources"]) == 2
    assert body["meta"] == {"total": 5, "has_more": True, "limit": 2, "offset": 1}


def test_source_list_bad_limit_is_422(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.sources_store["nb-1"] = {
        "src-1": Source(id="src-1", title="S", status=SourceStatus.READY)
    }
    assert authed_client.get("/v1/notebooks/nb-1/sources", params={"limit": 0}).status_code == 422
    assert authed_client.get("/v1/notebooks/nb-1/sources", params={"offset": -1}).status_code == 422


def test_source_list_limit_over_max_is_422(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.sources_store["nb-1"] = {
        "src-1": Source(id="src-1", title="S", status=SourceStatus.READY)
    }
    over = authed_client.get("/v1/notebooks/nb-1/sources", params={"limit": MAX_LIMIT + 1})
    assert over.status_code == 422
    at_cap = authed_client.get("/v1/notebooks/nb-1/sources", params={"limit": MAX_LIMIT})
    assert at_cap.status_code == 200


def test_source_list_offset_without_limit_is_rejected(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    fake_client.sources_store["nb-1"] = {
        f"src-{i}": Source(id=f"src-{i}", title=f"S{i}", status=SourceStatus.READY)
        for i in range(3)
    }
    # offset>0 with no limit is an ambiguous window → 400, not silently ignored.
    rejected = authed_client.get("/v1/notebooks/nb-1/sources", params={"offset": 2})
    assert rejected.status_code == 400
    assert rejected.json()["error"]["category"] == "validation"
    # offset=0 with no limit is the unchanged full-list default (no meta).
    full = authed_client.get("/v1/notebooks/nb-1/sources", params={"offset": 0})
    assert full.status_code == 200
    body = full.json()
    assert len(body["sources"]) == 3
    assert "meta" not in body


def test_add_url_returns_enriched_view(authed_client: TestClient) -> None:
    # The create path projects the shared enriched view (string kind /
    # status_label), matching GET rather than leaking bare integer codes.
    resp = authed_client.post("/v1/notebooks/nb-1/sources/url", json={"url": "https://example.com"})
    assert resp.status_code == 201
    body = resp.json()
    assert "kind" in body
    assert "status_label" in body
    assert body["status_label"] == "processing"


# --- Phase 4: source rename (PATCH) ------------------------------------------


def _seed_source(
    fake_client: FakeClient, nid: str, sid: str, *, status: SourceStatus = SourceStatus.PROCESSING
) -> None:
    fake_client.sources_store.setdefault(nid, {})[sid] = Source(
        id=sid, title="Old", url=None, status=status
    )


def test_rename_source(authed_client: TestClient, fake_client: FakeClient) -> None:
    _seed_source(fake_client, "nb-1", "src-1")
    resp = authed_client.patch("/v1/notebooks/nb-1/sources/src-1", json={"title": "Renamed"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["title"] == "Renamed"
    # Enriched view shape (matches GET).
    assert "kind" in body and "status_label" in body
    assert fake_client.sources_store["nb-1"]["src-1"].title == "Renamed"


def test_rename_missing_source_is_404(authed_client: TestClient) -> None:
    resp = authed_client.patch("/v1/notebooks/nb-1/sources/ghost", json={"title": "X"})
    assert resp.status_code == 404


# --- Phase 4: Drive source add -----------------------------------------------


def test_add_drive_source(authed_client: TestClient, fake_client: FakeClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/drive",
        json={"document_id": "doc-abc", "title": "Spec", "mime_type": "google-doc"},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["title"] == "Spec"
    assert "status_label" in body
    # The core maps the ``google-doc`` choice to the real Drive MIME before add.
    nid, doc, title, mime = fake_client.added_drive[0]
    assert (nid, doc, title) == ("nb-1", "doc-abc", "Spec")
    assert mime == "application/vnd.google-apps.document"


def test_add_drive_default_mime(authed_client: TestClient, fake_client: FakeClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/sources/drive", json={"document_id": "doc-1"})
    assert resp.status_code == 201
    # Default choice ``google-doc`` → Google Docs MIME.
    assert fake_client.added_drive[0][3] == "application/vnd.google-apps.document"


def test_add_drive_bad_mime_is_422(authed_client: TestClient) -> None:
    # mime_type is a Literal → rejected at the schema boundary.
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/drive",
        json={"document_id": "doc-1", "mime_type": "bogus"},
    )
    assert resp.status_code == 422


# --- Phase 4: batch URL add --------------------------------------------------


def test_add_batch_all_valid(authed_client: TestClient, fake_client: FakeClient) -> None:
    _seed_notebook(fake_client)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch",
        json={"urls": ["https://a.example.com", "https://b.example.com"]},
    )
    assert resp.status_code == 201
    body = resp.json()
    # Top-level status mirrors the MCP batch envelope (parity).
    assert body["status"] == "added"
    assert body["added"] == 2
    assert body["failed"] == 0
    # Results are positional and carry status_label.
    assert body["results"][0]["input"] == "https://a.example.com"
    assert body["results"][0]["status"] == "added"
    assert "status_label" in body["results"][0]


def test_add_batch_partial_failure_isolated(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # A private/SSRF URL fails its own item without aborting the batch.
    _seed_notebook(fake_client)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch",
        json={"urls": ["https://ok.example.com", "http://127.0.0.1:9/secret"]},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "added"  # ≥1 added
    assert body["added"] == 1
    assert body["failed"] == 1
    assert body["results"][1]["status"] == "error"
    err = body["results"][1]["error"]
    assert err["category"] == "validation"
    assert "retriable" in err


def test_add_batch_all_failed_is_200_status_error(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # Every item fails a per-item VALIDATION check → nothing created: 200 (not
    # 201) with a top-level status="error" (MCP _add_url_batch parity), so the
    # envelope never claims success while every result says error.
    _seed_notebook(fake_client)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch",
        json={"urls": ["http://127.0.0.1:9/a", "http://169.254.169.254/b"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    assert body["added"] == 0
    assert body["failed"] == 2


def test_add_batch_bad_notebook_is_top_level_404(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # A shared-context failure (unknown notebook) is validated ONCE up front and
    # surfaces as a top-level 404 — NOT a 201 with every item silently errored.
    # (nb-1 is intentionally NOT seeded.)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch",
        json={"urls": ["https://a.example.com", "https://b.example.com"]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["category"] == "not_found"
    # No item was attempted (the batch aborted before the loop).
    assert not fake_client.sources_store.get("nb-1")


def test_add_batch_stale_auth_is_top_level_401(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: object
) -> None:
    # Stale auth on the shared preflight surfaces as a top-level 401, not a
    # 201-all-errored body.
    import pytest

    from notebooklm.exceptions import AuthError

    assert isinstance(monkeypatch, pytest.MonkeyPatch)

    async def _boom(notebook_id: str) -> object:
        raise AuthError("session expired")

    monkeypatch.setattr(fake_client.notebooks, "get", _boom)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch", json={"urls": ["https://a.example.com"]}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["category"] == "auth"


def test_add_batch_mid_item_auth_is_top_level_401(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: object
) -> None:
    # An AUTH failure raised DURING an item's add (not the shared preflight) must
    # propagate as a top-level 401 — NOT be folded into a per-item error and
    # returned as a 200/201 batch envelope.
    import pytest

    from notebooklm.exceptions import AuthError

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    _seed_notebook(fake_client)

    async def _boom(notebook_id: str, url: str) -> object:
        raise AuthError("session expired mid-batch")

    monkeypatch.setattr(fake_client.sources, "add_url", _boom)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch",
        json={"urls": ["https://a.example.com", "https://b.example.com"]},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["category"] == "auth"
    # It aborted — no batch envelope with per-item results.
    assert "results" not in resp.json()


def test_add_batch_mid_item_rate_limit_is_top_level_429(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: object
) -> None:
    # A RATE_LIMIT failure mid-batch is fatal too (429), not a per-item error.
    import pytest

    from notebooklm.exceptions import RateLimitError

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    _seed_notebook(fake_client)

    async def _boom(notebook_id: str, url: str) -> object:
        raise RateLimitError("slow down")

    monkeypatch.setattr(fake_client.sources, "add_url", _boom)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch", json={"urls": ["https://a.example.com"]}
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["category"] == "rate_limited"
    assert "results" not in resp.json()


def test_add_batch_per_item_error_is_redacted(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: object
) -> None:
    # A per-item (isolated, 4xx-input) failure whose message carries a home path
    # must be scrubbed in the batch envelope — no raw /home/<user>/ leak.
    import pytest

    from notebooklm.exceptions import ValidationError

    assert isinstance(monkeypatch, pytest.MonkeyPatch)
    _seed_notebook(fake_client)

    async def _boom(notebook_id: str, url: str) -> object:
        raise ValidationError("bad source spooled to /home/alice/secret.txt")

    monkeypatch.setattr(fake_client.sources, "add_url", _boom)
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/batch", json={"urls": ["https://a.example.com"]}
    )
    # A VALIDATION failure stays isolated: nothing created → 200 status=error.
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "error"
    message = body["results"][0]["error"]["message"]
    assert "/home/alice" not in message
    assert "/home/***" in message


def test_add_batch_empty_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/sources/batch", json={"urls": []})
    assert resp.status_code == 400


# --- Phase 4: source_wait ----------------------------------------------------


def test_wait_specific_source_ready(authed_client: TestClient, fake_client: FakeClient) -> None:
    _seed_source(fake_client, "nb-1", "src-1")
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/wait", json={"source_ids": ["src-1"], "timeout": 1.0}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is True
    assert len(body["ready"]) == 1
    assert body["ready"][0]["status_label"] == "ready"
    assert body["timed_out"] == [] and body["failed"] == [] and body["not_found"] == []


def test_wait_all_sources_partial(authed_client: TestClient, fake_client: FakeClient) -> None:
    _seed_source(fake_client, "nb-1", "src-ok")
    _seed_source(fake_client, "nb-1", "src-slow")
    _seed_source(fake_client, "nb-1", "src-bad")
    fake_client.wait_outcomes["src-slow"] = "timeout"
    fake_client.wait_outcomes["src-bad"] = "processing"
    # No source_ids → wait for every source in the notebook.
    resp = authed_client.post("/v1/notebooks/nb-1/sources/wait", json={"timeout": 0.5})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    ready_ids = {r["id"] for r in body["ready"]}
    assert ready_ids == {"src-ok"}
    assert body["timed_out"][0]["source_id"] == "src-slow"
    assert body["failed"][0]["source_id"] == "src-bad"


def test_wait_not_found_bucket(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.wait_outcomes["src-x"] = "not_found"
    resp = authed_client.post("/v1/notebooks/nb-1/sources/wait", json={"source_ids": ["src-x"]})
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False
    assert body["not_found"][0]["source_id"] == "src-x"


def test_wait_bad_timeout_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/wait", json={"source_ids": ["s"], "timeout": -1}
    )
    assert resp.status_code == 400


def test_wait_bad_interval_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/wait", json={"source_ids": ["s"], "interval": 0}
    )
    assert resp.status_code == 400


def test_wait_non_finite_timeout_is_rejected(authed_client: TestClient) -> None:
    # JSON allows Infinity / NaN; timeout=inf would wait forever, NaN breaks
    # comparisons. Both must be rejected before any waiting starts.
    for bad in ("Infinity", "NaN"):
        resp = authed_client.post(
            "/v1/notebooks/nb-1/sources/wait",
            content=f'{{"source_ids": ["s"], "timeout": {bad}}}',
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code in (400, 422), (bad, resp.status_code)


def test_wait_non_finite_interval_is_rejected(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/wait",
        content='{"source_ids": ["s"], "interval": Infinity}',
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code in (400, 422)


def test_wait_timeout_over_max_is_400(authed_client: TestClient) -> None:
    from notebooklm.server.routes.sources import MAX_WAIT_TIMEOUT

    resp = authed_client.post(
        "/v1/notebooks/nb-1/sources/wait",
        json={"source_ids": ["s"], "timeout": MAX_WAIT_TIMEOUT + 1},
    )
    assert resp.status_code == 400


def test_wait_explicit_empty_source_ids_is_400(authed_client: TestClient) -> None:
    # An explicit empty list would return immediate ok:true (false-ready if a
    # caller serialized "all" as []). Reject it; omitting source_ids waits all.
    resp = authed_client.post("/v1/notebooks/nb-1/sources/wait", json={"source_ids": []})
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "validation"


def test_wait_bucket_entry_redacts_error_text() -> None:
    # A handled wait failure's error text is scrubbed via safe_detail before it
    # reaches the {source_id, error} bucket — no raw /home/<user>/ leak (F7).
    from notebooklm.server.routes.sources import _wait_bucket_entry

    class _Err:
        source_id = "src-1"

        def __str__(self) -> str:
            return "processing failed, spooled to /home/alice/tmp/x"

    entry = _wait_bucket_entry(_Err())
    assert entry["source_id"] == "src-1"
    assert "/home/alice" not in entry["error"]
    assert "/home/***" in entry["error"]
