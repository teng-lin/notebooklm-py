"""Internal helpers for emitting the project's ``DeprecationWarning`` family.

Centralises the one-off ``warnings.warn`` calls so the message text, the
``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression gate, and the ``stacklevel``
bookkeeping live in a single, tested place instead of being copy-pasted at
every deprecated call site.

This is an implementation module. There is no public surface here; the public
deprecation *policy* (what is deprecated, since when, removal target) is
documented in ``docs/deprecations.md``.
"""

from __future__ import annotations

import os
import warnings

# Suppression gate. Setting ``NOTEBOOKLM_QUIET_DEPRECATIONS`` to a truthy value
# silences the warnings emitted through this module. This re-activates the
# historically-documented env var (``docs/configuration.md``) for the new
# get()-returns-None deprecation; it is intentionally read live (not cached) so
# tests and callers can toggle it per call.
_QUIET_ENV_VAR = "NOTEBOOKLM_QUIET_DEPRECATIONS"

# Follow-up issue tracking the actual breaking flip in v0.8.0, where these
# ``get()`` methods stop returning ``None`` and start raising the relevant
# ``*NotFoundError``. Referenced in the warning message and in
# ``docs/deprecations.md`` so callers can find the migration guidance.
GET_RETURNS_NONE_FLIP_ISSUE = 1247


def _deprecations_quiet() -> bool:
    """Return ``True`` when deprecation warnings are suppressed via env var."""
    raw = os.environ.get(_QUIET_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _not_found_error_exists(exc_name: str) -> bool:
    """Return ``True`` if ``exc_name`` is already defined in ``exceptions``.

    Lazy/local import keeps ``_deprecation`` free of a module-load-time
    dependency on ``exceptions`` (which would risk an import cycle). Used only
    to decide whether the migration hint can name the exception unqualified.
    """
    from . import exceptions

    return hasattr(exceptions, exc_name)


def warn_get_returns_none(resource: str, *, removal: str = "0.8.0") -> None:
    """Warn that ``<resource>.get()`` returning ``None`` on a miss is deprecated.

    ``sources.get`` / ``artifacts.get`` / ``notes.get`` currently return
    ``None`` when the entity is not found, while ``notebooks.get`` raises
    :class:`~notebooklm.exceptions.NotebookNotFoundError`. This warning marks
    the ``None``-returning behavior as deprecated; in **v0.8.0** these methods
    will instead raise the relevant ``*NotFoundError`` (tracked by issue
    #1247), unifying the not-found contract across all four ``get()`` methods.

    The warning fires only on a *miss* (when the method is about to return
    ``None``); successful lookups stay silent. It is suppressible by setting
    ``NOTEBOOKLM_QUIET_DEPRECATIONS`` to a truthy value.

    Args:
        resource: Singular resource name for the message, e.g. ``"source"``,
            ``"artifact"``, or ``"note"``. Used to name the matching
            ``<Resource>NotFoundError`` in the migration hint.
        removal: Stated removal/flip version (default ``"0.8.0"``). Kept as a
            parameter so the message and the release-gate
            (``scripts/check_deprecation_targets.py``) share one source of
            truth.
    """
    if _deprecations_quiet():
        return

    exc_name = f"{resource.capitalize()}NotFoundError"
    # SourceNotFoundError / ArtifactNotFoundError already exist and are
    # importable today, but NoteNotFoundError is only introduced by the v0.8.0
    # flip (#1247). Qualify the hint so a notes caller who follows the migration
    # advice immediately doesn't hit an ImportError on a not-yet-defined class.
    exc_hint = (
        exc_name if _not_found_error_exists(exc_name) else f"{exc_name} (added in v{removal})"
    )
    message = (
        f"{resource}s.get() returning None for a missing {resource} is "
        f"deprecated and will be removed in v{removal}: in v{removal} it will "
        f"raise {exc_name} instead (issue "
        f"#{GET_RETURNS_NONE_FLIP_ISSUE}). To keep handling missing "
        f"{resource}s, wrap the call in try/except {exc_hint}."
    )
    # stacklevel=3: warn_get_returns_none (1) -> the public get() (2) ->
    # the user's call site (3). Points the warning's filename/lineno at the
    # caller that wrote ``await client.<resource>s.get(...)``.
    warnings.warn(message, DeprecationWarning, stacklevel=3)
