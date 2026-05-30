"""Request-shape types for the shared authed POST pipeline.

This module gathers the shared request-construction types used by the
middleware chain. It owns the request Interface that RPC, chat, auth refresh,
and the chain terminal share.

Five names live here:

- :data:`AuthSnapshot` — point-in-time view of auth headers used to build
  one HTTP attempt. ADR-009 pins this as the public input type of the
  ``AuthRefreshMiddleware`` callbacks.
- :data:`BuildRequest` — sync callable that maps an ``AuthSnapshot`` to a
  ``(url, body, headers)`` tuple ready for the transport. The chain leaf reads
  the materialized ``RpcRequest`` fields directly; the callable remains in
  ``RpcRequest.context["build_request"]`` so auth refresh and terminal
  freshness checks can rebuild the envelope from a new snapshot.
- :data:`PostBody` — body type accepted by the legacy tuple-return
  ``BuildRequest`` shape and by the low-level streaming POST helper.
- :class:`BuildRequestResult` — the *named* dataclass form of the same
  ``(url, body, headers)`` triple, introduced for PR 12.8's
  ``AuthRefreshMiddleware.build_request_factory`` callback. The dataclass
  shape is preferred for new code (named fields, immutable, type-checked
  at construction) over the legacy tuple return. Existing callers continue
  to use the tuple shape until they migrate.
- :func:`materialize_build_request` — bridge from the legacy tuple callback
  to ``BuildRequestResult``. This is the contract used before handing a request
  envelope to ``Kernel.post``.

See ``docs/adr/0009-middleware-chain.md`` for the full chain contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class AuthSnapshot:
    """Point-in-time view of auth headers used to build a single request.

    Captured once per HTTP attempt by ``_perform_authed_post`` and passed
    into the caller-supplied ``build_request`` factory so the URL/body are
    consistent for that attempt. On retry, a *new* snapshot is taken so
    refreshed credentials are picked up before the rebuild.
    """

    csrf_token: str
    session_id: str
    authuser: int
    account_email: str | None


# Build-request factory: receives a fresh ``AuthSnapshot`` and returns the
# triple (url, body, extra_headers) for one HTTP attempt. The chain terminal
# invokes this once per materialization so refreshed snapshots are picked up
# after auth refresh.
PostBody = str | bytes
BuildRequest = Callable[[AuthSnapshot], tuple[str, PostBody, dict[str, str] | None]]


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


def materialize_build_request(
    build_request: BuildRequest,
    snapshot: AuthSnapshot,
) -> BuildRequestResult:
    """Build one HTTP-attempt request and normalize it to named fields.

    ``BuildRequest`` is the legacy callback shape used by RPC and chat
    callers. It returns a positional tuple and allows the body to be either a
    ``str`` or ``bytes``. The middleware chain's target envelope pins
    ``RpcRequest.body`` to bytes, so this bridge converts strings to UTF-8
    bytes and copies headers into a detached ``dict``.
    """
    url, body, headers = build_request(snapshot)
    body_bytes = body.encode("utf-8") if isinstance(body, str) else body
    detached_headers = dict(headers) if headers is not None else None
    return BuildRequestResult(url=url, body=body_bytes, headers=detached_headers)


__all__ = [
    "AuthSnapshot",
    "BuildRequest",
    "BuildRequestResult",
    "PostBody",
    "materialize_build_request",
]
