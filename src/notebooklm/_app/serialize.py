"""Transport-neutral recursive JSON-able conversion.

:func:`to_jsonable` turns an arbitrary Python object graph into a structure
composed only of JSON-native types (``dict`` / ``list`` / ``str`` / ``int`` /
``float`` / ``bool`` / ``None``), so any adapter can hand the result to
``json.dumps`` (or a serializer that pins those types) without further work.

This module is the single source of truth for that conversion; the CLI and
the MCP server both build their ``--json`` / tool envelopes on top of it.
It must stay transport-neutral — no ``click`` / ``rich`` / ``cli`` / ``fastmcp``
imports (enforced by ``tests/_guardrails/test_app_boundary.py``).

Conversion rules, applied in order (the order is load-bearing):

1. ``None`` and JSON-native scalars (``bool`` / ``int`` / ``float`` / ``str``)
   pass through unchanged — **except** :class:`enum.Enum` members, which are
   handled first (rule 0) because a ``str``/``int`` enum is *also* an instance
   of its mixed-in primitive: checking the primitive first would leak the enum
   member instead of unwrapping it to ``.value``.
0. :class:`enum.Enum` -> recurse on ``member.value`` (so an
   ``IntEnum``/``str``-``Enum`` collapses to its underlying scalar).
2. :class:`datetime.datetime` / :class:`datetime.date` -> ``.isoformat()``.
   (``datetime`` is a subclass of ``date``, so the lone ``date`` check below
   also catches it; the ``isoformat`` call is shared.)
3. :class:`bytes` / :class:`bytearray` -> UTF-8 decoded text with
   ``errors="replace"`` so non-text payloads still serialize losslessly into a
   string rather than raising.
4. Dataclass instances (not classes) -> ``{field: to_jsonable(value)}`` over
   their declared fields.
5. :class:`collections.abc.Mapping` -> ``{to_jsonable_key(k): to_jsonable(v)}``;
   keys are coerced to JSON-legal scalar keys.
6. ``list`` / ``tuple`` / ``set`` / ``frozenset`` and other non-str/bytes
   iterables -> ``[to_jsonable(item)]``.
7. Last resort -> ``str(obj)``.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from datetime import date
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..types import Source

# JSON-native scalar leaves that pass through untouched. ``bool`` is a subclass
# of ``int`` so listing ``int`` alone would already catch it, but naming it
# keeps the contract explicit.
_PASSTHROUGH_SCALARS = (bool, int, float, str)


def to_jsonable(obj: Any) -> Any:
    """Recursively convert ``obj`` into a JSON-serializable structure.

    See the module docstring for the full, ordered ruleset. The function is
    pure (no I/O, no mutation of ``obj``) and total for any *acyclic* graph: it
    never raises for an unrecognized type — it falls back to ``str(obj)``. A
    self-referential structure (a dataclass/dict/list that contains itself) is
    out of scope and raises ``RecursionError``, exactly as the CLI's prior
    ``json.dumps(..., default=str)`` did; the typed domain results this serves
    are acyclic value objects.
    """
    # Rule 0 — Enum BEFORE the scalar passthrough. A ``str``/``int`` enum member
    # is also an instance of ``str``/``int``; if the passthrough ran first it
    # would return the enum member itself (which is not JSON-native and would
    # serialize via its repr). Unwrap to ``.value`` and recurse so an enum whose
    # value is itself non-native (rare) is still normalized.
    if isinstance(obj, Enum):
        return to_jsonable(obj.value)

    # Rule 1 — None + JSON-native scalars pass through unchanged.
    if obj is None or isinstance(obj, _PASSTHROUGH_SCALARS):
        return obj

    # Rule 2 — datetime/date -> ISO 8601 string. ``datetime`` subclasses
    # ``date``, so this single check covers both.
    if isinstance(obj, date):
        return obj.isoformat()

    # Rule 3 — raw bytes -> decoded text.
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", errors="replace")

    # Rule 4 — dataclass instances (NOT dataclass *classes*).
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {
            field.name: to_jsonable(getattr(obj, field.name)) for field in dataclasses.fields(obj)
        }

    # Rule 5 — mappings. Keys are coerced to JSON-legal scalar keys.
    if isinstance(obj, Mapping):
        return {_jsonable_key(key): to_jsonable(value) for key, value in obj.items()}

    # Rule 6 — generic iterables (list/tuple/set/frozenset/...), excluding the
    # str/bytes types already handled above.
    if isinstance(obj, Iterable):
        return [to_jsonable(item) for item in obj]

    # Rule 7 — last resort.
    return str(obj)


def source_summary(source: Source) -> dict[str, Any]:
    """Return the transport-neutral ``{"id", "title", "type", "url"}`` summary.

    The single source of truth for the source-summary shape shared by every
    adapter (§11): both the CLI's
    ``cli.services.source_serializers.source_summary_payload`` and the
    ``source add`` / ``source add-drive`` JSON envelopes import this helper so
    the summary dict is built in exactly one place. ``type`` is the source
    kind's public ``.value`` (``None`` when the kind is unknown).
    """
    kind = source.kind
    return {
        "id": source.id,
        "title": source.title,
        "type": kind.value if kind is not None else None,
        "url": source.url,
    }


def _jsonable_key(key: Any) -> str | int | float | bool | None:
    """Coerce a mapping key into a JSON-legal object key.

    JSON object keys must be strings; ``json.dumps`` additionally accepts
    ``int`` / ``float`` / ``bool`` / ``None`` keys (it stringifies them). To
    keep parity with that behavior we pass those scalars through (after
    unwrapping enums) and stringify everything else, so a dataclass / tuple
    used as a key serializes deterministically instead of raising.
    """
    if isinstance(key, Enum):
        return _jsonable_key(key.value)
    if key is None or isinstance(key, _PASSTHROUGH_SCALARS):
        return key
    return str(key)
