"""Test helper for patching values across the ``_auth/*`` split.

Replaces the ``_AuthFacadeModule`` write-through previously installed in
``notebooklm.auth`` (retired in D1 PR-2 alongside ADR-003 supersedence).
Production code no longer mirrors ``setattr(notebooklm.auth, name, value)``
writes onto the ``_auth.<module>`` seams; tests that need that mirroring
behaviour now request it explicitly via :func:`patch_auth_seam`.

This helper exists to bridge a small set of legacy tests
(``test_auth_cookie_save_race.py``) during the remediation arc — it is
NOT a long-lived replacement for the facade. New tests should prefer
constructor injection via :mod:`tests._fixtures.fake_core`. The names
covered below match the bidirectional mirror table that
``_AuthFacadeModule.__setattr__`` enforced at the production-side seam.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import notebooklm.auth as _auth_mod
import notebooklm._auth.cookies as _auth_cookies
import notebooklm._auth.keepalive as _auth_keepalive
import notebooklm._auth.refresh as _auth_refresh
import notebooklm._auth.storage as _auth_storage

if TYPE_CHECKING:
    from _pytest.monkeypatch import MonkeyPatch
    from types import ModuleType


# Module-targets per name — matches the mirroring tables previously
# encoded in ``notebooklm.auth._AUTH_*_FACADE_NAMES`` /
# ``_REFRESH_DEP_MIRROR_NAMES`` / ``_KEEPALIVE_DEP_MIRROR_NAMES``.
#
# Each name fans out to (a) ``notebooklm.auth`` — many auth.py body
# functions (e.g. ``AuthTokens.from_storage``) do bare-name lookups
# against the module-level re-export captured at import time, so the
# patch must override that re-export — and (b) every ``_auth.*`` seam
# module that owns or aliases the name, because the ``_auth/*`` split
# binds the same callable as a local attribute on multiple seams (for
# example ``_auth.keepalive`` imports ``_file_lock`` from
# ``_auth.storage`` and body code resolves the bare name against the
# local copy).
_TARGETS: dict[str, tuple[tuple["ModuleType", str], ...]] = {
    # _AUTH_STORAGE_FACADE_NAMES entries actually consumed in tests
    "_cookie_is_http_only": (
        (_auth_mod, "_cookie_is_http_only"),
        (_auth_storage, "_cookie_is_http_only"),
        (_auth_cookies, "_cookie_is_http_only"),
    ),
    "save_cookies_to_storage": (
        (_auth_mod, "save_cookies_to_storage"),
        (_auth_storage, "save_cookies_to_storage"),
        (_auth_refresh, "save_cookies_to_storage"),
    ),
    "_FLOCK_UNAVAILABLE_WARNED": (
        (_auth_mod, "_FLOCK_UNAVAILABLE_WARNED"),
        (_auth_storage, "_FLOCK_UNAVAILABLE_WARNED"),
    ),
    "_file_lock": (
        (_auth_mod, "_file_lock"),
        (_auth_storage, "_file_lock"),
        (_auth_keepalive, "_file_lock"),
    ),
    # _AUTH_REFRESH_FACADE_NAMES
    "_run_refresh_cmd": (
        (_auth_mod, "_run_refresh_cmd"),
        (_auth_refresh, "_run_refresh_cmd"),
    ),
    "_fetch_tokens_with_refresh": (
        (_auth_mod, "_fetch_tokens_with_refresh"),
        (_auth_refresh, "_fetch_tokens_with_refresh"),
    ),
    "_fetch_tokens_with_jar": (
        (_auth_mod, "_fetch_tokens_with_jar"),
        (_auth_refresh, "_fetch_tokens_with_jar"),
    ),
}


def patch_auth_seam(monkeypatch: "MonkeyPatch", name: str, value: Any) -> None:
    """Patch ``name`` onto every ``_auth/*`` module that owns or aliases it.

    Mirrors the legacy ``setattr(notebooklm.auth, name, value)`` write-through
    by directly patching each seam module the old facade would have mirrored
    into. Use only in tests that the remediation arc has not yet migrated to
    constructor injection.

    Raises :class:`KeyError` if ``name`` is not in the known mirror table —
    that surfaces the missing-mapping early instead of silently no-op'ing.
    """
    for module, attr_name in _TARGETS[name]:
        monkeypatch.setattr(module, attr_name, value)
