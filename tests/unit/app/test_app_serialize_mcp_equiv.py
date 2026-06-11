"""Compatibility guard for the former MCP serializer behavior.

MCP tools now import ``notebooklm._app.serialize.to_jsonable`` directly. This
test keeps a small inlined copy of the former MCP serializer as a reference so
values that mattered to MCP before the dedup still produce identical JSON wire
output.

One intentional intermediate difference, neutralized at the wire: ``_app`` passes
JSON-scalar mapping keys through while MCP eagerly ``str()``-ifies them — but
``json.dumps`` stringifies object keys either way, so the serialized bytes match.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import date, datetime
from enum import Enum, IntEnum

import pytest

from notebooklm._app.serialize import to_jsonable

# --- Former MCP serializer behavior, inlined as the compatibility reference ---
_MCP_PRIMS = (str, int, float, bool)


def _mcp_to_jsonable(obj):
    if obj is None:
        return obj
    if isinstance(obj, Enum):
        return _mcp_to_jsonable(obj.value)
    if isinstance(obj, _MCP_PRIMS):
        return obj
    if isinstance(obj, (datetime, date)):
        return obj.isoformat()
    if isinstance(obj, (bytes, bytearray)):
        return bytes(obj).decode("utf-8", "replace")
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: _mcp_to_jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, dict):
        return {str(k): _mcp_to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [_mcp_to_jsonable(v) for v in obj]
    return str(obj)


def _wire(value) -> str:
    """The bytes an adapter actually emits."""
    return json.dumps(value, sort_keys=True, default=str)


class _Color(str, Enum):
    RED = "red"


class _Level(IntEnum):
    HIGH = 3


@dataclasses.dataclass
class _Inner:
    a: int
    b: str | None = None


@dataclasses.dataclass
class _Outer:
    name: str
    inner: _Inner
    items: list[_Inner]
    when: datetime | None
    tags: tuple[str, ...]


SAMPLES = [
    None,
    True,
    42,
    3.14,
    "hello",
    _Color.RED,
    _Level.HIGH,
    datetime(2026, 6, 8, 9, 30, 0),
    date(2026, 6, 8),
    b"raw bytes",
    ["a", 1, _Color.RED],
    ("x", "y"),
    {"k": "v", "n": 2, "nested": {"d": date(2026, 1, 2)}},  # string-keyed
    {1: "one", 2: "two"},  # int-keyed — diverges intermediate, identical on the wire
    _Outer(
        name="top",
        inner=_Inner(a=1),
        items=[_Inner(a=2, b="two"), _Inner(a=3)],
        when=datetime(2026, 1, 2, 3, 4, 5),
        tags=("p", "q"),
    ),
]


@pytest.mark.parametrize("sample", SAMPLES, ids=lambda s: type(s).__name__)
def test_app_serialize_is_wire_identical_to_mcp_serializer(sample):
    assert _wire(to_jsonable(sample)) == _wire(_mcp_to_jsonable(sample))


def test_real_notebook_type_wire_identical():
    from notebooklm.types import Notebook

    nb = Notebook(id="nb1", title="Alpha", created_at=datetime(2026, 6, 8, 9, 0, 0))
    assert _wire(to_jsonable(nb)) == _wire(_mcp_to_jsonable(nb))
