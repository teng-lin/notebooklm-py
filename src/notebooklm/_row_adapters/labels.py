"""Typed positional view over a raw source-label tuple.

Strict-only per ADR-0019/0011: positional descent uses ``safe_index``
(raises ``UnknownRPCMethodError`` on drift) and type drift raises too. The only
tolerated "absence" is a legitimately empty label (``sources`` slot is ``None``)
— that is not drift. A drifted ``sources`` slot (non-list, malformed member,
non-string id) always raises; there is no degrade-to-empty path. (The
``NOTEBOOKLM_STRICT_DECODE=0`` opt-out that older adapters honoured was retired
in v0.7.0 — ``rpc/_safe_index.py`` is strict-only now.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..exceptions import UnknownRPCMethodError
from ..rpc import safe_index

__all__ = ["LabelRow"]

_SRC = "_row_adapters.labels"


@dataclass(frozen=True)
class LabelRow:
    """Typed positional view over a raw label tuple ``[name, sources, id, emoji]``."""

    name: str
    source_ids: tuple[str, ...]
    id: str
    emoji: str

    @classmethod
    def from_label_tuple(cls, data: list[Any], *, method_id: str | None = None) -> LabelRow:
        # Required positions — safe_index raises UnknownRPCMethodError on drift.
        name = safe_index(data, 0, method_id=method_id, source=_SRC)
        sources = safe_index(data, 1, method_id=method_id, source=_SRC)  # list OR None
        label_id = safe_index(data, 2, method_id=method_id, source=_SRC)
        emoji = safe_index(data, 3, method_id=method_id, source=_SRC)
        # Type drift fails loud — do NOT collapse to sentinels (ADR-0019/0011).
        if not isinstance(name, str) or not isinstance(label_id, str):
            raise UnknownRPCMethodError(
                message="label tuple name/id not strings",
                method_id=method_id,
                source=_SRC,
            )
        if sources is None:
            source_ids: tuple[str, ...] = ()  # legitimate empty label
        elif isinstance(sources, list):
            ids: list[str] = []
            for s in sources:
                # Each member must be exactly ``[source_id]``. A malformed member
                # — wrong length, not a list, or a non-string id — is drift:
                # RAISE, never silently skip (ADR-0019/0011).
                if not (isinstance(s, list) and len(s) == 1 and isinstance(s[0], str)):
                    raise UnknownRPCMethodError(
                        message="malformed label member row",
                        method_id=method_id,
                        source=_SRC,
                    )
                ids.append(s[0])
            source_ids = tuple(ids)
        else:
            raise UnknownRPCMethodError(
                message="label sources slot is neither None nor list",
                method_id=method_id,
                source=_SRC,
            )
        # Non-string emoji is drift — raise, do not coerce to "".
        if not isinstance(emoji, str):
            raise UnknownRPCMethodError(
                message="label emoji slot is not a string",
                method_id=method_id,
                source=_SRC,
            )
        return cls(name=name, source_ids=source_ids, id=label_id, emoji=emoji)
