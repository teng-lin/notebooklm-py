"""Type-only scaffolding for the Tier-12 middleware chain.

This module ships in PR 12.1 of the Tier-12/13 greenfield migration. It
defines:

- :class:`RpcRequest` / :class:`RpcResponse` — the HTTP-shape envelopes the
  chain passes around (NOT RPC-shape; encoding/decoding lives above the
  chain in :meth:`RpcExecutor.rpc_call`).
- :data:`NextCall` — the call-the-next-link type alias used by middlewares
  and by the chain builder.
- :class:`Middleware` — the ``Protocol`` every middleware satisfies. Around-
  style: each middleware receives the request and a ``next_call`` callable;
  it decides whether (and how) to invoke ``next_call(request)``, optionally
  observing or transforming the response.
- :func:`materialize_rpc_request` — converts the legacy ``BuildRequest``
  callback shape into the future populated ``RpcRequest`` envelope.
- :func:`build_chain` — composes a ``Sequence[Middleware]`` around a terminal
  ``NextCall`` so the leftmost middleware in the sequence becomes the
  *outermost* wrapper (matches the ordering documented in ADR-009).

Production ``Session`` wiring composes these envelopes through the current
middleware stack. During the request-materialization migration, the chain
enters with populated ``RpcRequest(url, headers, body)`` fields and the
terminal consumes that envelope directly through ``Kernel.post``. See
``docs/adr/0009-middleware-chain.md`` for the load-bearing decisions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from ._middleware_context import (
    ALLOWED_RPC_CONTEXT_KEYS,
    RPC_CONTEXT_AUTH_REFRESHED,
    RPC_CONTEXT_AUTH_SNAPSHOT,
    RPC_CONTEXT_BUILD_REQUEST,
    RPC_CONTEXT_DISABLE_INTERNAL_RETRIES,
    RPC_CONTEXT_LOG_LABEL,
    RPC_CONTEXT_RPC_METHOD,
    RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS,
)
from ._request_types import AuthSnapshot, BuildRequest, materialize_build_request

# ---------------------------------------------------------------------------
# Chain envelope types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RpcRequest:
    """HTTP-shape request envelope passed through the middleware chain.

    The chain wraps ``Kernel.post``. Every middleware sees an already-encoded
    HTTP request — encoding lives *above* the chain in :meth:`RpcExecutor.rpc_call`.
    RPC-level metadata that middlewares need (rpc
    method id, idempotency, operation variant, log labels, build-request
    callback, etc.) travels through :attr:`context`.

    Frozen: middlewares that want to alter the request build a new
    :class:`RpcRequest` via :func:`dataclasses.replace`. The
    :class:`AuthRefreshMiddleware` (PR 12.8) does exactly this when
    rebuilding headers and URL after an auth refresh.

    :attr:`context` is mutable by reference (it's a plain :class:`dict`) and
    is shared across the chain by design — see ADR-009 §"Per-request
    behavior". Middlewares that want isolation should make a shallow copy
    before mutating.
    """

    url: str
    """Fully-built ``batchexecute`` URL with ``authuser`` and ``_reqid`` set."""

    headers: Mapping[str, str]
    """HTTP headers for this attempt (auth headers, ``X-Goog-AuthUser``, …).

    Typed as :class:`~collections.abc.Mapping` (read-only protocol) rather
    than :class:`dict` so the frozen-dataclass contract extends to the
    header values: middlewares that want to add or alter headers build a
    new :class:`RpcRequest` via :func:`dataclasses.replace` with a freshly
    constructed dict (e.g.
    ``dataclasses.replace(request, headers={**request.headers, "X-Foo": "1"})``).
    Concrete :class:`dict` instances satisfy this annotation, so callers
    that pass a literal ``{...}`` need no special treatment.
    """

    body: bytes
    """Encoded ``batchexecute`` body bytes for this attempt."""

    context: dict[str, Any] = field(default_factory=dict)
    """RPC-level metadata the chain reads.

    The allowed vocabulary is exported as
    :data:`ALLOWED_RPC_CONTEXT_KEYS` and mirrored in ADR-009. Middlewares
    and the transport terminal use the ``RPC_CONTEXT_*`` constants for
    lookups and writes; adding a key requires updating this module, ADR-009,
    and the lint-style unit test that guards the vocabulary.
    """


@dataclass(frozen=True)
class RpcResponse:
    """HTTP-shape response envelope returned by the middleware chain.

    Carries the same :class:`httpx.Response` ``Kernel.post`` returns today,
    plus a propagated ``context`` so middlewares above the chain can read
    additions a deeper middleware made (e.g. a tracing middleware annotating
    the attempt with a trace id).

    Frozen for the same reason as :class:`RpcRequest`: middlewares that
    transform the response build a new instance via
    :func:`dataclasses.replace`.
    """

    response: httpx.Response
    """The buffered :class:`httpx.Response` from the transport leaf.

    Identical in shape to what ``Kernel.post`` returns via
    ``_streaming_post.stream_post_with_size_cap``: fully-buffered body,
    headers stripped of ``content-encoding`` / ``content-length`` so
    ``.text`` / ``.content`` work synchronously.
    """

    context: dict[str, Any] = field(default_factory=dict)
    """Propagated metadata. Typically the same dict as
    :attr:`RpcRequest.context` (so a middleware that wrote an allowed
    ``RPC_CONTEXT_*`` key can read it back here) plus any
    response-side additions a middleware made.
    """


def materialize_rpc_request(
    *,
    build_request: BuildRequest,
    snapshot: AuthSnapshot,
    context: dict[str, Any],
) -> RpcRequest:
    """Build a populated chain envelope from the legacy request callback.

    This is a behavior-neutral bridge for the Tier-13 request-materialization
    migration. ``Session`` uses this helper to enter the chain with populated
    ``RpcRequest(url, headers, body)`` fields while the transitional terminal
    still delegates through the legacy ``BuildRequest`` callback. A later
    ``Kernel.post`` terminal can consume the same envelope directly.

    ``context`` is intentionally retained by reference, matching ADR-009's
    mutable per-request metadata contract.
    """
    request = materialize_build_request(build_request, snapshot)
    return RpcRequest(
        url=request.url,
        headers=request.headers or {},
        body=request.body,
        context=context,
    )


# ---------------------------------------------------------------------------
# Chain-call callable type and the middleware Protocol.
# ---------------------------------------------------------------------------

#: Callable shape of the "call the next link" function passed to each
#: middleware. Implementations invoke ``await next_call(request)`` (or a
#: replaced ``await next_call(new_request)`` after transforming the
#: request via :func:`dataclasses.replace` — what
#: :class:`AuthRefreshMiddleware` does on its retry leg) to continue the
#: chain. A middleware may also short-circuit by returning a response
#: without invoking ``next_call`` at all; no production middleware in the
#: Tier-12 set does this, but test middlewares (e.g. a "deny all" canary)
#: are free to.
NextCall = Callable[[RpcRequest], Awaitable[RpcResponse]]


class Middleware(Protocol):
    """Around-style middleware Protocol.

    Each middleware is an *async callable*: it receives the
    :class:`RpcRequest` plus a :data:`NextCall` and returns an
    :class:`RpcResponse`. Implementations may:

    - Observe the request before calling ``next_call`` (logging, metrics).
    - Transform the request via :func:`dataclasses.replace` and pass the new
      one to ``next_call`` (auth refresh).
    - Wrap ``next_call`` in a try/except to handle specific exceptions
      (retry, auth refresh).
    - Observe or transform the response after ``next_call`` returns
      (metrics, tracing).
    - Short-circuit (return without calling ``next_call``). Used only by
      error-injection middlewares in tests; not by any production
      middleware in the Tier-12 set.

    The constructor of a middleware is not constrained by this Protocol —
    each middleware takes whatever collaborators it needs (the
    :class:`AuthRefreshMiddleware` constructor signature is pinned in
    ADR-009 §"AuthRefreshMiddleware constructor signature").
    """

    async def __call__(
        self,
        request: RpcRequest,
        next_call: NextCall,
    ) -> RpcResponse: ...


# ---------------------------------------------------------------------------
# Chain composition helper.
# ---------------------------------------------------------------------------


def build_chain(
    middlewares: Sequence[Middleware],
    terminal: NextCall,
) -> NextCall:
    """Compose ``middlewares`` around ``terminal`` and return the outer call.

    Ordering contract: the **first** middleware in the sequence becomes the
    **outermost** wrapper around ``terminal``. With
    ``middlewares = [A, B, C]`` and ``terminal = T``, the returned callable
    invokes ``A.__call__(request, →B)`` where ``→B`` invokes
    ``B.__call__(request, →C)`` where ``→C`` invokes
    ``C.__call__(request, →T)``.

    This matches the chain ordering documented in ADR-009: ``[Drain,
    Metrics, Semaphore, Retry, AuthRefresh, ErrorInjection, Tracing]`` —
    Drain at index 0 is the outermost wrapper, Tracing at index 6 is the
    innermost wrapper around the terminal.

    Implementation: wrap in reverse, so the last middleware in the sequence
    is composed first (it becomes the innermost wrapper, with ``terminal``
    as its ``next_call``), then each earlier middleware wraps the chain
    built so far. ``make_wrapper`` is a defensive idiom that captures
    ``mw`` and ``next_call`` in its own function scope (one set of
    bindings per call) rather than letting the inner ``wrapped`` close
    over the loop variable — without it, every wrapper in a Python loop
    would close over the *final* value of the loop variable.

    The returned :data:`NextCall` is safe to invoke concurrently from
    multiple tasks: the chain itself is stateless, and each middleware's
    state (if any) is its own concern. Returning a fresh chain per
    ``build_chain`` call also means tests can build per-test chains
    without leaking state across tests.

    An empty ``middlewares`` sequence returns ``terminal`` unchanged.
    """

    def make_wrapper(mw: Middleware, next_call: NextCall) -> NextCall:
        async def wrapped(request: RpcRequest) -> RpcResponse:
            return await mw(request, next_call)

        return wrapped

    chain: NextCall = terminal
    for mw in reversed(middlewares):
        chain = make_wrapper(mw, chain)
    return chain


__all__ = [
    "ALLOWED_RPC_CONTEXT_KEYS",
    "Middleware",
    "NextCall",
    "RPC_CONTEXT_AUTH_REFRESHED",
    "RPC_CONTEXT_AUTH_SNAPSHOT",
    "RPC_CONTEXT_BUILD_REQUEST",
    "RPC_CONTEXT_DISABLE_INTERNAL_RETRIES",
    "RPC_CONTEXT_LOG_LABEL",
    "RPC_CONTEXT_RPC_METHOD",
    "RPC_CONTEXT_RPC_QUEUE_WAIT_SECONDS",
    "RpcRequest",
    "RpcResponse",
    "build_chain",
    "materialize_rpc_request",
]
