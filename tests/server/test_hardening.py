"""Regression tests for the polish-pass security hardening.

Covers the review findings fixed after the initial implementation:
- Host-header parser edge cases (case-folding, bracketed-IPv6 trailing garbage)
- error-envelope secret scrubbing + generic message for unexpected bugs
- pre-buffer upload size limit (Content-Length rejected before the body is read)
- 204 responses carry no body
- the pending registry is bounded
"""

from __future__ import annotations

from typing import Any

import pytest

pytest.importorskip("fastapi")

from notebooklm.server import app as app_module  # noqa: E402
from notebooklm.server._auth import _host_is_loopback  # noqa: E402
from notebooklm.server._errors import _redact  # noqa: E402
from notebooklm.server._pending import _MAX_ENTRIES, PendingRegistry  # noqa: E402


class TestHostLoopbackGuard:
    @pytest.mark.parametrize(
        "host",
        [
            "127.0.0.1",
            "localhost",
            "LOCALHOST",
            "Localhost",
            "::1",
            "[::1]",
            "[::1]:8000",
            "127.0.0.1:9000",
            "localhost:8000",
        ],
    )
    def test_accepts_loopback(self, host: str) -> None:
        assert _host_is_loopback(host) is True

    @pytest.mark.parametrize(
        "host",
        [
            "[::1]evil.com",
            "[::1]@evil.com",
            "[::1]:bad",
            "evil.com",
            "0.0.0.0",
            "127.0.0.1.evil.com",
            "",
            "[::1",
            "2130706433",
        ],
    )
    def test_rejects_non_loopback(self, host: str) -> None:
        assert _host_is_loopback(host) is False


class TestErrorScrubbing:
    def test_redact_masks_credential_shaped_text(self) -> None:
        out = _redact("failed Authorization: Bearer abcSECRET123 while decoding")
        assert "abcSECRET123" not in out
        assert "***" in out

    def test_redact_caps_length(self) -> None:
        out = _redact("x" * 5000)
        assert len(out) <= 301  # 300 + the ellipsis

    def test_unexpected_bug_message_is_generic(self) -> None:
        from notebooklm.server._errors import error_response

        # A non-library exception (a bug) must never echo its str() — which could
        # carry anything — to the client.
        resp = error_response(RuntimeError("Bearer leakedTOKEN in a stray bug"))
        body = resp.body.decode()
        assert "leakedTOKEN" not in body
        assert "Internal server error" in body
        assert resp.status_code == 500


class TestUploadPreBufferLimit:
    def test_oversized_content_length_is_413_before_handler(
        self, app: Any, fake_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        # Spy: the upload handler must never run when the declared length is over cap.
        called = {"add_file": False}
        orig = fake_client.sources.add_file

        async def spy(*args: Any, **kwargs: Any) -> Any:
            called["add_file"] = True
            return await orig(*args, **kwargs)

        monkeypatch.setattr(fake_client.sources, "add_file", spy)
        monkeypatch.setattr(app_module, "MAX_UPLOAD_BYTES", 8)

        headers = {"Authorization": "Bearer test-token", "Host": "127.0.0.1"}
        with TestClient(app, headers=headers, raise_server_exceptions=False) as c:
            resp = c.post(
                "/v1/notebooks/nb-1/sources/file",
                files={"file": ("big.txt", b"x" * 4096, "text/plain")},
            )
        assert resp.status_code == 413
        assert called["add_file"] is False
        assert resp.json()["error"]["category"] == "validation"


class TestNoContentResponses:
    def test_delete_notebook_has_empty_body(self, authed_client: Any) -> None:
        resp = authed_client.delete("/v1/notebooks/nb-1")
        assert resp.status_code == 204
        assert resp.content == b""

    def test_delete_source_has_empty_body(self, authed_client: Any) -> None:
        resp = authed_client.delete("/v1/notebooks/nb-1/sources/src-1")
        assert resp.status_code == 204
        assert resp.content == b""


class TestUploadSuffixSanitization:
    """The temp-upload extension is rebuilt from a strict allowlist so no
    attacker-controlled string from the multipart filename reaches the path."""

    @pytest.mark.parametrize(
        "filename,expected",
        [
            ("report.pdf", ".pdf"),
            ("AUDIO.MP3", ".mp3"),  # lowercased
            ("archive.tar.gz", ".gz"),  # only the final extension
            ("noext", ""),
            (None, ""),
            ("evil.", ""),  # empty extension
            ("x." + "a" * 20, ""),  # over the 16-char cap
            ("dir.d/file", ""),  # dot before the final separator → no ext
            ("../../etc/passwd", ""),  # traversal-shaped, no real extension
            ("payload.sh;rm", ""),  # non-alphanumeric chars rejected
        ],
    )
    def test_safe_suffix(self, filename: str | None, expected: str) -> None:
        from notebooklm.server.routes.sources import _safe_suffix

        out = _safe_suffix(filename)
        assert out == expected
        # Invariant: whatever we return never contains a path separator.
        assert "/" not in out and "\\" not in out


class TestErrorEnvelopeShape:
    """Hand-raised ``HTTPException``s use the same ``{error:{category,message}}``
    envelope as classified library errors (R9 single-shape contract), not
    FastAPI's default ``{"detail": ...}``."""

    def test_loopback_guard_403_uses_envelope(self, raw_client: Any) -> None:
        # raw_client sends Host: testserver (not loopback) → 403 from the guard.
        resp = raw_client.get("/v1/notebooks")
        assert resp.status_code == 403
        body = resp.json()
        assert "detail" not in body
        assert body["error"]["category"] == "auth"
        assert isinstance(body["error"]["message"], str)

    def test_missing_token_401_uses_envelope(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        # Loopback Host clears the rebinding guard; the wrong token trips the 401.
        headers = {"Authorization": "Bearer wrong-token", "Host": "127.0.0.1"}
        with TestClient(app, headers=headers, raise_server_exceptions=False) as c:
            resp = c.get("/v1/notebooks")
        assert resp.status_code == 401
        body = resp.json()
        assert "detail" not in body
        assert body["error"]["category"] == "auth"

    def test_route_404_uses_envelope(self, authed_client: Any) -> None:
        # An unknown source is an in-route HTTPException(404) — not a classified
        # library error — and must still render the envelope.
        resp = authed_client.get("/v1/notebooks/nb-1/sources/missing")
        assert resp.status_code == 404
        body = resp.json()
        assert "detail" not in body
        assert body["error"]["category"] == "not_found"


class TestPendingRegistryBounded:
    def test_eviction_past_cap(self) -> None:
        reg = PendingRegistry()
        # Record cap + 5; the 5 oldest are evicted (their later poll → 404).
        for i in range(_MAX_ENTRIES + 5):
            reg.record("nb", f"id-{i}")
        assert reg.knows("nb", "id-0") is False
        assert reg.knows("nb", "id-4") is False
        assert reg.knows("nb", f"id-{_MAX_ENTRIES + 4}") is True

    def test_record_is_idempotent(self) -> None:
        reg = PendingRegistry()
        reg.record("nb", "x")
        reg.record("nb", "x")
        reg.drop("nb", "x")
        assert reg.knows("nb", "x") is False
