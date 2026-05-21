"""``make_fake_core`` factory — constructor-injection substrate for sub-clients.

This module provides a single entry point — :func:`make_fake_core` — that
returns a ``FakeSession`` instance shaped to satisfy the **shared
capability Protocols** in :mod:`notebooklm._session_contracts`
(``RpcCaller``, ``LoopGuard``, ``OperationScopeProvider``,
``AsyncWorkRuntime``, ``AuthMetadata``, ``Kernel``) plus the
feature-local runtime Protocols (``ChatRuntime``, ``ArtifactsRuntime``,
``UploadRuntime``) that compose them. Tests pass the result to a
sub-client constructor (``NotebooksAPI(fake)``) instead of constructing
a real ``Session`` and mutating its attributes after the fact.

Phase 7 (refactor-history.md §Migration Plan step 10) deleted the broad
``Session`` Protocol that this factory's defaults dict previously
mirrored member-for-member. The dict now lists only the attribute slots
features actually exercise — promoting an attribute requires a real
test-site consumer, mirroring the ADR-013 promotion criterion for
shared Protocols.

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
    """A duck-typed stand-in for capability-Protocol collaborators in tests.

    Named ``FakeSession`` for backward compatibility with the broad-
    ``Session``-era test sites; the class itself is just an explicit
    attribute bag and is not pinned to any single Protocol shape.

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
        api = NotebooksAPI(fake)
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

    # Phase 7 (refactor-history.md §Migration Plan step 10) shrunk this dict from
    # the broad-Session-era 25+ entries to the minimum set that satisfies
    # the post-refactor capability and feature-local runtime Protocols.
    # New entries should only be added when a real test site exercises
    # the attribute — mirroring the ADR-013 promotion criterion for
    # shared Protocols (≥2 consumers).
    defaults: dict[str, Any] = {
        # AuthMetadata + Kernel — consumed by SourceUploadPipeline test sites.
        "auth": auth,
        "kernel": kernel,
        # RpcCaller — every feature API uses this. Fresh list per call so
        # tests can mutate the response without bleeding into siblings.
        "rpc_call": AsyncMock(side_effect=lambda *a, **kw: []),
        # ChatRuntime — chat-only transport + reqid helpers consumed via
        # the chat composite runtime; kept here so the same FakeSession
        # can satisfy ChatRuntime, ArtifactsRuntime, and UploadRuntime
        # for tests that wire the bag through several sub-clients.
        "transport_post": AsyncMock(),
        "next_reqid": AsyncMock(return_value=100000),
        # AsyncWorkRuntime (LoopGuard + OperationScopeProvider) — used by
        # ArtifactsAPI polling and SourceUploadPipeline.
        "assert_bound_loop": MagicMock(return_value=None),
        "operation_scope": MagicMock(side_effect=_operation_scope),
        # DrainHookRegistration (local in ``_artifacts.py``) — close-time
        # hook the artifacts runtime registers against in
        # ``ArtifactsAPI.__init__``.
        "_drain_hooks": {},
        "register_drain_hook": MagicMock(return_value=None),
        # Upload-pipeline glue: queue-wait recorder consumed by the
        # ``SourceUploadPipeline`` upload metrics path. Kept on the bag
        # so test sites that wire a SourcesAPI + uploader pair against a
        # single FakeSession can rely on it.
        "record_upload_queue_wait": MagicMock(return_value=None),
        # NotebookSourceLister stub — exercised by ``test_notebooks.py``
        # paths that resolve source IDs through the lister collaborator.
        "get_source_ids": AsyncMock(side_effect=lambda *a, **kw: []),
    }

    def _register_drain_hook(name: str, hook: Any) -> None:
        defaults["_drain_hooks"][name] = hook

    defaults["register_drain_hook"] = MagicMock(side_effect=_register_drain_hook)

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
