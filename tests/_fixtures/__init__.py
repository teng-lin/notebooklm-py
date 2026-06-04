"""Constructor-injection factories for unit and integration tests.

This subpackage is the canonical replacement for the ``monkeypatch.setattr(...)``
+ ``core.X = AsyncMock(...)`` gravity well documented in ADR-0007. New tests
acquire collaborators through ``make_fake_core(**overrides)`` rather than
mutating production modules from the outside.

Import style from inside a test file (pytest adds ``tests/`` to ``sys.path``)::

    from _fixtures import make_fake_core

See ``docs/adr/0007-test-monkeypatch-policy.md`` for the policy and rationale.
"""

from __future__ import annotations

from .cli_session import patch_session_login_dual
from .fake_core import FakeSession, make_fake_core
from .kernel_test_helpers import install_http_client_for_test

__all__ = [
    "FakeSession",
    "install_http_client_for_test",
    "make_fake_core",
    "patch_session_login_dual",
]
