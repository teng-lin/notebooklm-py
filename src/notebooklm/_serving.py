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

__all__ = ["LOOPBACK_HOSTNAMES", "addr_is_loopback", "check_bind_allowed", "is_loopback"]

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
