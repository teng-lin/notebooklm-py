"""Regression tests for the polish-pass security hardening.

Covers the review findings fixed after the initial implementation:
- Host-header parser edge cases (case-folding, bracketed-IPv6 trailing garbage)
- error-envelope secret scrubbing + generic message for unexpected bugs
- pre-buffer upload size limit (Content-Length rejected before the body is read)
- 204 responses carry no body
- the pending registry is bounded
"""

from __future__ import annotations

import os
from typing import Any

import pytest

pytest.importorskip("fastapi")

from notebooklm.server import app as app_module  # noqa: E402
from notebooklm.server._auth import _host_is_loopback  # noqa: E402
from notebooklm.server._errors import _redact  # noqa: E402
from notebooklm.server._pending import _MAX_ENTRIES, PendingRegistry  # noqa: E402

from .conftest import TEST_TOKEN  # noqa: E402


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

        headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
        with TestClient(
            app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
        ) as c:
            resp = c.post(
                "/v1/notebooks/nb-1/sources/file",
                files={"file": ("big.txt", b"x" * 4096, "text/plain")},
            )
        assert resp.status_code == 413
        assert called["add_file"] is False
        assert resp.json()["error"]["category"] == "validation"


class TestChunkedMultipartRejected:
    def test_multipart_without_content_length_is_411(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        # A generator body makes httpx use chunked transfer-encoding (no
        # Content-Length), the exact bypass a running-cap-after-spool leaves open.
        def _chunks() -> Any:
            yield b"--b\r\nContent-Disposition: form-data; name=file; filename=x\r\n\r\n"
            yield b"data\r\n--b--\r\n"

        headers = {
            "Authorization": f"Bearer {TEST_TOKEN}",
            "Host": "127.0.0.1",
            "Content-Type": "multipart/form-data; boundary=b",
        }
        with TestClient(
            app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
        ) as c:
            resp = c.post("/v1/notebooks/nb-1/sources/file", content=_chunks())
        assert resp.status_code == 411
        assert resp.json()["error"]["category"] == "validation"


class TestPeerLoopbackGuard:
    def test_non_loopback_peer_rejected_even_with_spoofed_host(self, app: Any) -> None:
        from fastapi.testclient import TestClient

        # Off-loopback PEER address + a forged loopback Host header: the
        # unspoofable peer-address check rejects it (the Host spoof does not help).
        headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
        with TestClient(
            app, headers=headers, client=("8.8.8.8", 5555), raise_server_exceptions=False
        ) as c:
            resp = c.get("/v1/notebooks")
        assert resp.status_code == 403
        assert resp.json()["error"]["category"] == "auth"

    @pytest.mark.parametrize(
        ("addr", "expected"),
        [
            ("127.0.0.1", True),
            ("::1", True),
            # IPv4-mapped IPv6 loopback: ``ipaddress.is_loopback`` only resolves the
            # mapped IPv4 in newer CPython patch levels, so this must be pinned
            # version-independently (it regressed on some macOS 3.10/3.11 runners).
            ("::ffff:127.0.0.1", True),
            ("8.8.8.8", False),
            ("::ffff:8.8.8.8", False),  # mapped *external* — must NOT read as loopback
            ("0.0.0.0", False),
            ("not-an-ip", False),
        ],
    )
    def test_addr_is_loopback_version_independent(self, addr: str, expected: bool) -> None:
        from notebooklm.server._auth import _addr_is_loopback

        assert _addr_is_loopback(addr) is expected

    @pytest.mark.parametrize("peer_host", ["::1", "::ffff:127.0.0.1"])
    def test_ipv6_loopback_peer_allowed(self, app: Any, peer_host: str) -> None:
        from fastapi.testclient import TestClient

        # An IPv6 loopback peer (``::1``) and the IPv4-mapped loopback
        # (``::ffff:127.0.0.1``) both satisfy the unspoofable peer-loopback guard.
        headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "127.0.0.1"}
        with TestClient(
            app, headers=headers, client=(peer_host, 5555), raise_server_exceptions=False
        ) as c:
            resp = c.get("/v1/notebooks")
        assert resp.status_code == 200

    def test_external_bind_optin_allows_non_loopback_peer(
        self, app: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from fastapi.testclient import TestClient

        from notebooklm.server._auth import ALLOW_EXTERNAL_BIND_ENV

        monkeypatch.setenv(ALLOW_EXTERNAL_BIND_ENV, "1")
        headers = {"Authorization": f"Bearer {TEST_TOKEN}", "Host": "example.com"}
        with TestClient(
            app, headers=headers, client=("8.8.8.8", 5555), raise_server_exceptions=False
        ) as c:
            resp = c.get("/v1/notebooks")
        assert resp.status_code == 200


class TestNoContentResponses:
    def test_delete_notebook_has_empty_body(self, authed_client: Any) -> None:
        resp = authed_client.delete("/v1/notebooks/nb-1")
        assert resp.status_code == 204
        assert resp.content == b""

    def test_delete_source_has_empty_body(self, authed_client: Any) -> None:
        resp = authed_client.delete("/v1/notebooks/nb-1/sources/src-1")
        assert resp.status_code == 204
        assert resp.content == b""


class TestUploadPathSafety:
    """The upload spools into a private ``mkdtemp`` dir named after the caller's
    *basename* — directory components are stripped (traversal guard) and the file
    is isolated, so a malicious filename can neither escape nor reach a real path.
    """

    def test_malicious_filename_is_basenamed_and_isolated(
        self, authed_client: Any, fake_client: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}
        orig = fake_client.sources.add_file

        async def spy(notebook_id: str, path: str, *args: Any, **kwargs: Any) -> Any:
            captured["path"] = path
            return await orig(notebook_id, path, *args, **kwargs)

        monkeypatch.setattr(fake_client.sources, "add_file", spy)

        evil = "../../etc/passwd"
        resp = authed_client.post(
            "/v1/notebooks/nb-1/sources/file",
            files={"file": (evil, b"data", "application/pdf")},
        )
        assert resp.status_code == 201
        path = captured["path"]
        # The traversal is stripped: the file lives directly under a unique
        # server-owned ``nblm-upload-`` dir, named only by the basename.
        assert os.path.basename(path) == "passwd"
        assert os.path.basename(os.path.dirname(path)).startswith("nblm-upload-")
        # Absolute, canonical, no traversal component escapes our temp dir.
        assert os.path.isabs(path) and ".." not in path

    def test_safe_upload_name(self) -> None:
        # Now the shared neutral helper (_app.source_add.safe_upload_name): control
        # chars stripped, . / .. rejected, extension preserved on truncation.
        from notebooklm.server.routes.sources import _safe_upload_name

        assert _safe_upload_name("report.pdf") == "report.pdf"
        assert _safe_upload_name("../../etc/passwd") == "passwd"  # traversal stripped
        assert _safe_upload_name("a/b/c.txt") == "c.txt"
        assert _safe_upload_name("a\x00b.pdf") == "ab.pdf"  # control chars stripped
        assert _safe_upload_name("a\x7fb.pdf") == "ab.pdf"  # DEL stripped
        assert _safe_upload_name("a\x85b.pdf") == "ab.pdf"  # C1 control stripped
        assert _safe_upload_name("..") == "upload.bin"  # directory cursor rejected
        assert _safe_upload_name("") == "upload.bin"  # empty fallback
        assert _safe_upload_name(None) == "upload.bin"
        # Length-bounded AND extension-preserving (stem truncated, ".pdf" kept).
        long = _safe_upload_name("x" * 500 + ".pdf")
        assert len(long) <= 255 and long.endswith(".pdf")
        # Multibyte names are bounded by BYTE length (255), not char count.
        emoji = _safe_upload_name("😀" * 300 + ".pdf")
        assert len(emoji.encode("utf-8")) <= 255 and emoji.endswith(".pdf")


class TestGenerateSourceDefaulting:
    """A bare generate (no ``source_ids``) scopes to ALL sources, like the CLI:
    ``passthrough_source_ids`` resolves an empty selection to ``None`` (the
    client's all-sources sentinel), not an empty tuple (which the API rejects as
    "… generation is unavailable")."""

    async def test_empty_resolves_to_none(self) -> None:
        from notebooklm.server.routes._passthrough import passthrough_source_ids

        assert await passthrough_source_ids(None, "nb", ()) is None
        assert await passthrough_source_ids(None, "nb", []) is None

    async def test_nonempty_passes_through(self) -> None:
        from notebooklm.server.routes._passthrough import passthrough_source_ids

        assert await passthrough_source_ids(None, "nb", ("s1", "s2")) == ("s1", "s2")


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

        # Loopback peer + Host clear the rebinding guard; the wrong token trips 401.
        headers = {"Authorization": "Bearer wrong-token", "Host": "127.0.0.1"}
        with TestClient(
            app, headers=headers, client=("127.0.0.1", 5555), raise_server_exceptions=False
        ) as c:
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
