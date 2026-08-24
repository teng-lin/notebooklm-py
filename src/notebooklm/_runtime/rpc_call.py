"""Typed request envelopes for the web runtime pipeline.

This module defines:

- :class:`RpcRequest` / :class:`RpcResponse` — the HTTP-shape envelopes the
  pipeline passes around (NOT RPC-shape; encoding/decoding lives above the
  pipeline in :meth:`WebExecutionRuntime.rpc_call`).
- :data:`NextCall` — the callable shape between fixed pipeline behaviors.
- :func:`materialize_rpc_request` — converts the ``BuildRequest``
  callback shape into the populated ``RpcRequest`` envelope.
Production ``NotebookLMClient`` wiring composes these envelopes through the
fixed runtime pipeline. The pipeline enters with populated
``RpcRequest(url, headers, body)`` fields and the terminal consumes that
envelope directly through ``Kernel.post``. See
``docs/adr/0009-middleware-chain.md`` for the load-bearing decisions.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import httpx

from .._request_types import AuthSnapshot, BuildRequest, materialize_build_request
from .rpc_call_state import RpcCallState

# ---------------------------------------------------------------------------
# Chain envelope types.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RpcRequest:
    """HTTP-shape request envelope passed through the runtime pipeline.

    The chain wraps ``Kernel.post``. Every middleware sees an already-encoded
    HTTP request — encoding lives *above* the chain in
    :meth:`WebExecutionRuntime.rpc_call`. RPC-level metadata that middlewares
    need travels through the typed :attr:`state` carrier.

    Frozen: middlewares that want to alter the request build a new
    :class:`RpcRequest`. Some can use :func:`dataclasses.replace`;
    :class:`AuthRefreshBehavior` rematerializes via
    :func:`materialize_rpc_request` after refresh so URL, headers, and body
    are rebuilt from the fresh auth snapshot.

    :attr:`state` is shared by exact identity across retries. Its immutable
    configuration and bounded publication methods replace the former open
    ``dict[str, Any]`` protocol.
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

    state: RpcCallState = field(default_factory=RpcCallState)
    """Closed typed state for this logical call."""


@dataclass(frozen=True)
class RpcResponse:
    """HTTP-shape response envelope returned by the runtime pipeline.

    Carries the same :class:`httpx.Response` ``Kernel.post`` returns today,
    plus the exact typed call state shared by its request.

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

    state: RpcCallState = field(default_factory=RpcCallState)
    """The exact state object carried by the corresponding request."""


def materialize_rpc_request(
    *,
    build_request: BuildRequest,
    snapshot: AuthSnapshot,
    state: RpcCallState,
) -> RpcRequest:
    """Build a populated chain envelope from the request callback.

    ``NotebookLMClient`` uses this helper to enter the chain with populated
    ``RpcRequest(url, headers, body)`` fields, and
    :class:`RuntimeTransport.terminal` consumes that envelope directly through
    ``Kernel.post``.

    ``state`` is intentionally retained by identity across every attempt.
    """
    request = materialize_build_request(build_request, snapshot)
    return RpcRequest(
        url=request.url,
        headers=request.headers or {},
        body=request.body,
        state=state,
    )


# ---------------------------------------------------------------------------
# Pipeline-call callable type.
# ---------------------------------------------------------------------------

#: Callable shape used between the fixed runtime behaviors.
NextCall = Callable[[RpcRequest], Awaitable[RpcResponse]]

__all__ = [
    "NextCall",
    "RpcRequest",
    "RpcResponse",
    "materialize_rpc_request",
]
