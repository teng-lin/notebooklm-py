"""Internal helpers for emitting the project's ``DeprecationWarning`` family.

Centralises the one-off ``warnings.warn`` calls so the message text, the
``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression gate, and the ``stacklevel``
bookkeeping live in a single, tested place instead of being copy-pasted at
every deprecated call site.

This is an implementation module. There is no public surface here; the public
deprecation *policy* (what is deprecated, since when, removal target) is
documented in ``docs/deprecations.md``.

Two families live here:

* ``warn_get_returns_none`` — marks ``<resource>.get()`` returning ``None`` on
  a miss as deprecated (issue #1247).
* ``deprecated_kwarg`` — the keyword-alias pattern used when a public method
  renames a parameter but keeps the old name working for one MINOR cycle. The
  canonical case is the wait/poll timeout standardization (issue #1208):
  ``ResearchAPI.wait_for_completion`` renamed ``interval`` to
  ``initial_interval`` (matching ``SourcesAPI.wait_until_ready`` /
  ``ArtifactsAPI.wait_for_completion``) and accepts the old name as a
  deprecated alias removed in v0.8.0.

Both families share the single ``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression
gate (read live, never cached) and a parameterized ``stacklevel`` so the
warning's ``filename``/``lineno`` point at the *user's* call site. The warning
message always names the removal version (so ``scripts/check_deprecation_targets.py``
can verify the shipping release never names *itself* as the removal target), and
passing BOTH the old and new keyword raises :class:`TypeError` rather than
silently preferring one.
"""

from __future__ import annotations

import os
import warnings
from typing import TypeVar

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

# Canonical removal target for the kwarg aliases introduced by issue #1208.
# Kept as a module constant so the message text and the docs stay in lockstep
# and the release gate has a single string to scan. Warns in 0.7.0, removed in
# 0.8.0.
DEFAULT_REMOVAL = "0.8.0"

_T = TypeVar("_T")


def _deprecations_quiet() -> bool:
    """Return ``True`` when deprecation warnings are suppressed via env var."""
    raw = os.environ.get(_QUIET_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def deprecations_quiet() -> bool:
    """Public alias for :func:`_deprecations_quiet`.

    ``NOTEBOOKLM_QUIET_DEPRECATIONS=1`` (or any truthy ``1``/``true``/``yes``/
    ``on`` spelling, case-insensitive) silences the ``DeprecationWarning``
    emitted by :func:`deprecated_kwarg`. Any other value — including unset —
    leaves the warning enabled.
    """
    return _deprecations_quiet()


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


def deprecated_kwarg(
    old_value: _T | None,
    new_value: _T | None,
    *,
    old: str,
    new: str,
    owner: str,
    removal: str = DEFAULT_REMOVAL,
    sentinel: object = None,
    stacklevel: int = 3,
) -> _T | None:
    """Resolve a renamed keyword, warning if the deprecated name was used.

    Maps a deprecated keyword (``old``) onto its replacement (``new``) for a
    single public method. Returns the value that the method should actually
    use, after emitting a :class:`DeprecationWarning` when (and only when) the
    caller passed the deprecated name.

    Args:
        old_value: The value the caller passed for the deprecated keyword, or
            ``sentinel`` when the caller did not pass it.
        new_value: The value the caller passed for the canonical keyword, or
            ``sentinel`` when the caller did not pass it.
        old: Name of the deprecated keyword (for messages), e.g. ``"interval"``.
        new: Name of the canonical replacement keyword, e.g.
            ``"initial_interval"``.
        owner: Human-readable owner of the parameter for the warning message,
            e.g. ``"ResearchAPI.wait_for_completion"``.
        removal: Version in which the deprecated keyword is removed. Defaults
            to v0.8.0. Named in the warning text so the release gate can verify
            it is never the shipping version.
        sentinel: The "not provided" marker for both ``old_value`` and
            ``new_value``. Defaults to ``None``; pass a private sentinel object
            when ``None`` is itself a meaningful value.
        stacklevel: ``warnings.warn`` stacklevel. The default of ``3`` points
            the warning at the caller of the public method (caller →
            public method → this helper). Adjust when the helper is invoked
            through additional wrapper frames.

    Returns:
        ``new_value`` when the caller used the canonical keyword; ``old_value``
        when the caller used the deprecated keyword (after warning); otherwise
        ``sentinel`` (neither provided — the method keeps its own default).

    Raises:
        TypeError: If the caller passed BOTH the deprecated and the canonical
            keyword. They name the same concept, so two values is ambiguous.
    """
    new_provided = new_value is not sentinel
    old_provided = old_value is not sentinel

    if old_provided and new_provided:
        raise TypeError(
            f"{owner}() received both {new!r} and the deprecated alias {old!r}; pass only {new!r}."
        )

    if old_provided:
        if not _deprecations_quiet():
            warnings.warn(
                (
                    f"{owner}({old}=...) is deprecated and will be removed in "
                    f"v{removal}; use {new}=... instead (same behavior). "
                    f"Set {_QUIET_ENV_VAR}=1 to silence this warning."
                ),
                DeprecationWarning,
                stacklevel=stacklevel,
            )
        return old_value

    # Neither provided (``new_value`` already equals ``sentinel``) or only the
    # canonical keyword was passed: return it directly so the static type stays
    # ``_T | None`` rather than the widened ``object`` of ``sentinel``.
    return new_value
