"""Transport-aware artifact-download client + trusted-host allowlist.

Extracted from :mod:`notebooklm._artifact.downloads` (ADR-0008 module-size
ratchet). Holds the download SSRF host allowlist and the factory that wires the
#1521 per-hop redirect guard for whichever transport is active — httpx (default,
auto-follow + event hook) or the opt-in curl_cffi (manual ``get_guarded`` loop).
``downloads.py`` re-exports these names so existing import paths stay stable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from urllib.parse import ParseResult, urlparse

import httpx

from .._curl_cffi_transport import resolve_transport_factory
from .._hop_credentials import CredentialPolicy, HopCredentials
from ._guarded_transfer import MAX_DOWNLOAD_REDIRECTS
from ._redirect_guard import redirect_revalidation_hooks

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

_TRUSTED_DOWNLOAD_DOMAINS = (".google.com", ".googleusercontent.com", ".googleapis.com")


def _is_trusted_download_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    # Match the EXACT host the transport connects to. We do NOT percent-decode:
    # decoding made ``evil%2egoogleapis.com`` (%2e -> '.') read as trusted while
    # the connection went to the raw non-Google host (#1521). A real Google host
    # never contains ``%``, so reject any (defense-in-depth w/ the slash guards).
    # The curl_cffi path passes ``quote=False`` so libcurl can't re-introduce this
    # gap by requoting after the check (see ``CurlCffiAsyncClient.get_guarded``).
    hostname = hostname.lower()
    if "%" in hostname or "\\" in hostname or "/" in hostname:
        return False
    return any(
        hostname == domain.lstrip(".") or hostname.endswith(domain)
        for domain in _TRUSTED_DOWNLOAD_DOMAINS
    )


def _download_display_host(parsed: ParseResult) -> str:
    if parsed.hostname is not None:
        return parsed.hostname
    return parsed.netloc.rsplit("@", 1)[-1]


def _make_download_client(
    cookies: Any,
    timeout: Any,
    credential_for: CredentialPolicy | None = None,
    trusted_host: Callable[[str | None], bool] = _is_trusted_download_host,
) -> tuple[Any, Callable[[str], Awaitable[httpx.Response]]]:
    """Build a download client + redirect-guarded GET for the active transport.

    Both enforce the #1521 per-hop trusted-host allowlist, just by different
    mechanisms: the default httpx client auto-follows redirects with the
    revalidation event hook; the opt-in curl_cffi client follows redirects
    *manually* via ``get_guarded`` (libcurl's internal auto-follow can't host a
    per-hop Python policy callback). Same predicate (``_is_trusted_download_host``)
    in both. The returned client is an async context manager.
    """
    factory = resolve_transport_factory()
    resolved_credential_for = credential_for
    if resolved_credential_for is None:
        cookie_jar = cookies if isinstance(cookies, httpx.Cookies) else httpx.Cookies(cookies)

        async def _default_credential_for(_url: str) -> HopCredentials | None:
            parsed = urlparse(_url)
            if parsed.scheme == "https" and trusted_host(parsed.hostname):
                return HopCredentials(cookies=cookie_jar)
            return None

        resolved_credential_for = _default_credential_for
    if factory is httpx.AsyncClient:
        client: Any = httpx.AsyncClient(
            cookies=cookies,
            follow_redirects=True,
            max_redirects=MAX_DOWNLOAD_REDIRECTS,
            timeout=timeout,
            event_hooks=redirect_revalidation_hooks(
                trusted_host,
                resolved_credential_for,
            ),  # #1521 + per-hop credentials
        )

        async def _get(url: str) -> httpx.Response:
            return await client.get(url)
    else:
        # curl_cffi: no auto-follow; get_guarded re-validates each hop (#1521).
        # Credentials are attached per hop inside ``get_guarded`` so a future
        # narrower policy can drop them without a credential-bearing session.
        client = factory(cookies=None, follow_redirects=False, timeout=timeout)

        async def _get(url: str) -> httpx.Response:
            return await client.get_guarded(
                url,
                is_trusted_host=trusted_host,
                credential_for=resolved_credential_for,
                max_redirects=MAX_DOWNLOAD_REDIRECTS,
            )

    return client, _get
