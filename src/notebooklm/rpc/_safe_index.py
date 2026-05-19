"""Shared schema-drift helper for indexing into decoded RPC payloads.

``safe_index`` walks a nested list/tuple by integer keys with soft-strict
semantics. Since PR 13.9a the default is strict: drift raises
``UnknownRPCMethodError`` so callers fail fast when Google's response
shape moves out from under us. Setting ``NOTEBOOKLM_STRICT_DECODE=0``
opts back into the legacy warn-and-return-``None`` fallback for one
release window (see ``docs/adr/0011-schema-validation-policy.md``).

This is the single shared point of policy for "the payload didn't look like
we expected" — call sites should migrate to ``safe_index`` rather than
hand-rolling ``try/except IndexError`` blocks.
"""

from __future__ import annotations

import logging
import reprlib
import warnings
from typing import Any

from .._env import is_strict_decode_enabled
from ..exceptions import UnknownRPCMethodError

__all__ = ["safe_index"]

logger = logging.getLogger(__name__)

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
    """Walk ``data`` by ``path`` indices with soft-strict drift handling.

    Args:
        data: Nested list/tuple structure (typically a decoded RPC payload).
        *path: Sequence of integer indices to descend.
        method_id: RPC method ID (for diagnostics on drift).
        source: Caller label identifying where the drift was observed
            (e.g. ``"_notebooks.list"``); included in logs and the raised
            exception's ``source`` attribute.

    Returns:
        The value at ``data[path[0]][path[1]]...`` on success, or ``None`` in
        soft mode when descent fails.

    Raises:
        UnknownRPCMethodError: When ``NOTEBOOKLM_STRICT_DECODE`` is truthy and
            descent fails. The exception carries ``method_id``, ``source``,
            ``path`` (truncated to where descent stopped), and a truncated
            ``data_at_failure`` repr.
    """
    current: Any = data
    for i, key in enumerate(path):
        try:
            current = current[key]
        except (IndexError, TypeError, KeyError) as exc:
            failing_path = tuple(path[:i])
            data_repr = _truncate(current)
            if is_strict_decode_enabled():
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
            logger.warning(
                "safe_index drift at %r[%d] (method_id=%r, source=%r): %s",
                failing_path,
                key,
                method_id,
                source,
                data_repr,
            )
            warnings.warn(
                "safe_index soft-mode fallback via NOTEBOOKLM_STRICT_DECODE=0 "
                f"was used at source {source!r}; this opt-out is deprecated "
                "and scheduled for removal in v0.6.0. Handle "
                "UnknownRPCMethodError or fix the decoder call site before "
                "the soft-mode path is removed.",
                DeprecationWarning,
                stacklevel=2,
            )
            return None
    return current
