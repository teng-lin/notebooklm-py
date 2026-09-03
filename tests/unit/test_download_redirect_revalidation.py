"""Per-redirect-hop host/scheme revalidation for artifact downloads (#1521).

Both download clients (``download_url`` single + ``download_urls_batch``)
use ``follow_redirects=True``. The initial host+scheme allowlist gate only
checks the URL the caller passed, so a *trusted* Google URL whose
``Location`` points off-allowlist — a non-HTTPS hop, or a private/link-local
host such as ``169.254.169.254`` — would otherwise be followed and its body
written to ``output_path``. That is an SSRF-style fetch that defeats the
allowlist.

These tests drive a *real* ``httpx.AsyncClient`` wired to an
``httpx.MockTransport`` so the production ``event_hooks`` run against
httpx's own redirect machinery. They assert:

* a trusted→off-allowlist 30x is rejected with ``ArtifactDownloadError`` and
  nothing is written (covers ``169.254.169.254`` and ``evil.example``),
* a trusted→non-HTTPS (https→http downgrade) hop is rejected,
* an open-redirect that stays on a *trusted* host still succeeds,
* a multi-hop trusted→trusted→off-allowlist chain is rejected on the bad hop,
* a legitimate trusted→trusted redirect still downloads.

Covers BOTH the single-download and the batch surfaces.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import notebooklm._artifact.downloads as _downloads_mod
from notebooklm._android.assets import AndroidAssetDownloadService
from notebooklm._android.auth import BearerCredential
from notebooklm._artifact._download_client import (
    _is_trusted_download_host,
    _make_download_client,
)
from notebooklm._artifact._redirect_guard import redirect_revalidation_hooks
from notebooklm._client_metrics import ClientMetrics
from notebooklm._hop_credentials import HopCredentials
from notebooklm._runtime.call_supervisor import CallSupervisor
from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm.exceptions import ArtifactDownloadError, AuthError


@pytest.fixture
def mock_artifacts_api(tmp_path):
    """ArtifactsAPI wired to mocks -- no real network, real httpx clients."""
    from notebooklm._web.mind_maps import NoteBackedMindMapService
    from notebooklm._web.notes import NoteService
    from tests._fixtures.fake_core import make_fake_core

    mock_core = make_fake_core(
        rpc_call=AsyncMock(),
        get_source_ids=AsyncMock(return_value=[]),
    )
    api = WebArtifactsAPI(
        rpc=mock_core,
        supervisor=mock_core,
        notebooks=AsyncMock(),
        mind_maps=AsyncMock(spec=NoteBackedMindMapService),
        note_service=AsyncMock(spec=NoteService),
        storage_path=tmp_path / "storage.json",
    )
    return api


def _patch_real_client_with_transport(handler: Callable[[httpx.Request], httpx.Response]):
    """Patch the seams so a *real* ``httpx.AsyncClient`` runs over a MockTransport.

    The production code constructs ``httpx.AsyncClient(..., event_hooks=...)``;
    we forward every real kwarg (crucially ``event_hooks`` and
    ``follow_redirects``) and only inject ``transport=MockTransport(handler)``
    so the actual redirect machinery + the production request hook execute.
    """
    real_cls = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.setdefault("transport", httpx.MockTransport(handler))
        return real_cls(*args, **kwargs)

    return (
        patch.object(httpx, "AsyncClient", side_effect=_factory),
        # Freeze the PUBLIC ``load_httpx_cookies`` import, not the private
        # ``_load_httpx_cookies`` wrapper: both the single and batch download
        # paths in ``src/notebooklm/_artifact/downloads.py`` call
        # ``_load_httpx_cookies``, which delegates to this name — so patching
        # it here covers BOTH surfaces. Patching the private
        # ``_load_httpx_cookies`` alias directly would trip the ADR-0007
        # private-attribute monkeypatch guardrail.
        patch.object(_downloads_mod, "load_httpx_cookies", return_value=httpx.Cookies()),
    )


# A trusted host accepted by ``_is_trusted_download_host``.
_TRUSTED_HOST = "storage.googleapis.com"
_TRUSTED_URL = f"https://{_TRUSTED_HOST}/start"
_ANDROID_URL = "https://lh3.googleusercontent.com/start.png?capability=secret"
_SIGNED_URL = "https://storage.googleapis.com/bucket/final.png?signature=secret"
_PNG = b"\x89PNG\r\n\x1a\nredirect-test"


class _RotatingBearer:
    def __init__(self) -> None:
        self.calls: list[int] = []
        self.invalidations: list[int] = []

    async def get(self, expected_epoch: int) -> BearerCredential:
        self.calls.append(expected_epoch)
        generation = len(self.calls)
        return BearerCredential(f"token-{generation}", generation=generation)

    def invalidate(self, generation: int) -> None:
        self.invalidations.append(generation)


class _FailingBearer(_RotatingBearer):
    async def get(self, expected_epoch: int) -> BearerCredential:
        self.calls.append(expected_epoch)
        raise AuthError("master token expired")


async def _android_service(
    handler: Callable[[httpx.Request], httpx.Response],
    bearer: _RotatingBearer,
) -> AndroidAssetDownloadService:
    supervisor = CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=2,
    )
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)

    def client_factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=False,
            timeout=60.0,
        )

    service = AndroidAssetDownloadService(
        bearer_provider=bearer,  # type: ignore[arg-type]
        supervisor=supervisor,
        client_factory=client_factory,
    )
    await service.open(loop, 1)
    return service


@pytest.mark.asyncio
async def test_httpx_applies_cookie_policy_on_every_redirect_hop():
    """httpx runs the same credential policy for the initial and redirected request."""
    real_cls = httpx.AsyncClient
    seen_requests: list[tuple[str, str | None]] = []
    policy_calls: list[str] = []
    cookies = httpx.Cookies()
    cookies.set("SID", "secret", domain=".googleapis.com")

    def handler(request: httpx.Request) -> httpx.Response:
        seen_requests.append((str(request.url), request.headers.get("cookie")))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"ok")

    async def credential_for(url: str) -> HopCredentials:
        policy_calls.append(url)
        return HopCredentials(cookies=cookies)

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_cls(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", side_effect=client_factory):
        client, get_guarded = _make_download_client(
            cookies,
            timeout=30.0,
            credential_for=credential_for,
        )
        async with client:
            response = await get_guarded(_TRUSTED_URL)

    expected = [_TRUSTED_URL, f"https://{_TRUSTED_HOST}/final"]
    assert response.content == b"ok"
    assert policy_calls == expected
    assert [url for url, _cookie in seen_requests] == expected
    assert all(cookie == "SID=secret" for _url, cookie in seen_requests)


@pytest.mark.asyncio
async def test_httpx_policy_jar_replaces_constructor_jar():
    """The selected jar is authoritative, rather than only an allow/drop signal."""
    real_cls = httpx.AsyncClient
    seen_cookies: list[str | None] = []
    constructor_jar = httpx.Cookies()
    constructor_jar.set("SID", "constructor", domain=".googleapis.com")
    selected_jar = httpx.Cookies()
    selected_jar.set("SID", "selected", domain=".googleapis.com")

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, content=b"ok")

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_cls(*args, **kwargs)

    async def credential_for(_url: str) -> HopCredentials:
        return HopCredentials(cookies=selected_jar)

    with patch.object(httpx, "AsyncClient", side_effect=client_factory):
        client, get_guarded = _make_download_client(
            constructor_jar,
            timeout=30.0,
            credential_for=credential_for,
        )
        async with client:
            await get_guarded(_TRUSTED_URL)

    assert seen_cookies == ["SID=selected"]


@pytest.mark.asyncio
async def test_httpx_default_policy_normalizes_mapping_cookie_input():
    """Legacy cookie loaders may return a plain mapping rather than a Cookies jar."""
    real_cls = httpx.AsyncClient
    seen_cookies: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        return httpx.Response(200, content=b"ok")

    def client_factory(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real_cls(*args, **kwargs)

    with patch.object(httpx, "AsyncClient", side_effect=client_factory):
        client, get_guarded = _make_download_client(
            {"SID": "mapping"},
            timeout=30.0,
        )
        async with client:
            await get_guarded(_TRUSTED_URL)

    assert seen_cookies == ["SID=mapping"]


@pytest.mark.asyncio
async def test_httpx_none_policy_drops_first_hop_constructor_credentials():
    """A first-hop None result removes constructor cookies and bearer headers."""
    seen: list[tuple[str | None, str | None, str | None]] = []
    constructor_jar = httpx.Cookies()
    constructor_jar.set("SID", "constructor", domain=".googleapis.com")

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(
            (
                request.headers.get("cookie"),
                request.headers.get("authorization"),
                request.headers.get("proxy-authorization"),
            )
        )
        return httpx.Response(200, content=b"ok")

    async def no_credentials(_url: str) -> None:
        return None

    async with httpx.AsyncClient(
        cookies=constructor_jar,
        headers={
            "Authorization": "Bearer constructor",
            "Proxy-Authorization": "Bearer proxy-constructor",
        },
        event_hooks=redirect_revalidation_hooks(
            _is_trusted_download_host,
            no_credentials,
        ),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get(_TRUSTED_URL)

    assert seen == [(None, None, None)]


@pytest.mark.asyncio
async def test_httpx_bearer_policy_attaches_then_drops_on_trusted_redirect():
    """Policy headers are re-evaluated rather than inherited across trusted hops."""
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/final"})
        return httpx.Response(200, content=b"ok")

    async def credential_for(url: str) -> HopCredentials | None:
        if url.endswith("/start"):
            return HopCredentials(headers={"Authorization": "Bearer selected"})
        return None

    async with httpx.AsyncClient(
        follow_redirects=True,
        event_hooks=redirect_revalidation_hooks(_is_trusted_download_host, credential_for),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get(_TRUSTED_URL)

    assert seen == [
        (_TRUSTED_URL, "Bearer selected"),
        (f"https://{_TRUSTED_HOST}/final", None),
    ]


@pytest.mark.asyncio
async def test_httpx_selected_jar_receives_redirect_set_cookie():
    """A redirect response rotates the selected jar before the next hop."""
    seen_cookies: list[str | None] = []
    constructor_jar = httpx.Cookies()
    constructor_jar.set("SID", "constructor", domain=".googleapis.com")
    selected_jar = httpx.Cookies()
    selected_jar.set("SID", "selected", domain=".googleapis.com")

    def handler(request: httpx.Request) -> httpx.Response:
        seen_cookies.append(request.headers.get("cookie"))
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={
                    "location": "/final",
                    "set-cookie": "ROTATED=next; Path=/; Secure",
                },
            )
        return httpx.Response(200, content=b"ok")

    async def credential_for(_url: str) -> HopCredentials:
        return HopCredentials(cookies=selected_jar)

    async with httpx.AsyncClient(
        cookies=constructor_jar,
        follow_redirects=True,
        event_hooks=redirect_revalidation_hooks(
            _is_trusted_download_host,
            credential_for,
        ),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get(_TRUSTED_URL)

    assert seen_cookies[0] == "SID=selected"
    assert set(seen_cookies[1].split("; ")) == {"SID=selected", "ROTATED=next"}


@pytest.mark.asyncio
async def test_httpx_selected_jar_domain_matches_each_redirect_host():
    """A structured multi-domain jar emits only the cookie matching each hop."""
    selected_jar = httpx.Cookies()
    selected_jar.set("API", "api-cookie", domain=".googleapis.com")
    selected_jar.set("MEDIA", "media-cookie", domain=".googleusercontent.com")
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.url.host, request.headers.get("cookie")))
        if request.url.path == "/start":
            return httpx.Response(
                302,
                headers={"location": "https://lh3.googleusercontent.com/final"},
            )
        return httpx.Response(200, content=b"ok")

    async def credential_for(_url: str) -> HopCredentials:
        return HopCredentials(cookies=selected_jar)

    async with httpx.AsyncClient(
        follow_redirects=True,
        event_hooks=redirect_revalidation_hooks(
            _is_trusted_download_host,
            credential_for,
        ),
        transport=httpx.MockTransport(handler),
    ) as client:
        await client.get(_TRUSTED_URL)

    assert seen == [
        ("storage.googleapis.com", "API=api-cookie"),
        ("lh3.googleusercontent.com", "MEDIA=media-cookie"),
    ]


@pytest.mark.asyncio
async def test_android_sticky_bearer_stays_off_after_bounce_back(tmp_path: Path) -> None:
    returned = "https://lh3.googleusercontent.com/returned.png?capability=again"
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.host == "lh3.googleusercontent.com" and request.url.path == "/start.png":
            return httpx.Response(302, headers={"location": _SIGNED_URL})
        if request.url.host == "storage.googleapis.com":
            return httpx.Response(302, headers={"location": returned})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_PNG)

    bearer = _RotatingBearer()
    service = await _android_service(handler, bearer)
    output = tmp_path / "bounce.png"

    await service.download_url(_ANDROID_URL, str(output))

    assert output.read_bytes() == _PNG
    assert [authorization for _url, authorization in seen] == ["Bearer token-1", None, None]
    assert bearer.calls == [1]


@pytest.mark.asyncio
async def test_android_reacquires_bearer_on_each_allowed_hop(tmp_path: Path) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if request.url.path == "/start.png":
            return httpx.Response(307, headers={"location": "/fresh.png"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_PNG)

    bearer = _RotatingBearer()
    service = await _android_service(handler, bearer)

    await service.download_url(_ANDROID_URL, str(tmp_path / "fresh.png"))

    # The fake advances generation on every get(), modelling a cache deadline
    # crossed between hops. The second request must use the newly acquired token.
    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert bearer.calls == [1, 1]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403])
async def test_android_mid_chain_auth_failure_invalidates_failing_generation(
    tmp_path: Path, status: int
) -> None:
    seen: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("authorization"))
        if request.url.path == "/start.png":
            return httpx.Response(307, headers={"location": "/expired.png"})
        return httpx.Response(status)

    bearer = _RotatingBearer()
    service = await _android_service(handler, bearer)

    with pytest.raises(AuthError) as captured:
        await service.download_url(_ANDROID_URL, str(tmp_path / "expired.png"))

    assert f"HTTP {status}" in str(captured.value)
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert seen == ["Bearer token-1", "Bearer token-2"]
    assert bearer.invalidations == [2]


@pytest.mark.asyncio
async def test_web_mid_chain_auth_failure_preserves_http_cause(
    mock_artifacts_api: WebArtifactsAPI, tmp_path: Path
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/start":
            return httpx.Response(302, headers={"location": "/expired"})
        return httpx.Response(401)

    client_patch, cookies_patch = _patch_real_client_with_transport(handler)
    with client_patch, cookies_patch, pytest.raises(AuthError) as captured:
        await mock_artifacts_api._download_url(_TRUSTED_URL, str(tmp_path / "web.bin"))

    assert isinstance(captured.value.__cause__, httpx.HTTPStatusError)


@pytest.mark.asyncio
async def test_android_batch_uses_fresh_sticky_policy_for_each_url(tmp_path: Path) -> None:
    second = "https://lh3.googleusercontent.com/second.png?capability=two"
    seen: list[tuple[str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("authorization")))
        if request.url.path == "/start.png":
            return httpx.Response(302, headers={"location": _SIGNED_URL})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=_PNG)

    bearer = _RotatingBearer()
    service = await _android_service(handler, bearer)
    first_output = tmp_path / "first.png"
    second_output = tmp_path / "second.png"

    result = await service.download_urls_batch(
        [(_ANDROID_URL, str(first_output)), (second, str(second_output))]
    )

    assert result.succeeded == [str(first_output), str(second_output)]
    assert [authorization for _url, authorization in seen] == [
        "Bearer token-1",
        None,
        "Bearer token-2",
    ]


@pytest.mark.asyncio
async def test_android_batch_invalidates_each_failing_generation_before_next_url(
    tmp_path: Path,
) -> None:
    second = "https://lh3.googleusercontent.com/second.png?capability=two"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    bearer = _RotatingBearer()
    service = await _android_service(handler, bearer)

    with pytest.raises(AuthError) as captured:
        await service.download_urls_batch(
            [
                (_ANDROID_URL, str(tmp_path / "first.png")),
                (second, str(tmp_path / "second.png")),
            ]
        )

    assert captured.value.__cause__ is None
    assert bearer.invalidations == [1, 2]
    assert bearer.calls == [1, 1]


@pytest.mark.asyncio
async def test_android_batch_attempts_each_url_when_bearer_mint_fails(tmp_path: Path) -> None:
    second = "https://lh3.googleusercontent.com/second.png?capability=two"
    bearer = _FailingBearer()
    service = await _android_service(
        lambda _request: pytest.fail("mint failure must precede dispatch"), bearer
    )

    with pytest.raises(AuthError) as captured:
        await service.download_urls_batch(
            [
                (_ANDROID_URL, str(tmp_path / "first.png")),
                (second, str(tmp_path / "second.png")),
            ]
        )

    assert str(captured.value) == "master token expired"
    assert bearer.calls == [1, 1]
    assert bearer.invalidations == []


def _redirect_handler(
    *,
    location: str,
    body: bytes = b"PAYLOAD",
    content_type: str = "video/mp4",
) -> Callable[[httpx.Request], httpx.Response]:
    """Return a transport handler: trusted ``/start`` 302s to ``location``."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == _TRUSTED_HOST and request.url.path == "/start":
            return httpx.Response(302, headers={"location": location})
        # Any other (the redirect target) serves a body. If the hop is
        # off-allowlist the production hook must have already aborted, so
        # reaching here for an untrusted host is itself a test failure.
        return httpx.Response(200, content=body, headers={"content-type": content_type})

    return handler


# ---------------------------------------------------------------------------
# Single-download path: download_url
# ---------------------------------------------------------------------------


class TestSingleDownloadRedirectRevalidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "evil_location",
        [
            "https://169.254.169.254/latest/meta-data/",
            "https://evil.example/payload",
            "https://localhost/payload",
        ],
    )
    async def test_offallowlist_redirect_rejected_nothing_written(
        self, mock_artifacts_api, tmp_path, evil_location
    ):
        """trusted→off-allowlist 30x → ArtifactDownloadError, no file/temp left."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(location=evil_location)
        )

        with client_patch, cookies_patch, pytest.raises(ArtifactDownloadError) as exc_info:
            await api._download_url(_TRUSTED_URL, str(output_path))

        assert "Untrusted" in str(exc_info.value)
        assert not output_path.exists()
        assert list(tmp_path.glob("file.mp4.*.tmp")) == []

    @pytest.mark.asyncio
    async def test_non_https_redirect_hop_rejected(self, mock_artifacts_api, tmp_path):
        """https→http downgrade to a same-host hop is rejected (no plaintext fetch)."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(location=f"http://{_TRUSTED_HOST}/payload")
        )

        with client_patch, cookies_patch, pytest.raises(ArtifactDownloadError) as exc_info:
            await api._download_url(_TRUSTED_URL, str(output_path))

        assert "non-HTTPS" in str(exc_info.value)
        assert not output_path.exists()
        assert list(tmp_path.glob("file.mp4.*.tmp")) == []

    @pytest.mark.asyncio
    async def test_multihop_trusted_then_offallowlist_rejected(self, mock_artifacts_api, tmp_path):
        """trusted→trusted→evil: rejected on the off-allowlist hop, nothing written."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == _TRUSTED_HOST and request.url.path == "/start":
                # First hop -> another trusted host (a CDN already on the
                # allowlist).
                return httpx.Response(
                    302, headers={"location": "https://cdn.googleusercontent.com/hop2"}
                )
            if request.url.host == "cdn.googleusercontent.com":
                # Second (trusted) hop -> an off-allowlist host.
                return httpx.Response(302, headers={"location": "https://evil.example/payload"})
            return httpx.Response(200, content=b"EVIL", headers={"content-type": "video/mp4"})

        client_patch, cookies_patch = _patch_real_client_with_transport(handler)
        with client_patch, cookies_patch, pytest.raises(ArtifactDownloadError) as exc_info:
            await api._download_url(_TRUSTED_URL, str(output_path))

        assert "Untrusted download domain" in str(exc_info.value)
        assert "evil.example" in str(exc_info.value)
        assert not output_path.exists()
        assert list(tmp_path.glob("file.mp4.*.tmp")) == []

    @pytest.mark.asyncio
    async def test_trusted_to_trusted_redirect_still_succeeds(self, mock_artifacts_api, tmp_path):
        """A legitimate signed-URL CDN redirect (trusted→trusted) downloads fine."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        # storage.googleapis.com -> googleusercontent.com signed CDN (both trusted).
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(
                location="https://lh3.googleusercontent.com/signed/file.mp4",
                body=b"REAL MEDIA BYTES",
            )
        )

        with client_patch, cookies_patch:
            result = await api._download_url(_TRUSTED_URL, str(output_path))

        assert result == str(output_path)
        assert output_path.read_bytes() == b"REAL MEDIA BYTES"

    @pytest.mark.asyncio
    async def test_open_redirect_staying_on_trusted_host_succeeds(
        self, mock_artifacts_api, tmp_path
    ):
        """An open-redirect that stays on a trusted host is not over-blocked."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(
                location=f"https://{_TRUSTED_HOST}/redirected/file.mp4",
                body=b"SAME HOST BYTES",
            )
        )

        with client_patch, cookies_patch:
            result = await api._download_url(_TRUSTED_URL, str(output_path))

        assert result == str(output_path)
        assert output_path.read_bytes() == b"SAME HOST BYTES"


# ---------------------------------------------------------------------------
# Batch path: download_urls_batch
# ---------------------------------------------------------------------------


class TestBatchDownloadRedirectRevalidation:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "evil_location",
        [
            "https://169.254.169.254/latest/meta-data/",
            "https://evil.example/payload",
        ],
    )
    async def test_offallowlist_redirect_aggregated_into_failed_nothing_written(
        self, mock_artifacts_api, tmp_path, evil_location
    ):
        """trusted→off-allowlist 30x → failed entry, no file written for it."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(location=evil_location)
        )

        with client_patch, cookies_patch:
            result = await api._download_urls_batch([(_TRUSTED_URL, str(output_path))])

        assert result.succeeded == []
        assert len(result.failed) == 1
        failed_url, failed_exc = result.failed[0]
        assert failed_url == _TRUSTED_URL
        assert isinstance(failed_exc, ArtifactDownloadError)
        assert "Untrusted" in str(failed_exc)
        assert not output_path.exists()

    @pytest.mark.asyncio
    async def test_non_https_redirect_hop_rejected(self, mock_artifacts_api, tmp_path):
        """https→http downgrade hop aggregated into ``failed`` for the batch."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(location=f"http://{_TRUSTED_HOST}/payload")
        )

        with client_patch, cookies_patch:
            result = await api._download_urls_batch([(_TRUSTED_URL, str(output_path))])

        assert result.succeeded == []
        assert len(result.failed) == 1
        _, failed_exc = result.failed[0]
        assert isinstance(failed_exc, ArtifactDownloadError)
        assert "non-HTTPS" in str(failed_exc)
        assert not output_path.exists()

    @pytest.mark.asyncio
    async def test_bad_redirect_isolated_from_good_sibling(self, mock_artifacts_api, tmp_path):
        """A redirect-to-evil URL fails while a clean sibling still succeeds."""
        api = mock_artifacts_api
        bad_url = f"https://{_TRUSTED_HOST}/start"
        good_url = f"https://{_TRUSTED_HOST}/clean.mp4"
        bad_path = tmp_path / "bad.mp4"
        good_path = tmp_path / "good.mp4"

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/start":
                return httpx.Response(302, headers={"location": "https://evil.example/payload"})
            if request.url.path == "/clean.mp4":
                return httpx.Response(
                    200, content=b"GOOD BYTES", headers={"content-type": "video/mp4"}
                )
            return httpx.Response(200, content=b"EVIL", headers={"content-type": "video/mp4"})

        client_patch, cookies_patch = _patch_real_client_with_transport(handler)
        with client_patch, cookies_patch:
            result = await api._download_urls_batch(
                [(bad_url, str(bad_path)), (good_url, str(good_path))]
            )

        assert result.succeeded == [str(good_path)]
        assert good_path.read_bytes() == b"GOOD BYTES"
        assert len(result.failed) == 1
        failed_url, failed_exc = result.failed[0]
        assert failed_url == bad_url
        assert isinstance(failed_exc, ArtifactDownloadError)
        assert not bad_path.exists()
        assert result.partial

    @pytest.mark.asyncio
    async def test_trusted_to_trusted_redirect_still_succeeds(self, mock_artifacts_api, tmp_path):
        """A legitimate trusted→trusted redirect downloads in the batch path too."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(
                location="https://lh3.googleusercontent.com/signed/file.mp4",
                body=b"REAL MEDIA BYTES",
            )
        )

        with client_patch, cookies_patch:
            result = await api._download_urls_batch([(_TRUSTED_URL, str(output_path))])

        assert result.succeeded == [str(output_path)]
        assert result.failed == []
        assert output_path.read_bytes() == b"REAL MEDIA BYTES"


# ---------------------------------------------------------------------------
# Percent-encoded host parser-differential bypass (#1521 re-review)
# ---------------------------------------------------------------------------

# ``_is_trusted_download_host`` previously percent-decoded the hostname before
# matching, so ``evil%2egoogleapis.com`` decoded to ``evil.googleapis.com`` and
# was judged TRUSTED — but httpx connects to the RAW host
# ``evil%2egoogleapis.com``. The guard validated a *different* host than the
# one actually connected to (a parser differential), letting the body land on
# disk. These hosts must be rejected at every gate: the initial URL and any
# redirect target, single and batch.
_PERCENT_ENCODED_HOSTS = [
    "evil%2egoogleapis.com",  # %2e -> '.' under the old unquote()
    "evil%2Egoogleapis.com",  # uppercase variant
    "storage.googleapis.com%2eevil.example",  # encoded dot mid-host
]


class TestPercentEncodedHostBypass:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", _PERCENT_ENCODED_HOSTS)
    async def test_single_initial_url_rejected(self, mock_artifacts_api, tmp_path, host):
        """A percent-encoded host as the INITIAL url is rejected (single)."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        # No redirect needed: the bad host is the initial URL itself.
        client_patch, cookies_patch = _patch_real_client_with_transport(
            lambda request: httpx.Response(200, content=b"EVIL")
        )

        with client_patch, cookies_patch, pytest.raises(ArtifactDownloadError) as exc_info:
            await api._download_url(f"https://{host}/payload", str(output_path))

        assert "Untrusted" in str(exc_info.value)
        assert not output_path.exists()
        assert list(tmp_path.glob("file.mp4.*.tmp")) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", _PERCENT_ENCODED_HOSTS)
    async def test_single_redirect_target_rejected(self, mock_artifacts_api, tmp_path, host):
        """A trusted URL redirecting to a percent-encoded host is rejected (single)."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(location=f"https://{host}/payload")
        )

        with client_patch, cookies_patch, pytest.raises(ArtifactDownloadError) as exc_info:
            await api._download_url(_TRUSTED_URL, str(output_path))

        assert "Untrusted" in str(exc_info.value)
        assert not output_path.exists()
        assert list(tmp_path.glob("file.mp4.*.tmp")) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", _PERCENT_ENCODED_HOSTS)
    async def test_batch_initial_url_rejected(self, mock_artifacts_api, tmp_path, host):
        """A percent-encoded host as the INITIAL url is rejected (batch)."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        url = f"https://{host}/payload"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            lambda request: httpx.Response(200, content=b"EVIL")
        )

        with client_patch, cookies_patch:
            result = await api._download_urls_batch([(url, str(output_path))])

        assert result.succeeded == []
        assert len(result.failed) == 1
        failed_url, failed_exc = result.failed[0]
        assert failed_url == url
        assert isinstance(failed_exc, ArtifactDownloadError)
        assert "Untrusted" in str(failed_exc)
        assert not output_path.exists()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("host", _PERCENT_ENCODED_HOSTS)
    async def test_batch_redirect_target_rejected(self, mock_artifacts_api, tmp_path, host):
        """A trusted URL redirecting to a percent-encoded host is rejected (batch)."""
        api = mock_artifacts_api
        output_path = tmp_path / "file.mp4"
        client_patch, cookies_patch = _patch_real_client_with_transport(
            _redirect_handler(location=f"https://{host}/payload")
        )

        with client_patch, cookies_patch:
            result = await api._download_urls_batch([(_TRUSTED_URL, str(output_path))])

        assert result.succeeded == []
        assert len(result.failed) == 1
        _, failed_exc = result.failed[0]
        assert isinstance(failed_exc, ArtifactDownloadError)
        assert "Untrusted" in str(failed_exc)
        assert not output_path.exists()
