"""Filesystem paths, env-var name constants, and lock-path computation for auth storage.

This module is **private** (note the ``_auth`` package prefix) and distinct
from the package-level :mod:`notebooklm.paths`, which owns the user-facing
storage-path / profile-resolution helpers (``get_storage_path``,
``resolve_profile``). The two intentionally share a name because both are
"path stuff", but their concerns don't overlap and they import each other at
most transitively via :mod:`notebooklm.auth`.

This module owns the environment-variable name constants that gate refresh /
keepalive behaviour and the helper that computes the rotation sentinel path
sibling to ``storage_state.json``. Centralising them here keeps the public
``notebooklm.auth`` surface compatible while the underlying logic lives in a
single, easy-to-audit module.

Three categories of names live here:

1. **Refresh command env vars** (``NOTEBOOKLM_REFRESH_CMD``,
   ``NOTEBOOKLM_REFRESH_CMD_USE_SHELL``, ``_NOTEBOOKLM_REFRESH_ATTEMPTED``)
   read by :func:`notebooklm.auth._run_refresh_cmd` and friends.
2. **Keepalive env var** (``NOTEBOOKLM_DISABLE_KEEPALIVE_POKE``) read by
   :func:`notebooklm._auth.keepalive._poke_session` / ``_rotate_cookies``.
   It is conceptually an environment-variable name, not a keepalive parameter,
   so it lives here with the other auth env-var constants.
3. **Path helpers** (:func:`_storage_state_lock_path`,
   :func:`_rotation_lock_path`) that compute sentinel sibling files alongside
   the user's storage-state path.

The two refresh env vars and the keepalive env var are part of the documented
public surface of ``notebooklm.auth`` (see :data:`notebooklm.auth.__all__`);
``notebooklm.auth`` re-exports them by name. ``_REFRESH_ATTEMPTED_ENV`` and
``_rotation_lock_path`` are private but accessed as white-box affordances by
tests, so they are also re-exported.
"""

from __future__ import annotations

from pathlib import Path

NOTEBOOKLM_REFRESH_CMD_ENV = "NOTEBOOKLM_REFRESH_CMD"
NOTEBOOKLM_REFRESH_CMD_USE_SHELL_ENV = "NOTEBOOKLM_REFRESH_CMD_USE_SHELL"
_REFRESH_ATTEMPTED_ENV = "_NOTEBOOKLM_REFRESH_ATTEMPTED"

NOTEBOOKLM_DISABLE_KEEPALIVE_POKE_ENV = "NOTEBOOKLM_DISABLE_KEEPALIVE_POKE"


def _storage_state_lock_path(storage_path: Path) -> Path:
    """Canonical sibling flock file shared by every ``storage_state.json`` writer.

    ``save_cookies_to_storage`` (cookie writes) and ``write_account_metadata`` /
    ``_clear_in_band_account`` (account-metadata writes) all mutate the same
    ``storage_state.json``, so they MUST serialize on the *same* lock file or a
    read-modify-write from one loses the other's update. Deriving the dotted
    ``.storage_state.json.lock`` path here keeps that contract enforced by
    construction instead of by hand-synced string literals in each caller.
    """
    return storage_path.with_name(f".{storage_path.name}.lock")


def _rotation_lock_path(storage_path: Path | None) -> Path | None:
    """Sibling sentinel used by ``_poke_session`` for cross-process coordination.

    Distinct from the ``.storage_state.json.lock`` used by ``save_cookies_to_storage``
    so a long-running save doesn't block rotations or vice versa.
    """
    if storage_path is None:
        return None
    return storage_path.with_name(f".{storage_path.name}.rotate.lock")
