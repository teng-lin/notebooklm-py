"""Request spies through the real httpx and curl-cffi client adapters."""

from __future__ import annotations

import asyncio
import importlib.util
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from notebooklm._android.assets import AndroidAssetDownloadService
from notebooklm._android.auth import BearerCredential
from notebooklm._client_metrics import ClientMetrics
from notebooklm._runtime.call_supervisor import CallSupervisor

PNG = b"\x89PNG\r\n\x1a\ntransport-adapter"
INITIAL = "https://lh3.googleusercontent.com/start.png?cap=initial"
SIGNED = "https://storage.googleapis.com/bucket/final.png?X-Goog-Signature=secret"
TOKEN = "adapter-bearer-secret"


class _Bearer:
    async def get(self, expected_epoch: int) -> BearerCredential:
        assert expected_epoch == 1
        return BearerCredential(TOKEN, generation=1)

    def invalidate(self, generation: int) -> None:
        raise AssertionError(f"unexpected invalidation: {generation}")


class _CurlResponse:
    def __init__(self, status: int, headers: dict[str, str], chunks: list[bytes]) -> None:
        self.status_code = status
        self.headers = httpx.Headers(headers)
        self._chunks = chunks

    async def aiter_content(self):
        for chunk in self._chunks:
            yield chunk


class _CurlContext:
    def __init__(self, response: _CurlResponse) -> None:
        self.response = response

    async def __aenter__(self) -> _CurlResponse:
        return self.response

    async def __aexit__(self, *exc: object) -> None:
        return None


def _supervisor() -> CallSupervisor:
    return CallSupervisor(
        metrics=ClientMetrics(),
        max_concurrent_rpcs=2,
    )


async def _exercise(client: Any, output: Path) -> None:
    supervisor = _supervisor()
    loop = asyncio.get_running_loop()
    supervisor.set_bound_loop(loop)
    supervisor.reset_after_open()
    supervisor.prepare_generation(1)
    supervisor.start_accepting(1)
    service = AndroidAssetDownloadService(
        bearer_provider=_Bearer(),  # type: ignore[arg-type]
        supervisor=supervisor,
        client_factory=lambda: client,
    )
    await service.open(loop, 1)
    assert await service.download_url(INITIAL, str(output)) == str(output)


@pytest.mark.asyncio
async def test_httpx_adapter_applies_bearer_per_hop_and_streams_once(tmp_path: Path) -> None:
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": SIGNED}, request=request)
        return httpx.Response(
            200,
            headers={"content-type": "image/png"},
            content=PNG,
            request=request,
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        follow_redirects=False,
        timeout=60.0,
    )
    await _exercise(client, tmp_path / "httpx.png")

    assert [str(request.url) for request in requests] == [f"{INITIAL}&alr=yes", SIGNED]
    assert requests[0].headers["authorization"] == f"Bearer {TOKEN}"
    assert "authorization" not in requests[1].headers


@pytest.mark.asyncio
async def test_curl_cffi_adapter_applies_bearer_per_call_and_drops_it(tmp_path: Path) -> None:
    if importlib.util.find_spec("curl_cffi") is None:
        if os.environ.get("CI"):
            pytest.fail("CI must install the impersonate extra for Android asset coverage")
        pytest.skip("requires the optional [impersonate] extra")

    from notebooklm._curl_cffi_transport import CurlCffiAsyncClient

    client = CurlCffiAsyncClient(cookies=None, follow_redirects=False, timeout=60.0)
    calls: list[tuple[str, str, dict[str, Any]]] = []
    responses = iter(
        [
            _CurlResponse(302, {"location": SIGNED}, []),
            _CurlResponse(200, {"content-type": "image/png"}, [PNG]),
        ]
    )

    def stream(method: str, url: str, **kwargs: Any) -> _CurlContext:
        calls.append((method, url, kwargs))
        return _CurlContext(next(responses))

    client._curl.stream = stream
    await _exercise(client, tmp_path / "curl.png")

    assert [(method, url) for method, url, _kwargs in calls] == [
        ("GET", f"{INITIAL}&alr=yes"),
        ("GET", SIGNED),
    ]
    assert calls[0][2]["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    assert calls[1][2]["headers"] == {}
    assert all(kwargs["allow_redirects"] is False for _, _, kwargs in calls)
