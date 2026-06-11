"""Shared schema-drift helper for indexing into decoded RPC payloads.

``safe_index`` walks a nested list/tuple by integer keys with strict
semantics: drift raises ``UnknownRPCMethodError`` so callers fail fast
when Google's response shape moves out from under us. The legacy
``NOTEBOOKLM_STRICT_DECODE=0`` warn-and-return-``None`` opt-out was
retired in v0.7.0 (see ``docs/adr/0011-schema-validation-policy.md``);
strict is now the only mode.

This is the single shared point of policy for "the payload didn't look like
we expected" — call sites should migrate to ``safe_index`` rather than
hand-rolling ``try/except IndexError`` blocks.
"""

from __future__ import annotations

import reprlib
from typing import Any

from ..exceptions import UnknownRPCMethodError

__all__ = ["safe_index"]

_REPR_TRUNCATE = 200

# Use reprlib so we never materialize a huge repr just to slice it. Tune the
# knobs so the resulting representation stays close to ``repr(value)[:200]``
# semantics without recursing into giant inner structures.
_REPR = reprlib.Repr()
_REPR.maxstring = _REPR_TRUNCATE
_REPR.maxother = _REPR_TRUNCATE
_REPR.maxlist = 10
_REPR.maxtuple = 10
_REPR.maxdict = 10
_REPR.maxarray = 10
_REPR.maxset = 10
_REPR.maxfrozenset = 10
_REPR.maxdeque = 10
_REPR.maxlevel = 4


def _truncate(value: Any) -> str:
    """Return a length-bounded repr suitable for logs/exception attributes.

    Uses ``reprlib`` to avoid materialising the full repr of pathologically
    large/deep payloads before slicing.
    """
    text = _REPR.repr(value)
    if len(text) <= _REPR_TRUNCATE:
        return text
    return text[:_REPR_TRUNCATE] + "..."


def safe_index(
    data: Any,
    *path: int,
    method_id: str | int | None,
    source: str,
) -> Any:
    """Walk ``data`` by ``path`` indices with strict schema-drift handling.

    Args:
        data: Nested list/tuple structure (typically a decoded RPC payload).
        *path: Sequence of integer indices to descend.
        method_id: RPC method ID (for diagnostics on drift).
        source: Caller label identifying where the drift was observed
            (e.g. ``"_notebooks.list"``); included in logs and the raised
            exception's ``source`` attribute.

    Returns:
        The value at ``data[path[0]][path[1]]...`` on success.

    Raises:
        UnknownRPCMethodError: When descent fails — an out-of-range index, a
            non-indexable value (``None``/``int``), a missing key, or a
            ``str``/``bytes`` value at an intermediate hop (which is indexable
            but never a valid container, so descending it would silently yield a
            single character/byte instead of surfacing the shape drift). The
            exception carries ``method_id``, ``source``, ``path`` (truncated to
            where descent stopped), and a truncated ``data_at_failure`` repr.
    """
    current: Any = data
    for i, key in enumerate(path):
        # A str/bytes is indexable but is NEVER a valid container at an
        # *intermediate* descent hop in a decoded RPC payload: ``"abc"[0]``
        # silently returns ``"a"`` instead of descending into a nested list,
        # which would smuggle a bogus single-character "value" past drift
        # detection (the payload shape has actually moved). Reject it as drift
        # before indexing. (A string is fine as the returned *leaf* — the loop
        # never indexes the final value.)
        if isinstance(current, (str, bytes, bytearray)):
            failing_path = tuple(path[:i])
            raise UnknownRPCMethodError(
                f"safe_index drift at path {failing_path}[{key}]: cannot index "
                f"into {type(current).__name__} (expected a nested list/tuple)",
                method_id=method_id,
                path=failing_path,
                source=source,
                data_at_failure=_truncate(current),
            )
        try:
            current = current[key]
        except (IndexError, TypeError, KeyError) as exc:
            failing_path = tuple(path[:i])
            data_repr = _truncate(current)
            # method_id/source are appended by UnknownRPCMethodError.__str__
            # via its structured fields — don't duplicate them in the
            # message text.
            raise UnknownRPCMethodError(
                f"safe_index drift at path {failing_path}[{key}]",
                method_id=method_id,
                path=failing_path,
                source=source,
                data_at_failure=data_repr,
            ) from exc
    return current
