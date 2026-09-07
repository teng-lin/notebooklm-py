"""Real curl routing to a TLS loopback server with private construction seams.

The bundled key/certificate are public synthetic test material, generated with
openssl req -x509 -newkey rsa:2048 and the four explicit logical SANs below.
They are trusted only by these per-client test handles, never by the OS store.
"""

from __future__ import annotations

import asyncio
import ssl
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from notebooklm._client_assembly import _assemble_client
from notebooklm._client_options import normalize_legacy_client_options
from notebooklm._curl_cffi_transport import CurlCffiAsyncClient
from notebooklm._http_client_factory import HttpClientFactories
from notebooklm.client import NotebookLMClient
from notebooklm.options import (
    ClientConfig,
    FeatureOptions,
    RetryOptions,
    TimeoutOptions,
    TransferOptions,
    WebBackendConfig,
    WebRequestOptions,
    WebTransportOptions,
)

from .http import HttpFaultServer
from .web import synthetic_auth

FIXTURES = Path(__file__).with_name("fixtures")
CERTIFICATE = FIXTURES / "loopback-test-cert.pem"
KEY = FIXTURES / "loopback-test-key.pem"
HOSTS = frozenset(
    {
        "notebook.google.com",
        "accounts.google.com",
        "lh3.googleusercontent.com",
        "storage.googleapis.com",
    }
)


class CurlFaultServer(HttpFaultServer):
    """Same journals/framing as portable HTTP, with actual TLS termination."""

    async def __aenter__(self) -> CurlFaultServer:
        if self._server is not None:
            raise RuntimeError("HTTP fault server already started")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(CERTIFICATE, KEY)
        self._server = await asyncio.start_server(self._accept, "127.0.0.1", 0, ssl=context)
        return self


class CurlRouting:
    """Own libcurl route lists until sessions and upload workers have closed."""

    def __init__(self, server: CurlFaultServer, *, timeout: float = 0.5) -> None:
        from curl_cffi import CurlOpt, ffi, lib

        self._server = server
        self._timeout = timeout
        self.clients: list[CurlCffiAsyncClient] = []
        self.sessions: list[Any] = []
        self.handles: list[Any] = []
        self.body_descriptors: list[Any] = []
        self.rejected = 0
        self._closed = False
        host, port = server.address
        self._routes = ffi.NULL
        for logical in sorted(HOSTS):
            self._routes = lib.curl_slist_append(
                self._routes, f"{logical}:443:{host}:{port}".encode()
            )
        # Contain libcurl's automatic redirect engine too: unknown destinations
        # can only reach this loopback TLS listener, whose exact SAN certificate
        # rejects their hostname. No unmatched destination can fall back to DNS.
        self._routes = lib.curl_slist_append(self._routes, f"::{host}:{port}".encode())
        self._options = {
            CurlOpt.CONNECT_TO: self._routes,
            CurlOpt.PROXY: b"",
            CurlOpt.CAINFO: str(CERTIFICATE).encode(),
            CurlOpt.SSL_VERIFYPEER: 1,
            CurlOpt.SSL_VERIFYHOST: 2,
        }

    def validate(self, url: str) -> None:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in HOSTS
            or parsed.port not in (None, 443)
            or parsed.username is not None
            or parsed.password is not None
        ):
            self.rejected += 1
            raise httpx.ConnectError("curl fault route is unmapped")

    def _session_factory(self, **kwargs: Any) -> Any:
        from curl_cffi.requests import AsyncSession

        owner = self

        class RoutedSession(AsyncSession):
            async def request(self, method: str, url: str, *args: Any, **kw: Any) -> Any:
                owner.validate(url)
                return await super().request(method, url, *args, **kw)

        session = RoutedSession(curl_options=self._options, trust_env=False, **kwargs)
        self.sessions.append(session)
        return session

    def _curl_factory(self) -> Any:
        from curl_cffi import Curl, CurlOpt

        owner = self

        class RoutedCurl(Curl):
            def impersonate(self, *args: Any, **kwargs: Any) -> Any:
                outcome = super().impersonate(*args, **kwargs)
                for option, value in owner._options.items():
                    super().setopt(option, value)
                return outcome

            def setopt(self, option: Any, value: Any) -> int:
                if option == CurlOpt.READFUNCTION:
                    cells = dict(
                        zip(value.__code__.co_freevars, value.__closure__ or (), strict=True)
                    )
                    if "fh" in cells:
                        owner.body_descriptors.append(cells["fh"].cell_contents)
                if option == CurlOpt.URL:
                    owner.validate(value.decode() if isinstance(value, bytes) else value)
                return super().setopt(option, value)

        handle = RoutedCurl()
        self.handles.append(handle)
        return handle

    def client_factory(self, **kwargs: Any) -> CurlCffiAsyncClient:
        if self._closed:
            raise RuntimeError("curl fault routing has closed")
        # The adapter's fixed download windows are shortened at construction;
        # actual curl timeouts and worker settlement remain in production code.
        kwargs["timeout"] = httpx.Timeout(self._timeout)
        client = CurlCffiAsyncClient(
            session_factory=self._session_factory,
            curl_factory=self._curl_factory,
            impersonate="chrome",
            **kwargs,
        )
        self.clients.append(client)
        return client

    async def aclose(self) -> None:
        from curl_cffi import lib

        if self._closed:
            return
        await asyncio.gather(
            *(client.aclose() for client in self.clients if not client._curl._closed)
        )
        if any(handle._curl is not None for handle in self.handles):
            raise AssertionError("curl upload handle outlived its owner")
        if not all(body.closed for body in self.body_descriptors):
            raise AssertionError("curl request body outlived its owner")
        lib.curl_slist_free_all(self._routes)
        self._closed = True


def build_curl_client(routing: CurlRouting) -> NotebookLMClient:
    client = NotebookLMClient.__new__(NotebookLMClient)
    timeout = TimeoutOptions(0.5, 0.5, 0.5, 0.5)
    options = normalize_legacy_client_options(
        config=ClientConfig(
            backend=WebBackendConfig(
                transport=WebTransportOptions(
                    read_timeout=0.5, write_timeout=0.5, pool_timeout=0.5
                ),
                request=WebRequestOptions(transport="curl_cffi", impersonate="chrome"),
            ),
            retry=RetryOptions(rate_limit_max_retries=0, server_error_max_retries=0),
            transfers=TransferOptions(start_timeout=timeout, finalize_timeout=timeout),
            features=FeatureOptions(chat_timeout=0.5),
        )
    )
    _assemble_client(
        client,
        auth=synthetic_auth(),
        options=options,
        async_client_factory=routing.client_factory,
        http_client_factories=HttpClientFactories(curl_cffi=routing.client_factory),
    )
    return client
