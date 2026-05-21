"""Request-shape exports for the Tier-12 middleware chain.

This module gathers the shared request-construction types used by the
middleware chain. ``AuthSnapshot`` and ``BuildRequest`` are defined in
``_authed_transport.py`` and re-exported here for call sites that prefer the
request-types namespace; ``BuildRequestResult`` is owned here because it is the
named dataclass shape used by auth-refresh request rebuilding.

Three names live here:

- :data:`AuthSnapshot` — point-in-time view of auth headers used to build
  one HTTP attempt. ADR-009 pins this as the public input type of the
  ``AuthRefreshMiddleware`` callbacks.
- :data:`BuildRequest` — sync callable that maps an ``AuthSnapshot`` to a
  ``(url, body, headers)`` tuple ready for the transport. The chain leaf reads
  the callable from ``RpcRequest.context["build_request"]`` and invokes it
  inside ``AuthedTransport.perform_authed_post``.
- :class:`BuildRequestResult` — the *named* dataclass form of the same
  ``(url, body, headers)`` triple, introduced for PR 12.8's
  ``AuthRefreshMiddleware.build_request_factory`` callback. The dataclass
  shape is preferred for new code (named fields, immutable, type-checked
  at construction) over the legacy tuple return. Existing callers continue
  to use the tuple shape until they migrate.

See ``docs/adr/0009-middleware-chain.md`` for the full chain contract and
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` section 2 for the
PR sequence.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from ._authed_transport import AuthSnapshot, BuildRequest


@dataclass(frozen=True)
class BuildRequestResult:
    """Named dataclass form of the ``(url, body, headers)`` request triple.

    Introduced for the Tier-12 ``AuthRefreshMiddleware`` (ADR-009, PR 12.8):
    the middleware's ``build_request_factory`` callback returns this dataclass
    instead of the legacy ``(url, body, headers)`` tuple so the constructor
    signature reads as a single named value rather than positional unpacking.

    The fields mirror the tuple's positional order:

    - ``url`` — fully-built ``batchexecute`` URL (including ``authuser`` and
      ``_reqid`` query params).
    - ``body`` — encoded ``batchexecute`` body. Pinned to :class:`bytes` in
      ADR-009; the legacy ``BuildRequest`` tuple accepts ``str | bytes`` for
      backward compatibility with existing call sites that build the body as
      a UTF-8 string.
    - ``headers`` — extra headers to merge for this request, or ``None`` when
      the snapshot's headers are sufficient.

    Frozen so a middleware cannot accidentally mutate a callback's return
    value before passing it back to the chain. Equality is value-based so
    tests can assert against expected results without identity tracking.
    """

    url: str
    body: bytes
    headers: Mapping[str, str] | None


__all__ = [
    "AuthSnapshot",
    "BuildRequest",
    "BuildRequestResult",
]
