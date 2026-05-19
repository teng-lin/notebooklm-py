"""``make_fake_core`` factory — constructor-injection substrate for sub-clients.

This module provides a single entry point — :func:`make_fake_core` — that
returns a ``FakeSession`` instance shaped to satisfy the shared
``Session`` Protocol and explicit feature collaborators. Tests pass the
result to a sub-client constructor (``NotebooksAPI(core=fake)``) instead
of constructing a real ``Session`` and mutating its attributes after
the fact.

See :doc:`docs/adr/0007-test-monkeypatch-policy.md` for the policy that
makes this factory the only sanctioned substitute for the forbidden
``monkeypatch.setattr("notebooklm.…")`` and ``core.rpc_call = AsyncMock(…)``
patterns.

Design choices (documented in ADR-007 "Alternatives considered"):

- ``FakeSession`` is a plain class with explicit attribute storage
  (``types.SimpleNamespace``-shaped). It is *not* a spec-based
  ``MagicMock`` because spec-based mocks silently auto-vivify
  attributes and would tie the factory to a single concrete class
  shape rather than the open set of narrow Protocols.
- Async-surface defaults use :class:`unittest.mock.AsyncMock`;
  sync-surface defaults use :class:`unittest.mock.MagicMock`. Both are
  configured with benign return values so a test that only exercises one
  attribute does not have to define the others.
- Overrides are keyword-only — positional arguments would conflict with
  the ``**overrides`` extension point if new attributes are added later.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx


class FakeSession:
    """A duck-typed stand-in for ``Session`` collaborators in tests.

    Attribute storage is explicit (the constructor only sets what's
    passed in) so that accessing an attribute the production code does
    not actually use surfaces as a clear ``AttributeError`` rather than
    as a silent auto-vivified ``MagicMock``. The canonical schema lives
    in :func:`make_fake_core`'s ``defaults`` dict — one source of truth
    so the schema cannot drift between two declarations.

    Most tests should construct instances via :func:`make_fake_core`,
    which fills in benign defaults; direct construction is also
    supported when a test wants to assert that no defaults are read.
    """

    def __init__(self, **attrs: Any) -> None:
        for name, value in attrs.items():
            setattr(self, name, value)


def make_fake_core(**overrides: Any) -> FakeSession:
    """Return a :class:`FakeSession` with benign defaults overridden.

    All overrides are keyword-only and replace the corresponding default.
    Passing an unknown keyword raises ``TypeError`` early so test typos
    don't silently no-op.

    Example::

        fake = make_fake_core(rpc_call=AsyncMock(return_value=[payload]))
        api = NotebooksAPI(core=fake)
        result = await api.list()
        fake.rpc_call.assert_awaited_once()
    """

    def _operation_scope(_label: str):
        @asynccontextmanager
        async def scope() -> AsyncIterator[None]:
            yield None

        return scope()

    live_cookies = httpx.Cookies()
    fake_http_client = SimpleNamespace(cookies=live_cookies)
    auth = SimpleNamespace(authuser=0, account_email=None)
    kernel = SimpleNamespace(
        cookies=live_cookies,
        get_http_client=MagicMock(return_value=fake_http_client),
    )

    defaults: dict[str, Any] = {
        "auth": auth,
        "kernel": kernel,
        # Session — fresh list per call so tests can mutate without bleeding
        "rpc_call": AsyncMock(side_effect=lambda *a, **kw: []),
        "transport_post": AsyncMock(),
        # NotebookSourceLister
        "get_source_ids": AsyncMock(side_effect=lambda *a, **kw: []),
        "next_reqid": AsyncMock(return_value=100000),
        "_next_reqid": AsyncMock(return_value=100000),
        # Legacy Session compatibility bridge
        "poll_registry": MagicMock(),
        # DrainHookRegistration
        "_drain_hooks": {},
        "register_drain_hook": MagicMock(return_value=None),
        # Auth routing — used by SourceUploadPipeline tests and compatibility paths
        "authuser": 0,
        "account_email": None,
        "authuser_query": MagicMock(return_value="authuser=0"),
        "authuser_header": MagicMock(return_value="0"),
        "get_http_client": MagicMock(return_value=fake_http_client),
        "live_cookies": MagicMock(return_value=live_cookies),
        # Legacy transport drain helpers — fresh token object per call so drain tracking
        # gets unique identities (return_value=object() would share one instance).
        # The Protocol declares the underscore-private names that Session
        # exposes directly. The no-underscore aliases below are purely defensive
        # safety-net defaults — no test site currently calls them on a
        # FakeSession instance (all no-underscore callers in the test tree
        # invoke these on TransportDrainTracker, not FakeSession). Kept so a
        # stray legacy reference lands on a benign mock rather than AttributeError.
        "_begin_transport_post": AsyncMock(side_effect=lambda *a, **kw: object()),
        "_begin_transport_task": AsyncMock(side_effect=lambda *a, **kw: object()),
        "_finish_transport_post": AsyncMock(return_value=None),
        "_perform_authed_post": AsyncMock(),
        "begin_transport_post": AsyncMock(side_effect=lambda *a, **kw: object()),
        "begin_transport_task": AsyncMock(side_effect=lambda *a, **kw: object()),
        "finish_transport_post": AsyncMock(return_value=None),
        # Session.operation_scope / upload metrics compatibility
        "operation_scope": MagicMock(side_effect=_operation_scope),
        "record_upload_queue_wait": MagicMock(return_value=None),
        # Session loop affinity
        "bound_loop": None,
        "assert_bound_loop": MagicMock(return_value=None),
        # Auth-route helper alias
        "_route_url": MagicMock(return_value="https://notebooklm.google.com/_/.../batchexecute"),
    }

    def _register_drain_hook(name: str, hook: Any) -> None:
        defaults["_drain_hooks"][name] = hook

    defaults["register_drain_hook"] = MagicMock(side_effect=_register_drain_hook)
    defaults["get_http_client"].return_value.cookies = live_cookies

    # Validate overrides early so a typo like ``rpc_cal=`` fails loudly
    # rather than landing as an unread attribute.
    unknown = set(overrides) - set(defaults)
    if unknown:
        raise TypeError(
            "make_fake_core() got unexpected keyword(s): "
            f"{sorted(unknown)!r}. Known attributes: {sorted(defaults)!r}"
        )

    defaults.update(overrides)
    return FakeSession(**defaults)
