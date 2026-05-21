"""Test fixtures for the Tier-12 middleware chain.

These fixtures land in PR 12.1 so every subsequent middleware PR (12.3–12.8)
has a uniform test substrate. Each new middleware extraction can express its
unit tests as "build a chain with [..., FakeMiddleware(), ...], call it, assert
behaviour" without re-inventing the chain plumbing per PR.

Three helpers live here:

- :class:`FakeAuthedPost` — programmable stub matching the shape of
  :meth:`notebooklm._authed_transport.AuthedTransport.perform_authed_post`. This
  is **not** ``FakeKernel`` — the ``Kernel`` Protocol doesn't exist until
  PR 13.2, and renaming to ``FakeKernel`` is part of PR 13.2's scope. Until
  then, the terminal call still goes through ``AuthedTransport``.
- :func:`make_request` — factory for :class:`notebooklm._middleware.RpcRequest`
  instances with benign defaults. Tests override only the fields they care
  about via keyword arguments.
- :func:`chain_calls_through_to_authed_post` — assertion helper that builds
  a chain over a :class:`FakeAuthedPost` terminal, invokes it once, and
  returns whether the underlying ``perform_authed_post`` was called. The
  helper is the canonical answer to "did this set of middlewares actually
  wire up so the request reaches the transport?" and is used by every
  middleware PR's wire-up smoke test.

See ``docs/adr/0009-middleware-chain.md`` for the chain contract and
``.sisyphus/plans/tier-12-13-greenfield-migration.md`` section 2 for the
PR sequence.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any

import httpx

from notebooklm._middleware import (
    Middleware,
    RpcRequest,
    RpcResponse,
    build_chain,
)
from notebooklm._request_types import AuthSnapshot, BuildRequest


class FakeAuthedPost:
    """Programmable stub for ``AuthedTransport.perform_authed_post``.

    Matches the production callable's keyword-only signature (``build_request``,
    ``log_label``, ``disable_internal_retries``) so a terminal that adapts
    :class:`RpcRequest` into a transport call can target this fake without
    branching on "is this a real transport or a fake."

    Records every call into :attr:`calls` (a list of ``dict`` snapshots) so
    tests can assert both *that* the transport was reached and *what*
    arguments arrived. The default return is a minimal
    :class:`httpx.Response` with status 200 and an empty body (no attached
    ``.request``, unlike the production buffered response from
    ``stream_post_with_size_cap``) — enough for middlewares that just
    observe and pass through.

    Override the return value by setting :attr:`response` (single fixed
    response) or :attr:`response_factory` (callable producing per-call
    responses). If :attr:`raises` is set, the call raises that exception
    instead.
    """

    def __init__(
        self,
        *,
        response: httpx.Response | None = None,
        response_factory: Callable[[], httpx.Response] | None = None,
        raises: BaseException | None = None,
    ) -> None:
        self.response: httpx.Response | None = response
        self.response_factory: Callable[[], httpx.Response] | None = response_factory
        self.raises: BaseException | None = raises
        self.calls: list[dict[str, Any]] = []

    @property
    def was_called(self) -> bool:
        """``True`` if :meth:`perform_authed_post` was called at least once."""
        return bool(self.calls)

    @property
    def call_count(self) -> int:
        """Number of times :meth:`perform_authed_post` was called."""
        return len(self.calls)

    async def perform_authed_post(
        self,
        *,
        build_request: BuildRequest,
        log_label: str,
        disable_internal_retries: bool = False,
    ) -> httpx.Response:
        """Record the call and return the configured response.

        Signature mirrors
        :meth:`notebooklm._authed_transport.AuthedTransport.perform_authed_post`
        verbatim (keyword-only ``build_request`` / ``log_label`` /
        ``disable_internal_retries``). ``build_request`` is typed as
        :data:`BuildRequest` so a signature drift in the real transport
        surfaces as a mypy error on the fake too.
        """
        self.calls.append(
            {
                "build_request": build_request,
                "log_label": log_label,
                "disable_internal_retries": disable_internal_retries,
            }
        )

        # Resolution priority: ``raises`` (highest — exception preempts everything
        # else, but the call is *already recorded above* so tests can still
        # assert ``call_count == 1``) → ``response_factory`` (per-call dynamic)
        # → ``response`` (single canned value) → built-in 200/empty default.
        if self.raises is not None:
            raise self.raises

        if self.response_factory is not None:
            return self.response_factory()

        if self.response is not None:
            return self.response

        # Default: a benign 200 OK with an empty body. Constructed lazily so
        # tests that supply their own ``response`` / ``response_factory``
        # never pay the construction cost, and so the default Response is
        # a fresh instance per call rather than a shared one.
        return httpx.Response(status_code=200, content=b"")


def make_request(**overrides: Any) -> RpcRequest:
    """Build an :class:`RpcRequest` with benign defaults plus overrides.

    Default-shape fields:

    - ``url`` — a placeholder ``batchexecute`` URL (no live network call —
      this is a chain-input fixture, never reaches the wire).
    - ``headers`` — a minimal headers dict.
    - ``body`` — empty bytes.
    - ``context`` — an empty dict (each call returns a fresh dict so tests
      don't share mutable state).

    Passing an unknown keyword raises ``TypeError`` early so test typos
    don't silently no-op.
    """
    defaults: dict[str, Any] = {
        "url": "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute?authuser=0&_reqid=100000",
        "headers": {"X-Goog-AuthUser": "0"},
        "body": b"",
        # Fresh dict per call so independent invocations don't share state.
        "context": {},
    }

    unknown = set(overrides) - set(defaults)
    if unknown:
        raise TypeError(
            "make_request() got unexpected keyword(s): "
            f"{sorted(unknown)!r}. Known fields: {sorted(defaults)!r}"
        )

    defaults.update(overrides)
    return RpcRequest(**defaults)


def chain_calls_through_to_authed_post(
    transport: FakeAuthedPost,
    middlewares: Sequence[Middleware],
) -> bool:
    """Return ``True`` iff invoking the chain reaches the transport leaf.

    Builds a chain with ``middlewares`` wrapped around a terminal that
    adapts :class:`RpcRequest` into a
    :meth:`FakeAuthedPost.perform_authed_post` call (reading
    ``build_request`` / ``log_label`` / ``disable_internal_retries`` from
    ``request.context``), invokes the chain with a default
    :func:`make_request`, and reports whether the transport recorded any
    call.

    This is the canonical wire-up smoke test for every middleware PR
    12.3–12.8: extracts a middleware, asserts
    ``chain_calls_through_to_authed_post(fake_transport, [new_middleware])
    is True``. A short-circuiting middleware (one that returns without
    calling ``next_call``) is the only legitimate way for this to return
    ``False``; in the Tier-12 set, no production middleware does that.

    The helper synchronously drives the chain with :func:`asyncio.run` so
    callers don't need to mark their test as ``async`` just to use the
    helper. Tests that need finer control over the event loop or that want
    to assert on the chain's return value can drop the helper and call
    :func:`build_chain` directly — that's exactly what the
    :mod:`test_chain_fixtures` and per-middleware tests do.
    """

    # Default ``build_request`` returns the same triple every call (consistent
    # url/body across retry attempts). Production callers (PR 12.2's chain
    # leaf) will pass a real ``BuildRequest`` via
    # ``request.context["build_request"]``; this fallback keeps the helper
    # usable with a bare ``make_request()``.
    def _default_build_request(
        snapshot: AuthSnapshot,
    ) -> tuple[str, bytes, dict[str, str] | None]:
        return ("https://fake/build-request-fallback", b"", None)

    async def terminal(request: RpcRequest) -> RpcResponse:
        ctx = request.context
        build_request = ctx.get("build_request", _default_build_request)
        log_label = ctx.get("log_label", "fake-log-label")
        disable_internal_retries = bool(ctx.get("disable_internal_retries", False))
        response = await transport.perform_authed_post(
            build_request=build_request,
            log_label=log_label,
            disable_internal_retries=disable_internal_retries,
        )
        return RpcResponse(response=response, context=ctx)

    chain = build_chain(middlewares, terminal)

    async def driver() -> RpcResponse:
        return await chain(make_request())

    # ``asyncio.run`` raises if there's already a running loop. The fixture
    # is meant to be called from synchronous test bodies; tests that need
    # to invoke the chain from an async context should compose
    # ``build_chain`` + ``make_request`` directly.
    asyncio.run(driver())
    return transport.was_called


__all__ = [
    "FakeAuthedPost",
    "chain_calls_through_to_authed_post",
    "make_request",
]
