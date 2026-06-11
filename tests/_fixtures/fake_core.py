"""``make_fake_core`` factory — constructor-injection substrate for sub-clients.

This module provides a single entry point — :func:`make_fake_core` — that
returns a ``FakeSession`` instance shaped to satisfy the **shared
capability Protocols** in :mod:`notebooklm._runtime.contracts`
(``RpcCaller``, ``LoopGuard``, ``Kernel``) plus the single-consumer
Protocols inlined into their owning feature modules in issue #1327
(``AuthMetadata`` in ``notebooklm._source.upload``,
``OperationScopeProvider`` in ``notebooklm._artifact.polling``). Feature APIs that
need more than one capability take their direct collaborators by
keyword-only constructor argument (``ChatAPI`` in ``notebooklm._chat.api``,
``ArtifactsAPI`` in ``_artifacts.py``, ``SourceUploadPipeline`` in
``notebooklm._source.upload``); the feature-local composite Protocols
``ArtifactsRuntime`` and ``UploadRuntime`` (and their adapter
dataclasses) were retired once it was clear they only hid three stable
collaborators with one production satisfier. (``ChatRuntime`` was
deleted earlier on the same grounds — ADR-0014 Rule 2 Corollary.) The
``RpcCaller`` surface is exposed two ways: directly as
``fake.rpc_call`` (legacy single-attribute access path that some tests
still use) AND as ``fake.rpc_executor.rpc_call`` mirroring the
production composition where ``NotebookLMClient`` stores
``composed.executor`` as ``self._rpc_executor`` and passes it to every
feature API. Both attributes are wired to the same underlying mock so
``fake.rpc_call.assert_awaited`` and
``fake.rpc_executor.rpc_call.assert_awaited`` observe the same calls.
Tests pass the result to a sub-client constructor (e.g.
``NotebooksAPI(fake.rpc_executor)``) instead of constructing a real
client/runtime stack and mutating its attributes after the fact.

Phase 7 (refactor-history.md §Migration Plan step 10) deleted the broad
``Session`` Protocol that this factory's defaults dict previously
mirrored member-for-member. The dict now lists only the attribute slots
features actually exercise — promoting an attribute requires a real
test-site consumer, mirroring the ADR-0013 promotion criterion for
shared Protocols.

See :doc:`docs/adr/0007-test-monkeypatch-policy.md` for the policy that
makes this factory the only sanctioned substitute for the forbidden
``monkeypatch.setattr("notebooklm.…")`` and
``target.rpc_call = AsyncMock(…)`` patterns.

Design choices (documented in ADR-0007 "Alternatives considered"):

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

    The historical ``rpc_call=`` keyword is preserved as a convenience —
    it is unwrapped into ``rpc_executor=SimpleNamespace(rpc_call=<value>)``
    so the live ``RpcCaller`` Protocol surface on the fake matches the
    production shape (``NotebookLMClient.__init__`` stores
    ``composed.executor`` as ``self._rpc_executor`` and passes it to
    every feature API).

    Example::

        fake = make_fake_core(rpc_call=AsyncMock(return_value=[payload]))
        api = NotebooksAPI(fake.rpc_executor)
        result = await api.list()
        fake.rpc_executor.rpc_call.assert_awaited_once()
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
    # the attribute — mirroring the ADR-0013 promotion criterion for
    # shared Protocols (≥2 consumers).
    # ``rpc_call`` is shared between the direct ``fake.rpc_call`` and the
    # ``fake.rpc_executor.rpc_call`` mirror so both attribute paths see
    # the same observed calls. Fresh list per call so tests can mutate
    # the response without bleeding into siblings.
    rpc_call_mock = AsyncMock(side_effect=lambda *a, **kw: [])

    defaults: dict[str, Any] = {
        # AuthMetadata + Kernel — consumed by SourceUploadPipeline test sites.
        "auth": auth,
        "kernel": kernel,
        # RpcCaller — every feature API uses this. The fake exposes the
        # executor as a SimpleNamespace mirror so test sites address it
        # the same way production code does (``fake.rpc_executor.rpc_call``
        # mirrors ``client._rpc_executor.rpc_call``); the direct
        # ``rpc_call`` attribute is kept for legacy single-attribute test sites
        # that still treat the fake as a single bag-of-attributes.
        "rpc_call": rpc_call_mock,
        "rpc_executor": SimpleNamespace(rpc_call=rpc_call_mock),
        # LoopGuard + OperationScopeProvider (the latter lives in
        # ``notebooklm._artifact.polling`` after #1327) — used by ArtifactsAPI polling
        # and SourceUploadPipeline.
        "assert_bound_loop": MagicMock(return_value=None),
        "operation_scope": MagicMock(side_effect=_operation_scope),
        # DrainHookRegistration (local in ``_artifacts.py``) — close-time
        # hook the artifacts runtime registers against in
        # ``ArtifactsAPI.__init__``. Wave 2 of session-decoupling moved
        # the storage onto ``TransportDrainTracker`` (ADR-0014 Rule 1); we
        # keep ``_drain_hooks`` as a public attribute on the fake so test
        # sites that previously read ``fake._drain_hooks["name"]`` still
        # work (the fake doesn't have a real ``_drain_tracker``).
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

    # Convenience: ``rpc_call=AsyncMock(...)`` overrides BOTH the direct
    # ``rpc_call`` attribute AND the ``rpc_executor.rpc_call`` mirror with
    # the same mock so test idioms using either path observe the same
    # interactions.
    if "rpc_call" in overrides:
        overrides["rpc_executor"] = SimpleNamespace(rpc_call=overrides["rpc_call"])

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
