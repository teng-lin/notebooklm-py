"""Actual curl cannot escape loopback and still verifies peer/hostname identity."""

from __future__ import annotations

import io

import httpx
import pytest

pytest.importorskip("curl_cffi", reason="requires the optional [impersonate] extra")

from curl_cffi import CurlOpt  # noqa: E402

from tests._fault_server.curl_routing import CurlFaultServer, CurlRouting  # noqa: E402
from tests._fault_server.http import Reply, Route  # noqa: E402


async def test_curl_session_and_upload_reject_unmapped_initial_destinations() -> None:
    async with CurlFaultServer() as server:
        routing = CurlRouting(server)
        try:
            async with routing.client_factory() as client:
                with pytest.raises(httpx.ConnectError, match="unmapped"):
                    await client.get("https://unmapped.example/secret")
                with pytest.raises(httpx.ConnectError, match="unmapped"):
                    await client.stream_upload(
                        "https://unmapped.example/upload",
                        io.BytesIO(b"x"),
                        total_bytes=1,
                        headers={},
                    )
            assert routing.rejected == 2
            assert server.journal == []
        finally:
            await routing.aclose()
    assert server.active_handlers == 0


async def test_curl_unknown_redirect_is_contained_and_hostname_verification_rejects_it() -> None:
    server = CurlFaultServer()
    server.enqueue(
        Route("GET", "notebook.google.com", "/redirect"),
        Reply(302, headers={"location": "https://unmapped.example/secret"}),
    )
    async with server:
        routing = CurlRouting(server)
        try:
            async with routing.client_factory() as client:
                with pytest.raises(httpx.RequestError) as failure:
                    await client.get("https://notebook.google.com/redirect")
            # An actual certificate-name failure proves the wildcard containment
            # reached local TLS. No foreign host can match this test certificate.
            assert "certificate" in str(failure.value).lower()
            assert len(server.journal) == 1
            assert routing._options[CurlOpt.SSL_VERIFYHOST] == 2
            assert not server.errors
        finally:
            await routing.aclose()
    assert server.active_handlers == 0


async def test_curl_does_not_trust_the_test_peer_without_explicit_ca() -> None:
    async with CurlFaultServer() as server:
        routing = CurlRouting(server)
        del routing._options[CurlOpt.CAINFO]
        try:
            async with routing.client_factory() as client:
                with pytest.raises(httpx.RequestError) as failure:
                    await client.get("https://notebook.google.com/asset")
            assert "certificate" in str(failure.value).lower()
            assert routing._options[CurlOpt.SSL_VERIFYPEER] == 1
            assert server.journal == []
        finally:
            await routing.aclose()
