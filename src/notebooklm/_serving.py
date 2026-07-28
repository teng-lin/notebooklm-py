"""Shared bootstrap helpers for the ``notebooklm-mcp`` and ``notebooklm-server``
entry points: loopback classification and the non-loopback bind guard.

Both HTTP entry points must (a) decide whether a ``--host`` is loopback and (b)
refuse a non-loopback bind unless the operator opts in. That logic used to be
copy-pasted in ``mcp/__main__`` and ``server/__main__`` and had already drifted
(different normalization; neither handled IPv4-mapped IPv6). This module is the
single source; :func:`addr_is_loopback` is the same version-independent check the
REST request-auth path (``server/_auth``) uses.

This module imports NO ``click`` / ``rich`` / ``cli`` — it is reached from the MCP
stdio entry point, whose stdout must stay pristine and whose import surface stays
lean. (``server/_auth`` re-exposes :func:`addr_is_loopback` as ``_addr_is_loopback``.)
"""

from __future__ import annotations

import ipaddress

__all__ = [
    "LOOPBACK_HOSTNAMES",
    "addr_is_loopback",
    "check_bind_allowed",
    "host_header_is_loopback",
    "is_loopback",
]

#: Hostnames always treated as loopback even though they are not numeric IP
#: literals. An empty / whitespace host is intentionally absent — it must be
#: refused (binding to "" listens on all interfaces).
LOOPBACK_HOSTNAMES = frozenset({"localhost"})


def addr_is_loopback(text: str) -> bool:
    """Whether an IP literal is a loopback address, independent of Python version.

    ``ipaddress`` only resolves an IPv4-mapped IPv6 address (e.g.
    ``::ffff:127.0.0.1``) to its embedded IPv4 loopback in newer CPython patch
    releases, so ``IPv6Address.is_loopback`` is unreliable across the interpreter
    versions/patch levels we run on (it returned ``False`` for the mapped form on
    some macOS 3.10/3.11 runners). Unwrap ``ipv4_mapped`` ourselves first, then
    fall back to the native check. Returns ``False`` for anything unparseable.
    """
    try:
        addr = ipaddress.ip_address(text)
    except ValueError:
        return False
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        return mapped.is_loopback
    return addr.is_loopback


def is_loopback(host: str) -> bool:
    """Whether a bind ``host`` (a ``--host`` value) addresses a loopback interface.

    Normalizes case + surrounding whitespace, accepts the ``localhost`` alias, and
    otherwise parses ``host`` as an IP literal (IPv4-mapped-aware). Anything else (a
    public DNS name, ``0.0.0.0``, ``::``) is NOT loopback — fail closed.
    """
    stripped = host.strip()
    if stripped.lower() in LOOPBACK_HOSTNAMES:
        return True
    return addr_is_loopback(stripped)


def host_header_is_loopback(host_header: str) -> bool:
    """Whether an HTTP ``Host`` header addresses a loopback literal.

    Like :func:`is_loopback` but for a request ``Host`` header rather than a bind
    host: strips an optional ``:port`` suffix and tolerates the bracketed IPv6 form
    (``[::1]`` / ``[::1]:9420``). A public DNS name, ``0.0.0.0``, or an empty host is
    rejected — this is the DNS-rebinding guard for a loopback-bound HTTP server.
    """
    host = host_header.strip()
    if not host:
        return False
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return False
        candidate = host[1:end]
        # Anything after "]" must be empty or a ":port" — reject "[::1]evil.com".
        rest = host[end + 1 :]
        if rest and not (rest.startswith(":") and rest[1:].isdigit()):
            return False
    else:
        # Strip a trailing :port only when there is a single colon (a bare IPv6
        # literal has several and isn't a valid Host-with-port anyway).
        candidate = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
    return is_loopback(candidate)


def check_bind_allowed(host: str, *, allow_external: bool, what: str, allow_env: str) -> None:
    """Refuse to bind ``what`` to a non-loopback ``host`` unless explicitly opted in.

    An empty / whitespace-only ``host`` is a HARD refusal (fail closed) even with
    ``allow_external`` — binding to "" listens on all interfaces. ``allow_env`` names
    the per-server override env var in the refusal message.

    Raises:
        SystemExit: ``host`` is empty/whitespace, or is not loopback and
            ``allow_external`` is ``False``.
    """
    if not host.strip():
        raise SystemExit(
            f"Refusing to bind {what} to an empty host (this would expose it on all "
            "interfaces). Pass an explicit loopback host such as 127.0.0.1."
        )
    if is_loopback(host) or allow_external:
        return
    raise SystemExit(
        f"Refusing to bind {what} to non-loopback host '{host}'. This would expose it "
        f"to the network. Set {allow_env}=1 to override (only behind a trusted proxy)."
    )
