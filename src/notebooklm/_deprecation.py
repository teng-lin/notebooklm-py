"""Internal helper for emitting the project's ``DeprecationWarning`` family.

Centralises the one-off ``warnings.warn`` calls so the message text, the
``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression gate, and the ``stacklevel``
bookkeeping live in a single, tested place instead of being copy-pasted at
every deprecated call site.

This is an implementation module. There is no public surface here; the public
deprecation *policy* (what is deprecated, since when, removal target) is
documented in ``docs/deprecations.md``.

``warn_deprecated`` is the generic gated primitive for one-off deprecations
(e.g. awaiting ``from_storage(...)``). It exists so ad-hoc deprecations have a
gated home rather than hand-rolling ``warnings.warn(...)`` and silently
bypassing the suppression switch (issue #1369). Note that not every inline
warning is a deprecation: ``save_cookies_to_storage(original_snapshot=None)``
emits a permanent ``RuntimeWarning`` race advisory (not a scheduled removal), so
it is emitted inline and is *not* routed through here.

It honors the single ``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression gate (read
live, never cached) and a parameterized ``stacklevel`` so the warning's
``filename``/``lineno`` point at the *user's* call site. When a removal version
is given the warning message names it, so
``scripts/check_deprecation_targets.py`` can verify the shipping release never
names *itself* as the removal target.

The v0.8.0 error contract (ADR-0019, umbrella #1346) retired the
``NOTEBOOKLM_FUTURE_ERRORS`` preview gate and the dict-subscript / get-returns-
None / kwarg-alias runway helpers that lived here through v0.7.0; the breaks
they previewed are now the default behavior. See ``docs/deprecations.md``.
"""

from __future__ import annotations

import os
import warnings

# Suppression gate. Setting ``NOTEBOOKLM_QUIET_DEPRECATIONS`` to a truthy value
# silences the warnings emitted through this module. It is intentionally read
# live (not cached) so tests and callers can toggle it per call.
_QUIET_ENV_VAR = "NOTEBOOKLM_QUIET_DEPRECATIONS"


def _deprecations_quiet() -> bool:
    """Return ``True`` when deprecation warnings are suppressed via env var."""
    raw = os.environ.get(_QUIET_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def deprecations_quiet() -> bool:
    """Public alias for :func:`_deprecations_quiet`.

    ``NOTEBOOKLM_QUIET_DEPRECATIONS=1`` (or any truthy ``1``/``true``/``yes``/
    ``on`` spelling, case-insensitive) silences the ``DeprecationWarning``
    emitted by :func:`warn_deprecated`. Any other value — including unset —
    leaves the warning enabled.
    """
    return _deprecations_quiet()


def warn_deprecated(message: str, *, removal: str | None = None, stacklevel: int = 3) -> None:
    """Emit a project ``DeprecationWarning``, honoring the suppression gate.

    The generic primitive for one-off deprecations. Routing every ad-hoc
    warning through here keeps the ``NOTEBOOKLM_QUIET_DEPRECATIONS`` gate and the
    ``DeprecationWarning`` category in one place — ADR-0018 rejects inline
    ``warnings.warn(...)`` calls scattered through feature modules precisely
    because they bypass this gate.

    No-ops when :func:`_deprecations_quiet` is true (i.e. when
    ``NOTEBOOKLM_QUIET_DEPRECATIONS`` is set to a truthy value); otherwise emits
    a single :class:`DeprecationWarning` with ``message``.

    Args:
        message: The full warning text. Callers own the wording (what is
            deprecated, what to use instead). When ``removal`` is given and the
            message does not already name that version, a sentence naming the
            removal version is appended so every gated warning states its
            removal target consistently.
        removal: Optional removal version, e.g. ``"1.0"`` or ``"0.8.0"``. Pass
            a version when one is scheduled; the version is ensured to appear in
            the emitted text. Pass ``None`` in two cases — the message is emitted
            verbatim for both: (a) a *permanent* back-compat shim that is never
            scheduled for removal, or (b) a deprecation that *will* be removed
            but has no pinned version yet (the message can still say "a future
            major release"). Always pass ``removal`` explicitly so a future
            reader patching the call to add a version knows where to put it.
        stacklevel: ``warnings.warn`` stacklevel. The default of ``3`` accounts
            for the single-hop case ``warn_deprecated`` (1) → the deprecated
            method/property (2) → the user's call site (3), so the warning's
            ``filename``/``lineno`` point at user code. Pass ``4`` (etc.) when an
            extra wrapper frame sits between the deprecated public surface and
            this helper (e.g. ``poll`` → ``_select_polled_tasks`` →
            ``warn_deprecated``). The default ``3`` is correct for any call made
            directly from the deprecated public surface; do not drop to ``2``,
            which would attribute the warning to the library's own line.
    """
    if _deprecations_quiet():
        return

    text = message
    # ``v{removal}`` is the precise spelling our messages use; the bare
    # ``removal`` fallback catches messages that name the version without the
    # ``v`` prefix. Both checks are substring matches — fine for the short,
    # single-sentence deprecation messages this helper emits (no version-looking
    # URLs or longer numbers), and only ever skip an otherwise-redundant append.
    if removal is not None and f"v{removal}" not in text and removal not in text:
        text = f"{text} It will be removed in v{removal}."
    warnings.warn(text, DeprecationWarning, stacklevel=stacklevel)
