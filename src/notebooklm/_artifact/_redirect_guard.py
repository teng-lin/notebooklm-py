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


def redirect_revalidation_hooks(is_trusted_host: _HostPredicate) -> dict[str, list[Any]]:
    """Build httpx ``event_hooks`` re-validating every redirect hop (#1521).

    ``is_trusted_host`` is the download module's host-allowlist predicate; it
    is injected so this guard module has no import dependency on
    ``downloads.py`` (which imports *this* module).
    """

    async def _on_request(request: httpx.Request) -> None:
        _assert_trusted_download_request(request, is_trusted_host)

    return {"request": [_on_request]}
