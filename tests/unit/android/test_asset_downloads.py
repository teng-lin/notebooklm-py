"""Response-aware and secret-safe Android artifact transfer tests."""

from __future__ import annotations

import asyncio
import logging
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
from notebooklm._transport_drain import TransportDrainTracker
from notebooklm.exceptions import ArtifactDownloadError, UnsupportedOperationError

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


def _supervisor() -> CallSupervisor:
    return CallSupervisor(
        metrics=ClientMetrics(),
        drain_tracker=TransportDrainTracker(),
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
    for frame, _line in traceback.walk_tb(error.__traceback__):
        if "/src/notebooklm/" not in frame.f_code.co_filename:
            continue
        frame_text = repr(frame.f_locals)
        for secret in secrets:
            assert secret not in frame_text
        for raw in raw_objects:
            assert raw not in frame.f_locals.values()


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
async def test_hop_limit_is_bounded_and_never_dispatches_a_tenth_request(
    tmp_path: Path,
) -> None:
    client = FakeClient(
        [
            FakeResponse(
                302,
                headers={"location": f"https://google.com/hop-{hop}?cap=hop-secret-{hop}"},
            )
            for hop in range(9)
        ]
    )
    service, _, _ = await _open_service(client)

    with pytest.raises(ArtifactDownloadError, match="too_many_hops"):
        await service.download_url(INITIAL, str(tmp_path / "out.png"))

    assert len(client.requests) == 9


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
    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))
    assert raised.value.status_code == 401
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
    with pytest.raises(ArtifactDownloadError) as raised:
        await service.download_url(INITIAL, str(tmp_path / "out.png"))
    assert raised.value.status_code == 401
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
async def test_batch_seam_is_explicitly_unsupported() -> None:
    client = FakeClient([])
    service, _, _ = await _open_service(client)
    with pytest.raises(UnsupportedOperationError):
        await service.download_urls_batch([(INITIAL, "out.png")])
    assert client.requests == []


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
