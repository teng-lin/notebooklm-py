"""Pytest fixtures wrapping :mod:`tests._fixtures.fake_core`.

This file deliberately exposes a small surface — exactly two fixtures —
to satisfy the hybrid factory-first design recorded in ADR-0007 (see
"Alternatives considered: pure pytest fixtures"). A larger menu of
parametric fixtures was rejected because the combinatorial explosion of
``fake_core_with_X_returning_Y`` shapes scales worse than the
``monkeypatch.setattr`` sprawl the policy is replacing.

Tests can either:

1. Use the ``fake_core`` fixture directly when a vanilla
   defaults-only stand-in is enough::

       async def test_list_uses_rpc(fake_core):
           fake_core.rpc_executor.rpc_call.return_value = [payload]
           api = NotebooksAPI(fake_core.rpc_executor)
           ...

2. Use the ``make_fake_core`` fixture (the factory itself) when each
   test wants to express its overrides at the call site::

       async def test_list_uses_rpc(make_fake_core):
           fake = make_fake_core(rpc_call=AsyncMock(return_value=[payload]))
           api = NotebooksAPI(fake.rpc_executor)
           ...

3. Or, equivalently, ``from _fixtures import make_fake_core`` and skip
   the fixture indirection entirely (pytest discovery puts ``tests/`` on
   ``sys.path``, so the underscore-prefixed module is importable). The
   factory is the load-bearing substrate; the fixtures are convenience
   wrappers.

To make these fixtures available across the whole test tree (rather than
only in ``tests/_fixtures/``), a follow-up PR can register this module as
a pytest plugin from the root ``tests/conftest.py`` via
``pytest_plugins = ["_fixtures.conftest"]``. D1 PR-1 deliberately leaves
the wiring opt-in so test files in D1 PR-2 and D1 PR-3 can exercise
either invocation style without forcing a global change here.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from . import fake_core as _fake_core_module
from .fake_core import FakeSession


@pytest.fixture
def fake_core() -> FakeSession:
    """A default-shaped :class:`FakeSession` for tests that need a stand-in."""
    return _fake_core_module.make_fake_core()


@pytest.fixture
def make_fake_core() -> Callable[..., FakeSession]:
    """Return the :func:`tests._fixtures.fake_core.make_fake_core` factory.

    Lets a test express its overrides inline at the call site rather than
    mutating a fixture-provided default after the fact.
    """

    def _factory(**overrides: Any) -> FakeSession:
        return _fake_core_module.make_fake_core(**overrides)

    return _factory
