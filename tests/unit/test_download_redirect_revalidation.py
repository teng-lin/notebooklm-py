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

from collections.abc import Callable
from unittest.mock import AsyncMock, patch

import httpx
import pytest

import notebooklm._artifact.downloads as _downloads_mod
from notebooklm._artifacts import ArtifactsAPI
from notebooklm.types import ArtifactDownloadError


@pytest.fixture
def mock_artifacts_api(tmp_path):
    """ArtifactsAPI wired to mocks -- no real network, real httpx clients."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService
    from tests._fixtures.fake_core import make_fake_core

    mock_core = make_fake_core(
        rpc_call=AsyncMock(),
        get_source_ids=AsyncMock(return_value=[]),
    )
    api = ArtifactsAPI(
        rpc=mock_core,
        drain=mock_core,
        lifecycle=mock_core,
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
        # ``_load_httpx_cookies`` wrapper: both the single (downloads.py:717) and
        # batch (:817) paths call ``_load_httpx_cookies``, which delegates to this
        # name — so patching it here covers BOTH surfaces. Patching the private
        # ``_load_httpx_cookies`` alias directly would trip the ADR-0007
        # private-attribute monkeypatch guardrail.
        patch.object(_downloads_mod, "load_httpx_cookies", return_value=httpx.Cookies()),
    )


# A trusted host accepted by ``_is_trusted_download_host``.
_TRUSTED_HOST = "storage.googleapis.com"
_TRUSTED_URL = f"https://{_TRUSTED_HOST}/start"


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
