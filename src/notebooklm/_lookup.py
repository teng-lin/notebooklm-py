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

Every namespace ``get()`` raises its matching ``*NotFoundError`` on a miss by
calling :func:`unwrap_or_raise` directly (the v0.8.0 flip, #1247 — see ADR-0019
Enforcement tier-2).
"""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


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
