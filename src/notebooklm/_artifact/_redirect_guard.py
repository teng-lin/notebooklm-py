"""Per-redirect-hop allowlist + HTTPS revalidation for artifact downloads.

Both artifact-download clients (the single ``download_url`` stream and the
``download_urls_batch`` loop in :mod:`notebooklm._artifact.downloads`) use
``follow_redirects=True``. The initial host + scheme allowlist gate validates
only the URL the caller passed, so a *trusted* Google URL whose ``Location``
points off-allowlist — a non-HTTPS hop, or a private/link-local host such as
``169.254.169.254`` — would otherwise be followed and its body written to the
caller's ``output_path``. That is an SSRF-style fetch that defeats the
explicit allowlist (issue #1521).

This module supplies an httpx ``request`` event hook that re-checks every
hop's host + scheme *before the request is sent*, so an untrusted host never
receives a connection. The host-trust predicate is injected (rather than
imported) to keep this module free of a circular dependency on
``downloads.py``.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from .._hop_credentials import CredentialPolicy
from ..exceptions import ArtifactDownloadError

if TYPE_CHECKING:
    import httpx

# Host-trust predicate signature: ``(hostname | None) -> bool``.
_HostPredicate = Callable[[str | None], bool]


def _assert_trusted_download_request(
    request: httpx.Request, is_trusted_host: _HostPredicate
) -> None:
    """Reject an off-allowlist / non-HTTPS request hop.

    Runs for every hop (the initial request and each redirect target).
    Legitimate trusted→trusted redirects (Google signed-URL CDNs already on
    the allowlist) pass through untouched.

    Raises:
        ArtifactDownloadError: on the first hop whose scheme is not HTTPS or
            whose host is not on the trusted allowlist.
    """
    host = request.url.host or None
    if request.url.scheme != "https":
        raise ArtifactDownloadError(
            "media",
            details=f"Untrusted redirect to non-HTTPS hop: {host or '<unknown>'}",
        )
    if not is_trusted_host(host):
        raise ArtifactDownloadError(
            "media",
            details=f"Untrusted download domain: {host or '<unknown>'}",
        )


def redirect_revalidation_hooks(
    is_trusted_host: _HostPredicate,
    credential_for: CredentialPolicy | None = None,
) -> dict[str, list[Any]]:
    """Build httpx ``event_hooks`` re-validating every redirect hop (#1521).

    ``is_trusted_host`` is the download module's host-allowlist predicate; it
    is injected so this guard module has no import dependency on
    ``downloads.py`` (which imports *this* module).
    """

    # These credentials may already exist on a constructor/client request. Once
    # a policy is present, its result is authoritative even on the first hop.
    managed_header_names = {"authorization", "proxy-authorization"}

    cookie_jar_extension = "notebooklm.hop_cookie_jar"

    async def _on_request(request: httpx.Request) -> None:
        _assert_trusted_download_request(request, is_trusted_host)
        if credential_for is None:
            return

        credentials = credential_for(str(request.url))
        request.extensions[cookie_jar_extension] = (
            credentials.cookies if credentials is not None else None
        )

        # httpx builds Cookie from the constructor jar before request hooks run,
        # and redirect requests inherit same-origin headers. The policy result is
        # authoritative: remove all credentials managed on an earlier hop before
        # applying the current result.
        request.headers.pop("cookie", None)
        for name in managed_header_names:
            request.headers.pop(name, None)

        if credentials is None:
            return
        if credentials.cookies is not None:
            # Keep the jar structured and let its normal domain/path matching
            # decide whether this hop receives a Cookie header.
            credentials.cookies.set_cookie_header(request)
        managed_header_names.update(name.lower() for name in credentials.headers)
        request.headers.update(credentials.headers)

    async def _on_response(response: httpx.Response) -> None:
        cookie_jar = response.request.extensions.get(cookie_jar_extension)
        if cookie_jar is not None:
            # The httpx client owns an internal copy of its constructor jar. Keep
            # the policy-selected external jar in sync so a redirect Set-Cookie
            # is available when the policy selects that jar on the next hop.
            cookie_jar.extract_cookies(response)

    return {"request": [_on_request], "response": [_on_response]}
