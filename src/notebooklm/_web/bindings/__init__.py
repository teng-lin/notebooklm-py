"""Web binding rows, one module per domain (P9.3).

``WEB_BINDING_ROWS`` is the union of every domain's rows.  ``_web/registry.py``
requires each supported operation to be backed by exactly one of a legacy
handler name or a row here, so a row and a handler can never both claim an
operation; the construction-time audit in ``_binding`` then checks the
assembled table against the supported disposition set.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType

from ..._binding import Binding
from ..._operations import Operation
from .mind_maps import MIND_MAP_ROWS
from .notes import NOTE_ROWS
from .settings import SETTINGS_ROWS

_DOMAIN_ROWS: tuple[Mapping[Operation, Binding], ...] = (
    MIND_MAP_ROWS,
    NOTE_ROWS,
    SETTINGS_ROWS,
)


def _assemble_rows(domains: tuple[Mapping[Operation, Binding], ...]) -> Mapping[Operation, Binding]:
    rows: dict[Operation, Binding] = {}
    for domain in domains:
        for operation, row in domain.items():
            if operation in rows:
                raise RuntimeError(f"{operation.value} has a binding row in two domains")
            if row.definition.key is not operation:
                raise RuntimeError(
                    f"binding row for {operation.value} binds {row.definition.key.value}"
                )
            rows[operation] = row
    return MappingProxyType(rows)


WEB_BINDING_ROWS: Mapping[Operation, Binding] = _assemble_rows(_DOMAIN_ROWS)

__all__ = ["WEB_BINDING_ROWS"]
