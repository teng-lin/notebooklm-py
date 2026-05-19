"""Constructor-injection factories for unit and integration tests.

This subpackage is the canonical replacement for the ``monkeypatch.setattr(...)``
+ ``core.X = AsyncMock(...)`` gravity well documented in ADR-007. New tests
acquire collaborators through ``make_fake_core(**overrides)`` rather than
mutating production modules from the outside.

Import style from inside a test file (pytest adds ``tests/`` to ``sys.path``)::

    from _fixtures import make_fake_core

See ``docs/adr/0007-test-monkeypatch-policy.md`` for the policy and rationale.
"""

from __future__ import annotations

from .auth_seam import patch_auth_seam
from .cli_session import patch_session_login_dual
from .fake_core import FakeSession, make_fake_core

__all__ = [
    "FakeSession",
    "make_fake_core",
    "patch_auth_seam",
    "patch_session_login_dual",
]
