"""U6: POST /v1/notebooks/{id}/chat — blocking ask."""

from __future__ import annotations

from fastapi.testclient import TestClient

from notebooklm.exceptions import RateLimitError

from .fakes import FakeClient


def test_ask_returns_full_answer(authed_client: TestClient) -> None:
    resp = authed_client.post("/v1/notebooks/nb-1/chat", json={"question": "What is X?"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "answer to: What is X?"
    assert body["conversation_id"] == "conv-1"
    assert "references" in body


def test_conversation_id_is_forwarded(authed_client: TestClient, fake_client: FakeClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/chat",
        json={"question": "follow up", "conversation_id": "conv-42"},
    )
    assert resp.status_code == 200
    assert resp.json()["conversation_id"] == "conv-42"
    assert fake_client.last_ask == {"notebook_id": "nb-1", "conversation_id": "conv-42"}


def test_rate_limited_ask_is_429(authed_client: TestClient, fake_client: FakeClient) -> None:
    fake_client.chat_error = RateLimitError("slow down")
    resp = authed_client.post("/v1/notebooks/nb-1/chat", json={"question": "hi"})
    assert resp.status_code == 429
    assert resp.json()["error"]["category"] == "rate_limited"


def test_unauthorized_is_401(raw_client: TestClient) -> None:
    resp = raw_client.post(
        "/v1/notebooks/nb-1/chat", json={"question": "hi"}, headers={"Host": "127.0.0.1"}
    )
    assert resp.status_code == 401
