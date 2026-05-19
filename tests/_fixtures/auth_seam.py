"""Test helper for patching values across the ``_auth/*`` split.

Replaces the ``_AuthFacadeModule`` write-through previously installed in
``notebooklm.auth`` (retired in D1 PR-2 alongside ADR-003 supersedence).
Production code no longer mirrors ``setattr(notebooklm.auth, name, value)``
writes onto the ``_auth.<module>`` seams; tests that need that mirroring
behaviour now request it explicitly via :func:`patch_auth_seam`.

This helper exists to bridge a small set of legacy tests during the
remediation arc — it is NOT a long-lived replacement for the facade.
New tests should prefer constructor injection via
:mod:`tests._fixtures.fake_core`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import notebooklm._auth.account as _auth_account
import notebooklm._auth.cookie_policy as _auth_cookie_policy
import notebooklm._auth.cookies as _auth_cookies
import notebooklm._auth.keepalive as _auth_keepalive
import notebooklm._auth.refresh as _auth_refresh
import notebooklm._auth.storage as _auth_storage
import notebooklm.auth as _auth_mod

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch


# Every module on which a facade-mirrored name might live or alias from.
# ``patch_auth_seam`` walks this list and patches the name on every module
# that already binds it (via ``hasattr``) so the fan-out matches the
# retired ``_AuthFacadeModule.__setattr__`` mirroring tables without
# requiring a hand-maintained per-name target list.
#
# Order matters only for determinism in error messages; effectively the
# tuple is a set.
_SEAM_MODULES = (
    _auth_mod,
    _auth_storage,
    _auth_cookies,
    _auth_keepalive,
    _auth_refresh,
    _auth_account,
    _auth_cookie_policy,
)


def patch_auth_seam(monkeypatch: MonkeyPatch, name: str, value: Any) -> None:
    """Patch ``name`` onto every ``notebooklm.auth`` + ``_auth/*`` seam module that binds it.

    Mirrors the legacy ``setattr(notebooklm.auth, name, value)``
    write-through by directly patching each seam module that owns or
    aliases ``name`` at import time. The previous facade encoded these
    fan-out tables explicitly; this helper instead walks the known seam
    modules and patches any that already bind the attribute — the same
    invariant the facade relied on, expressed without a maintenance
    burden of per-name target lists.

    Raises :class:`AttributeError` if ``name`` is not bound on any seam
    module (so a typo in a test surfaces immediately rather than silently
    no-op'ing).
    """
    patched_any = False
    for module in _SEAM_MODULES:
        if hasattr(module, name):
            monkeypatch.setattr(module, name, value)
            patched_any = True
    if not patched_any:
        raise AttributeError(
            f"patch_auth_seam: name {name!r} is not bound on notebooklm.auth "
            f"or any _auth/* seam module — typo, or the seam moved? Add the "
            f"name to its owning seam module if this is intentional."
        )
