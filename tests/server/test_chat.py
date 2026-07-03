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


def test_ask_strips_raw_response_debug_blob(authed_client: TestClient) -> None:
    """The internal ``raw_response`` debug blob is never serialized (shared view)."""
    resp = authed_client.post("/v1/notebooks/nb-1/chat", json={"question": "What is X?"})
    assert resp.status_code == 200
    assert "raw_response" not in resp.json()


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


# --- POST /v1/notebooks/{id}/chat/configure ---------------------------------


def test_configure_preset_writes_mode(authed_client: TestClient, fake_client: FakeClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/chat/configure", json={"chat_mode": "learning-guide"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "configured"
    assert body["mode"] == "learning-guide"
    # A preset short-circuits to set_mode (not the custom configure path).
    assert fake_client.last_configure is not None
    assert fake_client.last_configure["notebook_id"] == "nb-1"
    assert "mode" in fake_client.last_configure


def test_configure_custom_writes_goal_and_length(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/chat/configure",
        json={"goal": "Explain like I'm five", "response_length": "shorter"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "configured"
    assert body["mode"] is None
    # A non-empty goal selects the CUSTOM chat goal.
    assert body["goal_name"] == "custom"
    assert body["persona"] == "Explain like I'm five"
    assert body["response_length"] == "shorter"
    assert fake_client.last_configure is not None
    assert fake_client.last_configure["custom_prompt"] == "Explain like I'm five"


def test_configure_partial_merges_preserving_omitted_field(
    authed_client: TestClient, fake_client: FakeClient
) -> None:
    """A length-only REST configure merges: the current CUSTOM goal+persona is preserved (#1751)."""
    from notebooklm.rpc.types import ChatGoal, ChatResponseLength
    from notebooklm.types import ChatSettings

    fake_client.chat_settings = ChatSettings(
        goal=ChatGoal.CUSTOM,
        response_length=ChatResponseLength.DEFAULT,
        custom_prompt="keep this persona",
    )
    resp = authed_client.post(
        "/v1/notebooks/nb-1/chat/configure",
        json={"response_length": "longer"},
    )
    assert resp.status_code == 200
    # The read-modify-write read happened, and the omitted goal/persona is preserved.
    assert fake_client.last_get_settings == "nb-1"
    assert fake_client.last_configure is not None
    assert fake_client.last_configure["goal"] == ChatGoal.CUSTOM
    assert fake_client.last_configure["response_length"] == ChatResponseLength.LONGER
    assert fake_client.last_configure["custom_prompt"] == "keep this persona"


def test_configure_preset_and_custom_conflict_is_400(authed_client: TestClient) -> None:
    resp = authed_client.post(
        "/v1/notebooks/nb-1/chat/configure",
        json={"chat_mode": "concise", "response_length": "longer"},
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["category"] == "validation"


def test_configure_unknown_mode_is_422(authed_client: TestClient) -> None:
    # ``chat_mode`` is a Literal, so an out-of-enum value is rejected at the
    # request-schema boundary (422), not reaching the core.
    resp = authed_client.post("/v1/notebooks/nb-1/chat/configure", json={"chat_mode": "bogus"})
    assert resp.status_code == 422
