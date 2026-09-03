"""Response-aware and secret-safe Android artifact transfer tests."""

from __future__ import annotations

import asyncio
import logging
import sys
import traceback
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm._android.assets as assets_module
from notebooklm._android.assets import AndroidAssetDownloadService
from notebooklm._android.auth import BearerCredential
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._runtime.lifecycle import TransportLifecycle
from notebooklm.exceptions import ArtifactDownloadError, AuthError

PNG = b"\x89PNG\r\n\x1a\n" + b"synthetic-png-body"
MP4 = b"\x00\x00\x00\x18ftypmp42synthetic-mp4-body"
WAV = b"RIFF\x24\x00\x00\x00WAVEsynthetic-wav-body"
PDF = b"%PDF-1.7\nsynthetic-pdf-body"
PPTX = b"PK\x03\x04synthetic-pptx-body"
INITIAL = "https://lh3.googleusercontent.com/image.png?capability=initial-secret"
SIGNED = "https://storage.googleapis.com/bucket/image.png?X-Goog-Signature=signed-secret"
BEARER = "bearer-secret-value"


class FakeBearer:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.invalidations: list[int] = []

    async def get(self, expected_epoch: int) -> BearerCredential:
        self.calls.append(expected_epoch)
        return BearerCredential(BEARER, generation=17)

    def invalidate(self, generation: int) -> None:
        self.invalidations.append(generation)


class FakeResponse:
    def __init__(
        self,
        status_code: int,
        *,
        headers: dict[str, str] | list[tuple[str, str]] | None = None,
        chunks: list[bytes] | None = None,
        gate: asyncio.Event | None = None,
        error: BaseException | None = None,
        wait_again_after_cancel: bool = False,
    ) -> None:
        self.status_code = status_code
        self.headers = httpx.Headers(headers or {})
        self.chunks = chunks or []
        self.gate = gate
        self.error = error
        self.wait_again_after_cancel = wait_again_after_cancel
        self.iterations = 0

    async def aiter_bytes(self):
        self.iterations += 1
        if self.gate is not None:
            try:
                await self.gate.wait()
            except asyncio.CancelledError:
                if not self.wait_again_after_cancel:
                    raise
                await self.gate.wait()
                raise
        if self.error is not None:
            raise self.error
        for chunk in self.chunks:
            yield chunk


class FakeResponseContext:
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        self.outcome = outcome
        self.exits = 0

    async def __aenter__(self) -> FakeResponse:
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.exits += 1


class FakeClient:
    def __init__(
        self,
        outcomes: list[FakeResponse | BaseException],
        *,
        close_gate: asyncio.Event | None = None,
    ) -> None:
        self.outcomes = list(outcomes)
        self.requests: list[tuple[str, str, dict[str, str], bool]] = []
        self.contexts: list[FakeResponseContext] = []
        self.closed = 0
        self.close_gate = close_gate

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
    ) -> FakeResponseContext:
        self.requests.append((method, url, dict(headers), follow_redirects))
        context = FakeResponseContext(self.outcomes.pop(0))
        self.contexts.append(context)
        return context

    async def aclose(self) -> None:
        self.closed += 1
        if self.close_gate is not None:
            self.close_gate.set()


class _FakeCookieJar:
    def __init__(self) -> None:
        self.value: str | None = None

    def clear(self) -> None:
        self.value = None


class _CookieContext(FakeResponseContext):
    def __init__(self, outcome: FakeResponse | BaseException, jar: _FakeCookieJar) -> None:
        super().__init__(outcome)
        self._jar = jar

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await super().__aexit__(exc_type, exc, tb)
        self._jar.value = "response-issued-secret"


class CookieAwareClient(FakeClient):
    def __init__(self, outcomes: list[FakeResponse | BaseException]) -> None:
        super().__init__(outcomes)
        self.cookies = _FakeCookieJar()

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
    ) -> FakeResponseContext:
        effective_headers = dict(headers)
        if self.cookies.value is not None:
            effective_headers["Cookie"] = self.cookies.value
        self.requests.append((method, url, effective_headers, follow_redirects))
        context = _CookieContext(self.outcomes.pop(0), self.cookies)
        self.contexts.append(context)
        return context


class BlockingCloseClient(FakeClient):
    def __init__(self, *, fail_after_release: bool = False) -> None:
        super().__init__([])
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.fail_after_release = fail_after_release

    async def aclose(self) -> None:
        self.closed += 1
        self.close_started.set()
        await self.close_release.wait()
        if self.fail_after_release:
            raise RuntimeError("raw close failure")


class BlockingExitContext(FakeResponseContext):
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        super().__init__(outcome)
        self.exit_started = asyncio.Event()
        self.exit_release = asyncio.Event()
        self.exit_finished = asyncio.Event()

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.exits += 1
        self.exit_started.set()
        await self.exit_release.wait()
        self.exit_finished.set()


class BlockingExitClient(FakeClient):
    def __init__(self, outcome: FakeResponse | BaseException) -> None:
        super().__init__([outcome])
        self.context = BlockingExitContext(outcome)

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
    ) -> FakeResponseContext:
        self.requests.append((method, url, dict(headers), follow_redirects))
        self.outcomes.pop(0)
        self.contexts.append(self.context)
        return self.context


def _supervisor() -> CallSupervisor:
    return CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=2,
    )


async def _open_service(
    client: FakeClient,
    *,
    epoch: int = 1,
) -> tuple[AndroidAssetDownloadService, FakeBearer, CallSupervisor]:
    supervisor = _supervisor()
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(epoch)
    supervisor.start_accepting(epoch)
    bearer = FakeBearer()
    service = AndroidAssetDownloadService(
        bearer_provider=bearer,  # type: ignore[arg-type]
        supervisor=supervisor,
        client_factory=lambda: client,
    )
    await service.open(loop, epoch)
    return service, bearer, supervisor


def _png_response(*, chunks: list[bytes] | None = None) -> FakeResponse:
    return FakeResponse(
        200,
        headers={"content-type": "image/png"},
        chunks=chunks or [PNG[:5], PNG[5:]],
    )


def _assert_library_traceback_is_secret_free(
    error: ArtifactDownloadError,
    *,
    secrets: list[str],
    raw_objects: list[object],
) -> None:
    assert error.cause is None
    assert error.__cause__ is None
    assert error.__context__ is None

    inspected: list[str] = []
    leaked_secret_in: list[str] = []
    leaked_object_in: list[str] = []
    for frame, _line in traceback.walk_tb(error.__traceback__):
        # Normalise separators before matching: ``co_filename`` uses backslashes
        # on Windows, where a "/"-joined substring matched nothing and silently
        # scanned zero frames — disarming this whole assertion. ``PurePath``
        # does NOT help here, because it only treats "\\" as a separator when
        # the test itself runs on Windows.
        source_path = frame.f_code.co_filename.replace("\\", "/")
        if "/src/notebooklm/" not in source_path:
            continue
        inspected.append(frame.f_code.co_name)
        locals_text = repr(frame.f_locals)
        if any(secret in locals_text for secret in secrets):
            leaked_secret_in.append(frame.f_code.co_name)
        if any(raw in frame.f_locals.values() for raw in raw_objects):
            leaked_object_in.append(frame.f_code.co_name)

    # Without this the helper passes vacuously when the traceback shape changes
    # (or on a platform where the path match fails), which is exactly how the
    # Windows bug hid.
    assert inspected, "no notebooklm frame was inspected; the scan proved nothing"
    # Report frame NAMES, never ``repr(f_locals)`` — this module's frames hold
    # the capability URL and bearer, and a failure message must not print them.
    assert not leaked_secret_in, f"secret survived in library frames: {leaked_secret_in}"
    assert not leaked_object_in, f"raw object survived in library frames: {leaked_object_in}"


@pytest.mark.asyncio
async def test_direct_png_is_streamed_once_from_open_response(
    tmp_path: Path,
) -> None:
    response = _png_response()
    client = FakeClient([response])
    service, bearer, _ = await _open_service(client)
    destination = tmp_path / "direct.png"

    assert await service.download_url(INITIAL, str(destination)) == str(destination)

    assert destination.read_bytes() == PNG
    assert bearer.calls == [1]
    assert len(client.requests) == 1
    method, url, headers, follow_redirects = client.requests[0]
    assert method == "GET"
    assert url.endswith("capability=initial-secret&alr=yes")
    assert headers == {"Authorization": f"Bearer {BEARER}"}
    assert follow_redirects is False
    assert response.iterations == 1
    assert client.contexts[0].exits == 1
    assert client.closed == 1
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("representation", "media_type", "body"),
    [
        ("audio", "audio/mp4", MP4),
        ("audio", "audio/wav", WAV),
        ("video", "video/mp4", MP4),
        ("slide_pdf", "application/pdf", PDF),
        (
            "slide_pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            PPTX,
        ),
    ],
)
async def test_typed_representation_enforces_and_publishes_exact_format(
    tmp_path: Path,
    representation: str,
    media_type: str,
    body: bytes,
) -> None:
    response = FakeResponse(
        200,
        headers={"content-type": f"{media_type}; charset=binary"},
        chunks=[body[:3], body[3:7], body[7:]],
    )
    client = FakeClient([response])
    service, bearer, _ = await _open_service(client)
    destination = tmp_path / f"artifact-{representation}"

    result = await service.download_representation(
        INITIAL,
        str(destination),
        representation=representation,  # type: ignore[arg-type]
    )

    assert result == str(destination)
    assert destination.read_bytes() == body
    assert bearer.calls == [1]
    assert len(client.requests) == 1
    assert client.requests[0][2] == {"Authorization": f"Bearer {BEARER}"}
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
async def test_live_wav_audio_corrects_registry_m4a_suffix(tmp_path: Path) -> None:
    client = FakeClient([FakeResponse(200, headers={"content-type": "audio/wav"}, chunks=[WAV])])
    service, _, _ = await _open_service(client)
    requested = tmp_path / "audio.m4a"

    result = await service.download_representation(
        INITIAL,
        str(requested),
        representation="audio",
    )

    actual = tmp_path / "audio.wav"
    assert result == str(actual)
    assert actual.read_bytes() == WAV
    assert not requested.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("representation", "url", "media_type", "body"),
    [
        (
            "slide_pdf",
            "https://contribution.usercontent.google.com/download?cap=pdf-secret",
            "application/octet-stream",
            PDF,
        ),
        (
            "slide_pptx",
            "https://contribution.usercontent.google.com/download?cap=pptx-secret",
            "application/octet-stream",
            PPTX,
        ),
    ],
)
async def test_slide_capability_initial_uses_mobile_bearer_and_alr(
    tmp_path: Path,
    representation: str,
    url: str,
    media_type: str,
    body: bytes,
) -> None:
    client = FakeClient([FakeResponse(200, headers={"content-type": media_type}, chunks=[body])])
    service, bearer, _ = await _open_service(client)
    destination = tmp_path / f"artifact-{representation}"

    await service.download_representation(
        url,
        str(destination),
        representation=representation,  # type: ignore[arg-type]
    )

    assert destination.read_bytes() == body
    assert bearer.calls == [1]
    assert client.requests == [
        ("GET", f"{url}&alr=yes", {"Authorization": f"Bearer {BEARER}"}, False)
    ]


@pytest.mark.asyncio
async def test_media_representation_still_rejects_capability_initial_without_bearer(
    tmp_path: Path,
) -> None:
    url = "https://contribution.usercontent.google.com/download?cap=media-secret"
    client = FakeClient([])
    service, bearer, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError, match="code=url_policy"):
        await service.download_representation(
            url,
            str(tmp_path / "audio.mp4"),
            representation="audio",
        )

    assert bearer.calls == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_slide_capability_initial_is_limited_to_live_exact_host(tmp_path: Path) -> None:
    url = "https://rr5---sn-ab5sznzy.googlevideo.com/slides.pdf?cap=secret"
    client = FakeClient([])
    service, bearer, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError, match="code=url_policy"):
        await service.download_representation(
            url,
            str(tmp_path / "slides.pdf"),
            representation="slide_pdf",
        )

    assert bearer.calls == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_progressive_googlevideo_application_redirect_is_allowed_without_bearer(
    tmp_path: Path,
) -> None:
    progressive = "https://rr5---sn-ab5sznzy.googlevideo.com/media?cap=progressive-secret"
    client = FakeClient(
        [
            FakeResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[progressive.encode()],
            ),
            FakeResponse(200, headers={"content-type": "video/mp4"}, chunks=[MP4]),
        ]
    )
    service, bearer, _ = await _open_service(client)
    destination = tmp_path / "video.mp4"

    await service.download_representation(
        INITIAL,
        str(destination),
        representation="video",
    )

    assert destination.read_bytes() == MP4
    assert bearer.calls == [1]
    assert [request[1] for request in client.requests] == [f"{INITIAL}&alr=yes", progressive]
    assert client.requests[0][2] == {"Authorization": f"Bearer {BEARER}"}
    assert client.requests[1][2] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "redirect_url",
    [
        "https://evil%2f.googlevideo.com/media",
        "https://evil\\.googlevideo.com/media",
        "https://evil%00.usercontent.google.com/media",
    ],
)
async def test_malformed_android_media_host_is_rejected_before_dispatch(
    tmp_path: Path,
    redirect_url: str,
) -> None:
    client = FakeClient([FakeResponse(302, headers={"location": redirect_url})])
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError, match="code=url_policy"):
        await service.download_representation(
            INITIAL,
            str(tmp_path / "video.mp4"),
            representation="video",
        )

    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_response_cookies_are_cleared_before_every_redirect_hop(tmp_path: Path) -> None:
    client = CookieAwareClient(
        [
            FakeResponse(302, headers={"location": SIGNED}),
            _png_response(),
        ]
    )
    service, _, _ = await _open_service(client)

    await service.download_url(INITIAL, str(tmp_path / "cookie-free.png"))

    assert len(client.requests) == 2
    assert all("Cookie" not in request[2] for request in client.requests)


@pytest.mark.asyncio
async def test_bearer_is_not_reattached_after_chain_leaves_exact_origin(tmp_path: Path) -> None:
    returned = "https://lh3.googleusercontent.com/returned.png?capability=return-secret"
    client = FakeClient(
        [
            FakeResponse(302, headers={"location": SIGNED}),
            FakeResponse(302, headers={"location": returned}),
            _png_response(),
        ]
    )
    service, _, _ = await _open_service(client)

    await service.download_url(INITIAL, str(tmp_path / "returned.png"))

    assert client.requests[0][2] == {"Authorization": f"Bearer {BEARER}"}
    assert client.requests[1][2] == {}
    assert client.requests[2][2] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("representation", "media_type", "body", "code", "artifact_type"),
    [
        ("audio", "video/mp4", MP4, "content_type", "audio"),
        ("audio", "audio/mp4", WAV, "signature", "audio"),
        ("audio", "audio/wav", MP4, "signature", "audio"),
        ("audio", "audio/wav", b"RIFF\x00\x00\x00\x00NOPE", "signature", "audio"),
        ("video", "video/mp4", b"not-an-mp4", "signature", "video"),
        ("slide_pdf", "application/pdf", b"not-a-pdf", "signature", "slide_deck"),
        (
            "slide_pdf",
            "application/octet-stream",
            b"not-a-pdf",
            "signature",
            "slide_deck",
        ),
        (
            "slide_pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            b"not-a-zip",
            "signature",
            "slide_deck",
        ),
        (
            "slide_pptx",
            "application/octet-stream",
            b"not-a-zip",
            "signature",
            "slide_deck",
        ),
    ],
)
async def test_typed_representation_rejects_mime_or_signature_without_publication(
    tmp_path: Path,
    representation: str,
    media_type: str,
    body: bytes,
    code: str,
    artifact_type: str,
) -> None:
    destination = tmp_path / "existing.bin"
    destination.write_bytes(b"existing")
    client = FakeClient([FakeResponse(200, headers={"content-type": media_type}, chunks=[body])])
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_representation(
            INITIAL,
            str(destination),
            representation=representation,  # type: ignore[arg-type]
        )

    assert raised.value.artifact_type == artifact_type
    assert f"code={code}" in str(raised.value)
    assert destination.read_bytes() == b"existing"
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("declared_length", ["invalid", "11"])
async def test_declared_size_limit_rejects_before_staging(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    declared_length: str,
) -> None:
    policy = assets_module._REPRESENTATION_POLICIES["infographic"]
    monkeypatch.setitem(
        assets_module._REPRESENTATION_POLICIES,
        "infographic",
        assets_module._RepresentationPolicy(
            artifact_type=policy.artifact_type,
            formats=policy.formats,
            max_bytes=10,
        ),
    )
    response = FakeResponse(
        200,
        headers={"content-type": "image/png", "content-length": declared_length},
        chunks=[PNG],
    )
    client = FakeClient([response])
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError, match="code=size"):
        await service.download_url(INITIAL, str(tmp_path / "oversized.png"))

    assert response.iterations == 0
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
async def test_streamed_size_limit_rejects_before_overflow_write(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = assets_module._REPRESENTATION_POLICIES["infographic"]
    monkeypatch.setitem(
        assets_module._REPRESENTATION_POLICIES,
        "infographic",
        assets_module._RepresentationPolicy(
            artifact_type=policy.artifact_type,
            formats=policy.formats,
            max_bytes=len(PNG) - 1,
        ),
    )
    client = FakeClient([_png_response(chunks=[PNG[:8], PNG[8:]])])
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError, match="code=size"):
        await service.download_url(INITIAL, str(tmp_path / "oversized.png"))

    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
async def test_unknown_representation_rejects_before_credentials_or_dispatch(
    tmp_path: Path,
) -> None:
    client = FakeClient([])
    service, bearer, _ = await _open_service(client)

    with pytest.raises(ValueError, match="Unsupported Android artifact representation"):
        await service.download_representation(
            INITIAL,
            str(tmp_path / "out.bin"),
            representation="unknown",  # type: ignore[arg-type]
        )

    assert bearer.calls == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_http_redirect_revalidates_and_drops_bearer_on_signed_gcs(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [
            FakeResponse(302, headers={"location": SIGNED}),
            _png_response(),
        ]
    )
    service, _, _ = await _open_service(client)

    await service.download_url(INITIAL, str(tmp_path / "redirect.png"))

    assert [request[1] for request in client.requests] == [
        f"{INITIAL}&alr=yes",
        SIGNED,
    ]
    assert client.requests[0][2] == {"Authorization": f"Bearer {BEARER}"}
    assert client.requests[1][2] == {}
    assert all(context.exits == 1 for context in client.contexts)


@pytest.mark.asyncio
async def test_relative_redirect_keeps_exact_bearer_origin_and_initial_only_alr(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [
            FakeResponse(307, headers={"location": "/next.png?cap=relative-secret"}),
            _png_response(),
        ]
    )
    service, _, _ = await _open_service(client)

    await service.download_url(INITIAL, str(tmp_path / "relative.png"))

    assert [request[1] for request in client.requests] == [
        f"{INITIAL}&alr=yes",
        "https://lh3.googleusercontent.com/next.png?cap=relative-secret",
    ]
    assert all(request[2] == {"Authorization": f"Bearer {BEARER}"} for request in client.requests)
    assert "alr=" not in client.requests[1][1]


@pytest.mark.asyncio
async def test_application_redirect_is_bounded_and_final_capability_is_not_reopened(
    tmp_path: Path,
) -> None:
    redirect = FakeResponse(
        200,
        headers={"content-type": "text/plain; charset=utf-8"},
        chunks=[SIGNED.encode()],
    )
    png = _png_response()
    client = FakeClient([redirect, png])
    service, _, _ = await _open_service(client)

    await service.download_url(INITIAL, str(tmp_path / "application.png"))

    assert [request[1] for request in client.requests] == [f"{INITIAL}&alr=yes", SIGNED]
    assert redirect.iterations == 1
    assert png.iterations == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "url",
    [
        "http://lh3.googleusercontent.com/image.png",
        "https://user@lh3.googleusercontent.com/image.png",
        "https://lh3.googleusercontent.com:444/image.png",
        "https://lh3.googleusercontent.com/image.png#fragment",
        "https://evil.example/image.png",
        SIGNED,
        "https://lh3.googleusercontent.com/image.png\nX-Injected: value",
        "https://lh3.googleusercontent.com/image.png?alr=no",
        "https://lh3.googleusercontent.com/image.png?alr=yes&alr=yes",
    ],
)
async def test_invalid_initial_url_rejects_before_credentials_and_dispatch(
    tmp_path: Path,
    url: str,
) -> None:
    client = FakeClient([])
    service, bearer, _ = await _open_service(client)
    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(url, str(tmp_path / "out.png"))
    assert raised.value.details is not None and "code=url_policy" in raised.value.details
    assert bearer.calls == []
    assert client.requests == []


@pytest.mark.asyncio
async def test_untrusted_redirect_rejects_before_next_dispatch(tmp_path: Path) -> None:
    client = FakeClient([FakeResponse(302, headers={"location": "https://evil.example/secret"})])
    service, _, _ = await _open_service(client)
    with pytest.raises(ArtifactDownloadError, match="url_policy") as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))
    assert len(client.requests) == 1
    assert "evil.example" not in str(raised.value)
    assert "host=<rejected>" in str(raised.value)


@pytest.mark.asyncio
async def test_hop_limit_is_bounded_at_twenty_redirects(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [
            FakeResponse(
                302,
                headers={"location": f"https://google.com/hop-{hop}?cap=hop-secret-{hop}"},
            )
            for hop in range(21)
        ]
    )
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError, match="too_many_hops"):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert len(client.requests) == 21


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "code"),
    [
        (FakeResponse(302), "redirect"),
        (
            FakeResponse(
                302,
                headers=[("location", SIGNED), ("location", "https://google.com/other")],
            ),
            "redirect",
        ),
        (
            FakeResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"x" * 8_193],
            ),
            "application_redirect_size",
        ),
        (
            FakeResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"\xff"],
            ),
            "application_redirect_encoding",
        ),
        (
            FakeResponse(
                200,
                headers={"content-type": "text/plain"},
                chunks=[b"https://google.com/a\nb"],
            ),
            "application_redirect",
        ),
        (FakeResponse(200, headers={"content-type": "text/html"}), "content_type"),
        (FakeResponse(200, headers={"content-type": "image/png"}), "empty"),
        (
            FakeResponse(
                200,
                headers={"content-type": "image/png"},
                chunks=[b"not a png"],
            ),
            "signature",
        ),
    ],
)
async def test_bounded_response_failures_preserve_existing_destination(
    tmp_path: Path,
    response: FakeResponse,
    code: str,
) -> None:
    destination = tmp_path / "existing.png"
    destination.write_bytes(b"existing")
    client = FakeClient([response])
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(INITIAL, str(destination))

    assert f"code={code}" in str(raised.value)
    assert destination.read_bytes() == b"existing"
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
async def test_401_invalidates_for_next_caller_without_replay(tmp_path: Path) -> None:
    client = FakeClient([FakeResponse(401)])
    service, bearer, _ = await _open_service(client)
    with pytest.raises(AuthError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))
    assert "notebooklm login" in str(raised.value)
    assert bearer.invalidations == [17]
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_signed_gcs_401_does_not_invalidate_lh3_bearer(tmp_path: Path) -> None:
    client = FakeClient(
        [
            FakeResponse(302, headers={"location": SIGNED}),
            FakeResponse(401),
        ]
    )
    service, bearer, _ = await _open_service(client)
    with pytest.raises(AuthError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))
    assert "notebooklm login" in str(raised.value)
    assert bearer.invalidations == []
    assert len(client.requests) == 2


@pytest.mark.asyncio
async def test_raw_transport_failure_becomes_secret_free_bounded_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = httpx.ConnectError(f"raw failure {INITIAL} {BEARER}")
    client = FakeClient([raw])
    service, _, _ = await _open_service(client)

    with caplog.at_level(logging.DEBUG), pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    error = raised.value
    assert "code=transport" in str(error)
    rendered = "\n".join([str(error), repr(error), caplog.text])
    assert INITIAL not in rendered
    assert "initial-secret" not in rendered
    assert BEARER not in rendered
    assert raw not in error.args

    _assert_library_traceback_is_secret_free(
        error,
        secrets=[INITIAL, "initial-secret", BEARER],
        raw_objects=[raw, service, client],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("redirect_kind", ["location", "application"])
async def test_post_redirect_failures_drop_all_capabilities_and_raw_objects(
    tmp_path: Path,
    redirect_kind: str,
) -> None:
    next_url = (
        "https://storage.googleapis.com/bucket/final.png?X-Goog-Signature=post-redirect-secret"
    )
    first = (
        FakeResponse(302, headers={"location": next_url})
        if redirect_kind == "location"
        else FakeResponse(
            200,
            headers={"content-type": "text/plain"},
            chunks=[next_url.encode()],
        )
    )
    raw = httpx.ReadError(f"stream failed {next_url} {BEARER}")
    final = FakeResponse(
        200,
        headers={"content-type": "image/png"},
        chunks=[PNG],
        error=raw,
    )
    client = FakeClient([first, final])
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    error = raised.value
    assert "code=transport" in str(error)
    rendered = repr(error)
    assert "post-redirect-secret" not in rendered
    assert BEARER not in rendered
    _assert_library_traceback_is_secret_free(
        error,
        secrets=[INITIAL, "initial-secret", next_url, "post-redirect-secret", BEARER],
        raw_objects=[raw, first, final, client, service],
    )


@pytest.mark.asyncio
async def test_client_factory_failure_does_not_publish_raw_exception(tmp_path: Path) -> None:
    supervisor = _supervisor()
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    bearer = FakeBearer()
    raw = httpx.ConnectError(f"factory leaked {INITIAL}")

    def fail_factory() -> Any:
        raise raw

    service = AndroidAssetDownloadService(
        bearer_provider=bearer,  # type: ignore[arg-type]
        supervisor=supervisor,
        client_factory=fail_factory,
    )
    await service.open(loop, 1)

    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))
    assert "code=transport" in str(raised.value)
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert raw not in raised.value.args


@pytest.mark.asyncio
async def test_cancellation_cleans_staging_and_forced_close_cancels_transfer(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    response = FakeResponse(
        200,
        headers={"content-type": "image/png"},
        chunks=[PNG],
        gate=gate,
        wait_again_after_cancel=True,
    )
    client = FakeClient([response], close_gate=gate)
    service, _, _ = await _open_service(client)
    task = asyncio.create_task(service.download_url(INITIAL, str(tmp_path / "out.png")))
    while not client.requests:
        await asyncio.sleep(0)

    task_repr = repr(task)
    assert INITIAL not in task_repr
    assert "initial-secret" not in task_repr
    assert BEARER not in task_repr

    await service.prepare_close()
    await service.close_resources()
    assert task.done()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not (tmp_path / "out.png").exists()
    assert list(tmp_path.glob(".*.part")) == []
    assert client.closed >= 1


@pytest.mark.asyncio
async def test_forced_close_settles_old_task_before_reopen_and_new_epoch_dispatch(
    tmp_path: Path,
) -> None:
    gate = asyncio.Event()
    old_response = FakeResponse(
        200,
        headers={"content-type": "image/png"},
        chunks=[PNG],
        gate=gate,
        wait_again_after_cancel=True,
    )
    old_client = FakeClient([old_response], close_gate=gate)
    new_client = FakeClient([_png_response()])
    clients = iter([old_client, new_client])
    supervisor = _supervisor()
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    bearer = FakeBearer()
    service = AndroidAssetDownloadService(
        bearer_provider=bearer,  # type: ignore[arg-type]
        supervisor=supervisor,
        client_factory=lambda: next(clients),
    )
    await service.open(loop, 1)

    old_task = asyncio.create_task(service.download_url(INITIAL, str(tmp_path / "old.png")))
    while not old_client.requests:
        await asyncio.sleep(0)
    await service.prepare_close()
    await service.close_resources()

    assert old_task.done()
    with pytest.raises(asyncio.CancelledError):
        await old_task
    assert service._tasks == set()
    assert service._clients == set()

    await supervisor.begin_closing(1)
    supervisor.mark_closed(1)
    supervisor.reset_after_open()
    supervisor.prepare_generation(2)
    supervisor.start_accepting(2)
    await service.open(loop, 2)
    await service.download_url(INITIAL, str(tmp_path / "new.png"))

    assert bearer.calls == [1, 2]
    assert len(old_client.requests) == 1
    assert len(new_client.requests) == 1
    assert (tmp_path / "new.png").read_bytes() == PNG


@pytest.mark.asyncio
async def test_reopen_epoch_fences_old_service_generation_before_dispatch(tmp_path: Path) -> None:
    client = FakeClient([_png_response()])
    service, bearer, supervisor = await _open_service(client, epoch=1)
    await service.prepare_close()
    await service.close_resources()
    await supervisor.begin_closing(1)
    supervisor.mark_closed(1)

    loop = asyncio.get_running_loop()
    supervisor.reset_after_open()
    supervisor.prepare_generation(2)
    supervisor.start_accepting(2)
    await service.open(loop, 2)
    await service.download_url(INITIAL, str(tmp_path / "new.png"))

    assert bearer.calls == [2]
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_batch_download_uses_the_neutral_transfer_plane(tmp_path: Path) -> None:
    client = FakeClient([_png_response()])
    service, _, _ = await _open_service(client)
    output = tmp_path / "out.png"

    result = await service.download_urls_batch([(INITIAL, str(output))])

    assert result.succeeded == [str(output)]
    assert result.failed == []
    assert output.read_bytes() == PNG
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_asset_owner_is_a_phased_transport_lifecycle_participant() -> None:
    client = FakeClient([])
    service, _, _ = await _open_service(client)
    assert isinstance(service, TransportLifecycle)
    assert service.name == "android-assets"
    await service.prepare_close()
    await service.close_resources()


@pytest.mark.asyncio
async def test_close_cancellation_waits_for_cleanup_and_precedes_normal_close_failure() -> None:
    client = BlockingCloseClient(fail_after_release=True)
    service, _, _ = await _open_service(client)
    service._clients.add(client)
    await service.prepare_close()

    close_task = asyncio.create_task(service.close_resources())
    await client.close_started.wait()
    close_task.cancel()
    await asyncio.sleep(0)
    assert not close_task.done()
    client.close_release.set()

    with pytest.raises(asyncio.CancelledError):
        await close_task
    assert service._clients == set()
    assert service._tasks == set()


class ExitFailingContext(FakeResponseContext):
    """A response context whose ``__aexit__`` fails after the body is consumed."""

    def __init__(self, outcome: FakeResponse | BaseException, error: BaseException) -> None:
        super().__init__(outcome)
        self.error = error

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.exits += 1
        raise self.error


class ExitFailingClient(FakeClient):
    def __init__(self, outcomes: list[FakeResponse | BaseException], *, error: BaseException):
        super().__init__(outcomes)
        self.error = error

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
    ) -> FakeResponseContext:
        self.requests.append((method, url, dict(headers), follow_redirects))
        context = ExitFailingContext(self.outcomes.pop(0), self.error)
        self.contexts.append(context)
        return context


class StreamRefusingClient(FakeClient):
    """``stream`` raises before returning a context manager to enter."""

    def __init__(self, error: BaseException) -> None:
        super().__init__([])
        self.error = error

    def stream(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str],
        follow_redirects: bool,
    ) -> FakeResponseContext:
        self.requests.append((method, url, dict(headers), follow_redirects))
        raise self.error


class CloseFailingClient(FakeClient):
    def __init__(self, outcomes: list[FakeResponse | BaseException], *, error: BaseException):
        super().__init__(outcomes)
        self.error = error

    async def aclose(self) -> None:
        self.closed += 1
        raise self.error


async def _unopened_service(client: Any) -> AndroidAssetDownloadService:
    """A service whose ``open`` was never called, as after a failed startup.

    The supervisor behind it *is* accepting, so the service's own
    ``_active_epoch`` guard is the only thing standing in front of a transfer.
    """

    supervisor = _supervisor()
    supervisor.set_bound_loop(asyncio.get_running_loop())
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    return AndroidAssetDownloadService(
        bearer_provider=FakeBearer(),  # type: ignore[arg-type]
        supervisor=supervisor,
        client_factory=lambda: client,
    )


# ---------------------------------------------------------------------------
# lifecycle fencing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_transfer_before_open_is_refused_instead_of_dispatching(tmp_path: Path) -> None:
    """``_active_epoch`` is the only proof a resource generation exists.

    Dispatching without one would build a client the close sweep never learns
    about, leaking a live connection past ``close_resources``.
    """
    client = FakeClient([_png_response()])
    service = await _unopened_service(client)

    with pytest.raises(RuntimeError, match="Client not initialized"):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert client.requests == []
    assert service._clients == set()


@pytest.mark.asyncio
async def test_a_transfer_with_no_owning_task_is_refused_before_dispatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forced close cancels ``self._tasks``; an unregistered transfer is unstoppable.

    Rather than run a transfer the close sweep cannot reach, the service
    refuses it.
    """
    client = FakeClient([_png_response()])
    service, bearer, _ = await _open_service(client)
    monkeypatch.setattr(assets_module.asyncio, "current_task", lambda: None)

    with pytest.raises(RuntimeError, match="no owning task"):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert client.requests == []
    assert bearer.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("closing", "active_epoch", "expected"),
    [
        pytest.param(True, 1, 1, id="closing-fence"),
        pytest.param(False, 2, 1, id="retired-generation"),
        pytest.param(False, None, 1, id="already-closed"),
    ],
)
async def test_a_retired_or_closing_generation_fails_the_epoch_fence(
    closing: bool,
    active_epoch: int | None,
    expected: int,
) -> None:
    """This fence sits between every hop, so it must reject on either condition.

    A transfer that survived it would keep writing into a destination the next
    client generation already believes it owns.
    """
    service, _, _ = await _open_service(FakeClient([]))
    service._closing = closing
    service._active_epoch = active_epoch

    with pytest.raises(RuntimeError, match="retired resource generation") as raised:
        service.assert_epoch(expected)

    assert f"expected={expected}" in str(raised.value)
    assert f"active={active_epoch}" in str(raised.value)


@pytest.mark.asyncio
async def test_retirement_during_bearer_await_preserves_the_lifecycle_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A post-await epoch failure must not be relabeled as a transport error."""
    client = FakeClient([_png_response()])
    service, bearer, _ = await _open_service(client)

    async def _retire_while_minting(expected_epoch: int) -> BearerCredential:
        bearer.calls.append(expected_epoch)
        await asyncio.sleep(0)
        service._active_epoch = expected_epoch + 1
        return BearerCredential(BEARER, generation=17)

    monkeypatch.setattr(bearer, "get", _retire_while_minting)

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert bearer.calls == [1]
    assert client.requests == []


@pytest.mark.asyncio
async def test_retirement_after_streaming_preserves_the_lifecycle_failure(
    tmp_path: Path,
) -> None:
    """The pre-publish fence must not be relabeled as a transport error."""
    response = _png_response(chunks=[PNG])
    client = FakeClient([response])
    service, _, _ = await _open_service(client)
    destination = tmp_path / "out.png"

    async def _retire_after_body():
        yield PNG
        service._active_epoch = 2

    response.aiter_bytes = _retire_after_body  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="retired resource generation"):
        await service.download_url(INITIAL, str(destination))

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []
    assert client.contexts[0].exits == 1


def test_android_downloads_disable_exception_cause_chaining_at_construction() -> None:
    """Neither Android single nor batch transfers should build a URL-bearing cause."""
    service = AndroidAssetDownloadService(
        bearer_provider=FakeBearer(),  # type: ignore[arg-type]
        supervisor=_supervisor(),
        client_factory=lambda: FakeClient([]),
    )

    assert service._chain is False


@pytest.mark.asyncio
async def test_the_matching_open_generation_passes_the_epoch_fence() -> None:
    """Guards the test above from passing because the fence rejects everything."""
    service, _, _ = await _open_service(FakeClient([]), epoch=4)

    assert service.assert_epoch(4) is None


@pytest.mark.asyncio
async def test_closing_a_service_that_never_opened_is_a_no_op() -> None:
    """Client startup can fail between constructing the service and ``open``.

    The lifecycle still runs both close phases on it, and neither may trip the
    bound-loop assertion on a service that has no loop yet.
    """
    service = await _unopened_service(FakeClient([]))

    await service.prepare_close()
    await service.close_resources()

    assert service._closing is True
    assert service._bound_loop is None


@pytest.mark.asyncio
async def test_prepare_close_never_cancels_the_task_that_called_it() -> None:
    """A transfer may close the client it is running under.

    Cancelling the caller here would turn an orderly close into a
    ``CancelledError`` raised inside the very transfer requesting it.
    """
    service, _, _ = await _open_service(FakeClient([]))

    async def _never() -> None:
        await asyncio.Event().wait()

    bystander = asyncio.create_task(_never())
    service._tasks.add(bystander)

    async def _closes_from_inside() -> str:
        service._tasks.add(asyncio.current_task())  # type: ignore[arg-type]
        await service.prepare_close()
        return "survived"

    assert await asyncio.create_task(_closes_from_inside()) == "survived"

    # ``Task.cancelling()`` is 3.11+, and ``cancelled()`` is still False right
    # after ``cancel()`` because the task has not been resumed yet. Awaiting is
    # version-agnostic AND stronger: the previous form cancelled the bystander
    # itself first, so it passed even if ``prepare_close`` had cancelled nothing.
    # If the cancel never arrived, ``_never()`` waits forever and the timeout
    # fails the test rather than hanging it.
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(bystander, timeout=5)


@pytest.mark.asyncio
async def test_a_close_failure_alone_is_reported_as_a_close_failure() -> None:
    """Distinct from the cancellation case: nothing cancels this close.

    Swallowing it would let a transport that never released its socket look
    like a clean shutdown.
    """
    client = CloseFailingClient([], error=OSError("socket already gone"))
    service, _, _ = await _open_service(client)
    service._clients.add(client)
    await service.prepare_close()

    with pytest.raises(RuntimeError, match="Android asset transport close failed"):
        await service.close_resources()

    assert client.closed == 1
    assert service._clients == set()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_an_interpreter_exit_during_close_outranks_a_close_failure(
    error: BaseException,
) -> None:
    """Ctrl-C during shutdown must reach the interpreter, not become a RuntimeError."""
    interrupting = CloseFailingClient([], error=error)
    also_failing = CloseFailingClient([], error=OSError("socket already gone"))
    service, _, _ = await _open_service(interrupting)
    service._clients.update({interrupting, also_failing})
    await service.prepare_close()

    with pytest.raises(type(error)):
        await service.close_resources()

    assert service._clients == set()
    assert service._tasks == set()


@pytest.mark.asyncio
async def test_a_second_close_cancellation_does_not_displace_the_first() -> None:
    """Repeated Ctrl-C must not restart the wait or relabel the cancellation.

    The sweep is shielded, so every extra cancel lands while it is still
    running; only the first is kept and republished once it settles.
    """
    client = BlockingCloseClient()
    service, _, _ = await _open_service(client)
    service._clients.add(client)
    await service.prepare_close()

    close_task = asyncio.create_task(service.close_resources())
    await client.close_started.wait()
    close_task.cancel("first")
    await asyncio.sleep(0)
    close_task.cancel("second")
    await asyncio.sleep(0)
    assert not close_task.done(), "the shielded sweep still runs"

    client.close_release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await close_task

    # Python 3.10 drops the optional cancellation message when a cancel is
    # caught and republished through shielded cleanup; 3.11+ preserves it.
    # Neither may REPLACE it with the later message, which is the contract.
    assert raised.value.args == (("first",) if sys.version_info >= (3, 11) else ())
    assert raised.value.args != ("second",)
    assert client.closed == 1


@pytest.mark.asyncio
async def test_transfer_cancellation_waits_for_response_exit_and_preserves_first_request(
    tmp_path: Path,
) -> None:
    client = BlockingExitClient(_png_response())
    service, _, _ = await _open_service(client)
    destination = tmp_path / "published-before-response-exit.png"

    transfer = asyncio.create_task(service.download_url(INITIAL, str(destination)))
    await client.context.exit_started.wait()
    assert destination.read_bytes() == PNG, "publication precedes advisory response teardown"

    transfer.cancel("first")
    await asyncio.sleep(0)
    transfer.cancel("second")
    await asyncio.sleep(0)
    assert not transfer.done(), "response teardown remains strongly retained"

    client.context.exit_release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await transfer

    assert raised.value.args == (("first",) if sys.version_info >= (3, 11) else ())
    assert raised.value.args != ("second",)
    assert client.context.exit_finished.is_set()
    assert client.context.exits == 1
    assert client.closed == 1
    assert service._clients == set()
    assert service._tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
async def test_transfer_cancellation_waits_for_client_close_and_preserves_first_request(
    tmp_path: Path,
    batch: bool,
) -> None:
    client = BlockingCloseClient()
    client.outcomes.append(_png_response())
    service, _, _ = await _open_service(client)
    destination = tmp_path / f"published-before-{'batch' if batch else 'single'}-close.png"

    if batch:
        transfer = asyncio.create_task(service.download_urls_batch([(INITIAL, str(destination))]))
    else:
        transfer = asyncio.create_task(service.download_url(INITIAL, str(destination)))
    await client.close_started.wait()
    assert destination.read_bytes() == PNG, "publication precedes advisory client teardown"

    transfer.cancel("first")
    await asyncio.sleep(0)
    transfer.cancel("second")
    await asyncio.sleep(0)
    assert not transfer.done(), "client teardown remains strongly retained"

    client.close_release.set()
    with pytest.raises(asyncio.CancelledError) as raised:
        await transfer

    assert raised.value.args == (("first",) if sys.version_info >= (3, 11) else ())
    assert raised.value.args != ("second",)
    assert client.closed == 1
    assert service._clients == set()
    assert service._tasks == set()


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["response", "client"])
async def test_cleanup_originated_cancellation_is_not_advisory(
    tmp_path: Path,
    origin: str,
) -> None:
    cancellation = asyncio.CancelledError(f"{origin} cleanup cancelled")
    if origin == "response":
        client: FakeClient = ExitFailingClient([_png_response()], error=cancellation)
    else:
        client = CloseFailingClient([_png_response()], error=cancellation)
    service, _, _ = await _open_service(client)
    destination = tmp_path / f"published-before-{origin}-cancellation.png"

    with pytest.raises(asyncio.CancelledError) as raised:
        await service.download_url(INITIAL, str(destination))

    assert raised.value is cancellation
    assert destination.read_bytes() == PNG
    assert client.closed == 1
    assert service._clients == set()
    assert service._tasks == set()


# ---------------------------------------------------------------------------
# transfer-worker failure arms
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_an_interpreter_exit_from_the_client_factory_is_not_a_transport_failure(
    tmp_path: Path,
    error: BaseException,
) -> None:
    """Ctrl-C while building the transport must not be reported as a bad URL/host."""

    def interrupted_factory() -> Any:
        raise error

    supervisor = _supervisor()
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    service = AndroidAssetDownloadService(
        bearer_provider=FakeBearer(),  # type: ignore[arg-type]
        supervisor=supervisor,
        client_factory=interrupted_factory,
    )
    await service.open(loop, 1)

    with pytest.raises(type(error)):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_an_interpreter_exit_mid_stream_is_not_downgraded_to_a_bounded_failure(
    tmp_path: Path,
    error: BaseException,
) -> None:
    """The catch-all below it turns everything into ``code=transport``.

    Ctrl-C has to be re-raised ahead of it, and the partial file still removed.
    """
    response = FakeResponse(200, headers={"content-type": "image/png"}, chunks=[PNG], error=error)
    client = FakeClient([response])
    service, _, _ = await _open_service(client)
    destination = tmp_path / "interrupted.png"

    with pytest.raises(type(error)):
        await service.download_url(INITIAL, str(destination))

    assert not destination.exists()
    assert list(tmp_path.glob(".*.part")) == []


@pytest.mark.asyncio
async def test_a_stream_that_never_opens_still_closes_the_hop_cleanly(tmp_path: Path) -> None:
    """``stream`` can raise before yielding a context there is anything to exit.

    The ``finally`` must skip ``__aexit__`` rather than call it on ``None``,
    which would replace the bounded failure with an ``AttributeError``.
    """
    client = StreamRefusingClient(httpx.ConnectError(f"refused {INITIAL} {BEARER}"))
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert "code=transport" in str(raised.value)
    assert "host=lh3.googleusercontent.com" in str(raised.value)
    assert len(client.requests) == 1
    assert client.closed == 1


@pytest.mark.asyncio
async def test_a_failing_response_close_does_not_fail_a_completed_transfer(
    tmp_path: Path,
) -> None:
    """The bytes are already fsynced and renamed; the socket teardown is advisory."""
    client = ExitFailingClient([_png_response()], error=httpx.ReadError("close after body"))
    service, _, _ = await _open_service(client)
    destination = tmp_path / "exit-failure.png"

    assert await service.download_url(INITIAL, str(destination)) == str(destination)

    assert destination.read_bytes() == PNG
    assert client.contexts[0].exits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_an_interpreter_exit_closing_the_response_is_not_swallowed(
    tmp_path: Path,
    error: BaseException,
) -> None:
    """The arm beside it discards every other ``__aexit__`` failure."""
    client = ExitFailingClient([_png_response()], error=error)
    service, _, _ = await _open_service(client)
    destination = tmp_path / "published.png"

    with pytest.raises(type(error)):
        await service.download_url(INITIAL, str(destination))

    assert destination.read_bytes() == PNG, "the file was already published"


@pytest.mark.asyncio
async def test_a_failing_client_close_does_not_fail_a_completed_transfer(
    tmp_path: Path,
) -> None:
    """A transport that cannot close still leaves the caller a valid file."""
    client = CloseFailingClient([_png_response()], error=OSError("socket already gone"))
    service, _, _ = await _open_service(client)
    destination = tmp_path / "close-failure.png"

    assert await service.download_url(INITIAL, str(destination)) == str(destination)

    assert destination.read_bytes() == PNG
    assert client.closed == 1
    assert service._clients == set(), "the failed client is still untracked"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error", [KeyboardInterrupt(), SystemExit()], ids=["keyboard-interrupt", "system-exit"]
)
async def test_an_interpreter_exit_closing_the_client_is_not_swallowed(
    tmp_path: Path,
    error: BaseException,
) -> None:
    """Ctrl-C during the per-transfer client close must reach the interpreter."""
    client = CloseFailingClient([_png_response()], error=error)
    service, _, _ = await _open_service(client)

    with pytest.raises(type(error)):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert client.closed == 1


@pytest.mark.asyncio
async def test_an_undeletable_partial_file_does_not_replace_the_real_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cleanup of the ``.part`` file is best effort.

    Letting its ``OSError`` escape would hide ``code=signature`` -- the reason
    the transfer actually failed -- behind an unactionable filesystem error.
    """

    def _refuse_unlink(self: Path, **_kwargs: Any) -> None:
        raise OSError("read-only filesystem")

    client = FakeClient(
        [FakeResponse(200, headers={"content-type": "image/png"}, chunks=[b"not a png"])]
    )
    service, _, _ = await _open_service(client)
    from notebooklm._artifact import _guarded_transfer

    monkeypatch.setattr(_guarded_transfer.Path, "unlink", _refuse_unlink)

    with pytest.raises(ArtifactDownloadError, match="code=signature"):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert list(tmp_path.glob(".*.part")) != [], "the undeletable partial really did survive"


# ---------------------------------------------------------------------------
# streaming and bearer edge cases
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_chunks_mid_stream_do_not_end_the_transfer(tmp_path: Path) -> None:
    """A keep-alive frame arrives as a zero-length chunk.

    Treating it as the end of the body -- or as an ``empty`` failure -- would
    truncate a perfectly good download.
    """
    response = FakeResponse(
        200,
        headers={"content-type": "image/png"},
        chunks=[b"", PNG[:4], b"", PNG[4:], b""],
    )
    client = FakeClient([response])
    service, _, _ = await _open_service(client)
    destination = tmp_path / "keepalive.png"

    assert await service.download_url(INITIAL, str(destination)) == str(destination)

    assert destination.read_bytes() == PNG


@pytest.mark.asyncio
async def test_a_capability_host_outside_the_bearer_set_is_never_sent_a_bearer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The bearer set and the capability-entry set are deliberately separate.

    Today they coincide, so this arm has no production caller; the moment a
    capability host is added that Google does not accept a bearer for, sending
    one there would leak the credential to that host.
    """
    policy = assets_module._REPRESENTATION_POLICIES["infographic"]
    progressive_host = "rr5---sn-ab5sznzy.googlevideo.com"
    monkeypatch.setitem(
        assets_module._REPRESENTATION_POLICIES,
        "infographic",
        assets_module._RepresentationPolicy(
            artifact_type=policy.artifact_type,
            formats=policy.formats,
            max_bytes=policy.max_bytes,
            capability_initial_hosts=frozenset({progressive_host}),
        ),
    )
    url = f"https://{progressive_host}/image.png?cap=progressive-secret"
    client = FakeClient([_png_response()])
    service, bearer, _ = await _open_service(client)
    destination = tmp_path / "no-bearer.png"

    assert await service.download_url(url, str(destination)) == str(destination)

    assert bearer.calls == [], "no credential is even minted for a non-bearer host"
    assert client.requests[0][2] == {}
    assert client.requests[0][1] == f"{url}&alr=yes"
