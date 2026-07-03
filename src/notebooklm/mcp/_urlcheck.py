"""Shared validation for a **bare public https origin**.

claude.ai reaches the server at a public https tunnel URL, and two surfaces mount
their routes at the ROOT of that origin:

* the self-hosted OAuth AS (:mod:`._oauth`) — ``/authorize``, ``/token``,
  ``/register``, ``/login``, ``/.well-known/*``; and
* the file-transfer side-channel (:mod:`._fileroutes`) — ``/files/{ul,dl}/…``.

So the configured base URL must be a *bare* origin: an https scheme with a host
and no path/query/fragment. A ``/mcp``-suffixed connector URL (or any path) would
make the OAuth discovery metadata advertise endpoints that don't exist and the
signed file links point at the wrong place. This is the single check both call
sites share — extracted here so they stay in lockstep.

This module imports NO ``click`` / ``rich`` / ``cli``.
"""

from __future__ import annotations

from urllib.parse import urlsplit

__all__ = ["_validate_bare_https_origin"]


def _validate_bare_https_origin(url: str, env_name: str) -> None:
    """Validate ``url`` is a bare public https origin, or raise ``SystemExit``.

    A bare origin has the https scheme, a netloc (host), and no path other than a
    trailing ``"/"``, and no query or fragment.

    Args:
        url: The candidate base URL (already whitespace-stripped by the caller).
        env_name: The env var the value came from, named in the error so the
            operator knows which setting to fix.

    Raises:
        SystemExit: ``url`` is not a bare https origin (clear, env-named message).
    """
    # NOTE (security tripwire): this validator guards the *shape* of the origin
    # (scheme/host/no-path), NOT its reachability. A special-use / private / loopback /
    # link-local / metadata host is INTENTIONALLY accepted — the base URL is operator-
    # supplied config that is only advertised in OAuth discovery metadata + signed links,
    # never fetched by the server, so it is not an SSRF sink today, and rejecting private
    # hosts would break split-horizon / self-hosted-DNS tunnels. IF a future refactor ever
    # makes the server *fetch* this URL, it inherits an SSRF hole: at that call site resolve
    # the hostname and reject if ANY resolved address is not a public unicast target — i.e.
    # `not ip.is_global or ip.is_multicast` (this subsumes private/loopback/link-local/
    # reserved AND catches CGNAT/unspecified/multicast that a partial predicate list misses).
    # Inspecting an IP literal alone is insufficient (DNS rebinding — resolve, then filter).
    parsed = urlsplit(url)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        # Reject userinfo (``user:pass@host``): a misconfigured value like
        # ``https://a@evil.example`` would otherwise pass and every minted link
        # would carry the credential / point at the wrong host. Operator footgun,
        # not an attacker path — but a bare origin has no userinfo.
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise SystemExit(
            f"{env_name} must be a bare public https origin claude.ai reaches "
            f"(e.g. https://your-host) — NOT the /mcp connector URL, no userinfo, and "
            f"no path/query/fragment; got {url!r}."
        )
