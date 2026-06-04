"""Shared single-row-lookup helper for the public ``get`` / ``get_or_none`` pair.

ADR-0019 (error-and-return contract) makes resource absence an exception on
``get`` and reserves ``None``-on-miss for an explicit ``get_or_none``. Both share
the same underlying optional-lookup body; only their handling of a genuine miss
differs. :func:`unwrap_or_raise` is the one-line bridge that lets a namespace
keep its fully-typed, per-arity signatures while single-sourcing the
"None means missing" decision:

    note = unwrap_or_raise(
        await self.get_or_none(notebook_id, note_id),
        NoteNotFoundError(note_id),
    )

(The ``get()``-raises wiring lands with the v0.8.0 flip, issue #1247; this module
is the additive foundation it will build on — see ADR-0019 Enforcement tier-2.)
"""

from __future__ import annotations

from typing import TypeVar

from ._deprecation import future_errors_enabled, warn_get_returns_none

T = TypeVar("T")


def resolve_get(result: T | None, *, not_found: Exception, resource: str) -> T | None:
    """Resolve a public ``get()`` miss under the v0.7.0 / v0.8.0 error contract.

    Single-sources the warn-runway decision for every namespace ``get()``
    (``sources`` / ``artifacts`` / ``notes`` / ``mind_maps``) so the
    hand-duplicated ``if result is None: warn_get_returns_none(...)`` pattern
    can no longer drift between copies (the #1358-class bug). The behavior on a
    miss is gated:

    * ``result is not None`` — a hit. Returned unchanged; no warning, no raise.
    * miss + ``NOTEBOOKLM_FUTURE_ERRORS`` on — preview the v0.8.0 flip (#1247):
      ``not_found`` (the matching ``*NotFoundError``) is raised. This takes
      precedence over ``NOTEBOOKLM_QUIET_DEPRECATIONS``.
    * miss + future errors off — the v0.7.0 runway: emit the gated
      ``get()``-returns-``None`` ``DeprecationWarning`` and return ``None``.

    ``notebooks.get()`` already raises today and does **not** route through here.

    Args:
        result: The value returned by the namespace's ``get_or_none()`` lookup —
            the resolved entity, or ``None`` for a genuine miss. Transport/auth/
            decode faults are raised by the lookup before reaching here, so
            ``None`` means "not found" and nothing else.
        not_found: The ``*NotFoundError`` instance to raise on a miss when the
            future-errors preview is enabled.
        resource: Singular resource name for the warning message, e.g.
            ``"source"`` / ``"artifact"`` / ``"note"`` / ``"mind_map"``.

    Returns:
        ``result`` when it is a hit; ``None`` on a miss in the (default) warn
        runway.

    Raises:
        Exception: ``not_found`` itself, on a miss, when
            ``NOTEBOOKLM_FUTURE_ERRORS`` is enabled.
    """
    if result is not None:
        return result
    if future_errors_enabled():
        raise not_found
    # stacklevel=4: warn_get_returns_none (1) -> resolve_get (2) -> the public
    # get() (3) -> the user's call site (4). The extra bridge frame over the
    # historical inline ``warn_get_returns_none()`` call (which used the
    # default 3) keeps the warning pointed at the user's ``get()`` call.
    warn_get_returns_none(resource, stacklevel=4)
    return None


def unwrap_or_raise(obj: T | None, exc: Exception) -> T:
    """Return ``obj`` unchanged, or raise ``exc`` when ``obj`` is ``None``.

    The narrow contract is deliberate: callers pass the result of an
    optional-lookup (``get_or_none``) and the exception to raise on a genuine
    miss. The lookup itself owns re-raising transport/auth/decode faults, so by
    the time a value reaches here ``None`` means "not found" and nothing else.

    Args:
        obj: The looked-up value, or ``None`` when the resource was absent.
        exc: The exception instance to raise when ``obj`` is ``None``.

    Returns:
        ``obj`` narrowed to its non-``None`` type.

    Raises:
        Exception: ``exc`` itself, when ``obj`` is ``None``.
    """
    if obj is None:
        raise exc
    return obj
