"""Deep-research routes — /v1/notebooks/{id}/research start / status / cancel / import."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from notebooklm._types.research import ResearchStart

from .fakes import FakeClient


def test_start_status_import_happy_path(authed_client: TestClient, fake_client: FakeClient) -> None:
    # Start (202): fast mode has only a task_id, so poll_id == task_id.
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "topic"})
    assert resp.status_code == 202
    body = resp.json()
    poll_id = body["poll_id"]
    assert body["report_id"] is None
    assert poll_id == body["task_id"]
    assert fake_client.last_research_start == {
        "notebook_id": "nb-1",
        "query": "topic",
        "source": "web",
        "mode": "fast",
    }

    # Status: in progress right after start.
    resp = authed_client.get(f"/v1/notebooks/nb-1/research/{poll_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "in_progress"

    # Drive the run to completion with one found source.
    fake_client.set_research_completed(
        "nb-1", poll_id, sources=[{"url": "https://a.example", "title": "A"}]
    )
    resp = authed_client.get(f"/v1/notebooks/nb-1/research/{poll_id}")
    body = resp.json()
    assert body["status"] == "completed"
    assert body["run_id"] == poll_id
    assert len(body["sources"]) == 1

    # Import the completed run's sources.
    resp = authed_client.post(f"/v1/notebooks/nb-1/research/{poll_id}/import")
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "imported"
    assert body["sources_found"] == 1
    assert len(body["imported"]) == 1
    # The import is keyed on the poll id, not the notebook's current task.
    assert fake_client.imported_research[0][0] == "nb-1"
    assert fake_client.imported_research[0][1] == poll_id


def test_deep_start_poll_id_is_report_id(authed_client: TestClient) -> None:
    # Deep mode returns BOTH a task_id and a report_id; poll_id must be the
    # report_id (deep's task_id is a sessionId the poll/cancel cannot use).
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t", "mode": "deep"})
    assert resp.status_code == 202
    body = resp.json()
    assert body["report_id"] is not None
    assert body["poll_id"] == body["report_id"]
    assert body["poll_id"] != body["task_id"]


def test_deep_start_status_import_happy_path(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # Deep mode keys the whole workflow off report_id: prove start→status→import
    # survives end-to-end when poll_id is the report_id (not the sessionId task_id).
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t", "mode": "deep"})
    assert resp.status_code == 202
    body = resp.json()
    poll_id = body["poll_id"]
    assert poll_id == body["report_id"]
    assert poll_id != body["task_id"]

    # Status polls the report_id and sees the in-progress run.
    resp = authed_client.get(f"/v1/notebooks/nb-1/research/{poll_id}")
    assert resp.json()["status"] == "in_progress"

    fake_client.set_research_completed(
        "nb-1", poll_id, sources=[{"url": "https://a.example", "title": "A"}]
    )
    resp = authed_client.post(f"/v1/notebooks/nb-1/research/{poll_id}/import")
    assert resp.status_code == 201
    assert resp.json()["sources_found"] == 1
    # The import keyed on the report_id (the deep task_id would poll as not_found).
    assert fake_client.imported_research[0][1] == poll_id


def test_deep_start_missing_report_id_raises(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # A deep start that returns no report_id cannot form a pollable id — the
    # route must fail loud (server/decode error) rather than emit the sessionId
    # task_id that would immediately poll as not_found.
    fake_client.deep_missing_report_id = True
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t", "mode": "deep"})
    assert resp.status_code >= 500
    assert "poll_id" not in resp.json()


def test_import_failed_run_rejected(authed_client: TestClient, fake_client: FakeClient) -> None:
    # A FAILED run is a distinct non-importable branch — refuse (400) and never
    # touch import_sources.
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t"})
    poll_id = resp.json()["poll_id"]
    fake_client.set_research_failed("nb-1", poll_id)
    resp = authed_client.post(f"/v1/notebooks/nb-1/research/{poll_id}/import")
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "validation"
    assert "failed" in resp.json()["error"]["message"].lower()
    assert fake_client.imported_research == []


def test_deep_drive_combination_rejected(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/research", json={"query": "t", "source": "drive", "mode": "deep"}
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "validation"


def test_cancel_is_fire_and_forget(authed_client: TestClient, fake_client: FakeClient) -> None:
    # Cancel does not validate the id — an unknown run still reports cancelled.
    resp = authed_client.delete("/v1/notebooks/nb-1/research/run-9")
    assert resp.status_code == 200
    body = resp.json()
    assert body["cancelled"] is True
    assert body["run_id"] == "run-9"
    assert ("nb-1", "run-9") in fake_client.cancelled_research


def test_import_before_complete_rejected(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t"})
    poll_id = resp.json()["poll_id"]
    # Still in progress — import must refuse and never touch import_sources.
    resp = authed_client.post(f"/v1/notebooks/nb-1/research/{poll_id}/import")
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "validation"
    assert fake_client.imported_research == []


def test_import_unknown_run_rejected(authed_client: TestClient, fake_client: FakeClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/research/does-not-exist/import")
    assert resp.status_code == 400
    assert fake_client.imported_research == []


def test_import_empty_completed_rejected(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t"})
    poll_id = resp.json()["poll_id"]
    fake_client.set_research_completed("nb-1", poll_id, sources=[])
    resp = authed_client.post(f"/v1/notebooks/nb-1/research/{poll_id}/import")
    assert resp.status_code == 400
    assert fake_client.imported_research == []


def test_status_unknown_run_is_not_found(authed_client: TestClient) -> None:
    resp = authed_client.get("/v1/notebooks/nb-1/research/ghost")
    assert resp.status_code == 200
    assert resp.json()["status"] == "not_found"


def test_start_explicit_discriminators_win_over_spread(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # ``to_jsonable(result)`` carries ``notebook_id`` (and ``task_id`` /
    # ``report_id``), so the explicit ``notebook_id`` / ``poll_id`` must be spread
    # LAST or a stale/embedded value could clobber the computed discriminator.
    # Return a start whose embedded ``notebook_id`` DIFFERS from the path arg.
    async def _start(
        notebook_id: str, query: str, source: str = "web", mode: str = "fast"
    ) -> ResearchStart:
        return ResearchStart(
            task_id="rtask-x",
            report_id=None,
            notebook_id="EMBEDDED-WRONG",
            query=query,
            mode=mode,
        )

    monkeypatch.setattr(fake_client.research, "start", _start)
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t"})
    assert resp.status_code == 202
    body = resp.json()
    # The explicit path notebook_id wins over the spread's embedded one.
    assert body["notebook_id"] == "nb-1"
    # poll_id is the computed fast task_id, present alongside the spread.
    assert body["poll_id"] == "rtask-x"
    assert body["task_id"] == "rtask-x"


def test_fast_start_missing_task_id_raises(
    authed_client: TestClient, fake_client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A fast start that returns no task_id cannot form a pollable poll_id — the
    # route must fail loud (parallel to the deep no-report_id guard) rather than
    # emit an empty, unpollable poll_id.
    async def _start(
        notebook_id: str, query: str, source: str = "web", mode: str = "fast"
    ) -> ResearchStart:
        return ResearchStart(
            task_id="", report_id=None, notebook_id=notebook_id, query=query, mode=mode
        )

    monkeypatch.setattr(fake_client.research, "start", _start)
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t"})
    assert resp.status_code >= 500
    assert "poll_id" not in resp.json()


def test_import_records_imported_ids_in_pending(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    # After an import, each imported source id is recorded in the pending registry
    # so a source poll for it resolves to 200 pending (not a spurious 404) during
    # the not-yet-listable window (same provenance contract as the create routes).
    resp = authed_client.post("/v1/notebooks/nb-1/research", json={"query": "t"})
    poll_id = resp.json()["poll_id"]
    fake_client.set_research_completed(
        "nb-1", poll_id, sources=[{"url": "https://a.example", "title": "A"}]
    )
    resp = authed_client.post(f"/v1/notebooks/nb-1/research/{poll_id}/import")
    assert resp.status_code == 201
    imported = resp.json()["imported"]
    assert len(imported) == 1
    imported_id = imported[0]["id"]

    # The imported id (not in the source store yet) is now pending-known → 200.
    resp = authed_client.get(f"/v1/notebooks/nb-1/sources/{imported_id}")
    assert resp.status_code == 200
    assert resp.json()["status"] == "pending"

    # An id that was never imported is still an unknown 404 (guards over-recording).
    resp = authed_client.get("/v1/notebooks/nb-1/sources/never-imported")
    assert resp.status_code == 404


def test_unauthorized_is_401(raw_client: TestClient) -> None:
    resp = raw_client.post(
        "/v1/notebooks/nb-1/research", json={"query": "t"}, headers={"Host": "127.0.0.1"}
    )
    assert resp.status_code == 401
