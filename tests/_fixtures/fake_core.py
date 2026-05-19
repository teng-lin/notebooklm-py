"""``make_fake_core`` factory — constructor-injection substrate for sub-clients.

This module provides a single entry point — :func:`make_fake_core` — that
returns a ``FakeClientCore`` instance shaped to satisfy every narrow
capability Protocol in :mod:`notebooklm._capabilities`. Tests pass the
result to a sub-client constructor (``NotebooksAPI(core=fake)``) instead
of constructing a real ``ClientCore`` and mutating its attributes after
the fact.

See :doc:`docs/adr/0007-test-monkeypatch-policy.md` for the policy that
makes this factory the only sanctioned substitute for the forbidden
``monkeypatch.setattr("notebooklm.…")`` and ``core.rpc_call = AsyncMock(…)``
patterns.

Design choices (documented in ADR-007 "Alternatives considered"):

- ``FakeClientCore`` is a plain class with explicit attribute storage
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

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import httpx


class FakeClientCore:
    """A duck-typed stand-in for ``ClientCore`` collaborators in tests.

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


def make_fake_core(**overrides: Any) -> FakeClientCore:
    """Return a :class:`FakeClientCore` with benign defaults overridden.

    All overrides are keyword-only and replace the corresponding default.
    Passing an unknown keyword raises ``TypeError`` early so test typos
    don't silently no-op.

    Example::

        fake = make_fake_core(rpc_call=AsyncMock(return_value=[payload]))
        api = NotebooksAPI(core=fake)
        result = await api.list()
        fake.rpc_call.assert_awaited_once()
    """

    defaults: dict[str, Any] = {
        # CoreRPCProvider — fresh list per call so tests can mutate without bleeding
        "rpc_call": AsyncMock(side_effect=lambda *a, **kw: []),
        # SourceListProvider
        "get_source_ids": AsyncMock(side_effect=lambda *a, **kw: []),
        # CoreReqIdProvider — both the public name and the underscore alias
        "next_reqid": AsyncMock(return_value=100000),
        "_next_reqid": AsyncMock(return_value=100000),
        # PollRegistryProvider
        "poll_registry": MagicMock(),
        # AuthRouteProvider — sync helpers, used during request build
        "authuser": 0,
        "account_email": None,
        "authuser_query": MagicMock(return_value="authuser=0"),
        "authuser_header": MagicMock(return_value="0"),
        # CookieJarProvider
        "live_cookies": MagicMock(return_value=httpx.Cookies()),
        # TransportOperationProvider — fresh token object per call so drain tracking
        # gets unique identities (return_value=object() would share one instance).
        # The Protocol declares the underscore-private names that ClientCore
        # exposes directly; the no-underscore aliases below are preserved as
        # forward-compat shims so any legacy callers that still reach for the
        # adapter-era names land on benign mocks rather than AttributeError.
        "_begin_transport_post": AsyncMock(side_effect=lambda *a, **kw: object()),
        "_begin_transport_task": AsyncMock(side_effect=lambda *a, **kw: object()),
        "_finish_transport_post": AsyncMock(return_value=None),
        "_perform_authed_post": AsyncMock(),
        "begin_transport_post": AsyncMock(side_effect=lambda *a, **kw: object()),
        "begin_transport_task": AsyncMock(side_effect=lambda *a, **kw: object()),
        "finish_transport_post": AsyncMock(return_value=None),
        # UploadConcurrencyProvider — Semaphore must be created inside a running loop
        # (Python 3.10+ raises RuntimeError otherwise); defer via side_effect.
        "get_upload_semaphore": MagicMock(side_effect=lambda: asyncio.Semaphore(1)),
        "record_upload_queue_wait": MagicMock(return_value=None),
        # LoopAffinityProvider — None is the silent-no-op value
        "bound_loop": None,
        # Auth-route helper alias
        "_route_url": MagicMock(return_value="https://notebooklm.google.com/_/.../batchexecute"),
    }

    # Validate overrides early so a typo like ``rpc_cal=`` fails loudly
    # rather than landing as an unread attribute.
    unknown = set(overrides) - set(defaults)
    if unknown:
        raise TypeError(
            "make_fake_core() got unexpected keyword(s): "
            f"{sorted(unknown)!r}. Known attributes: {sorted(defaults)!r}"
        )

    defaults.update(overrides)
    return FakeClientCore(**defaults)
