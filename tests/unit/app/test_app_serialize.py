"""Golden tests pinning ``notebooklm._app.serialize.to_jsonable``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum, IntEnum

from notebooklm._app.serialize import to_jsonable
from notebooklm.types import Notebook


class Color(str, Enum):
    """A str-mixin enum (member is also a ``str`` instance)."""

    RED = "red"
    GREEN = "green"


class Priority(IntEnum):
    """An int-mixin enum (member is also an ``int`` instance)."""

    LOW = 1
    HIGH = 9


@dataclass
class Inner:
    name: str
    when: datetime | None = None


@dataclass
class Outer:
    label: str
    color: Color
    priority: Priority
    children: list[Inner] = field(default_factory=list)
    blob: bytes = b""
    tags: tuple[str, ...] = ()


def test_nested_dataclass_is_fully_converted() -> None:
    obj = Outer(
        label="root",
        color=Color.GREEN,
        priority=Priority.HIGH,
        children=[Inner(name="a", when=datetime(2026, 6, 8, 12, 0, 0, tzinfo=timezone.utc))],
        blob=b"hi",
        tags=("x", "y"),
    )

    result = to_jsonable(obj)

    assert result == {
        "label": "root",
        "color": "green",
        "priority": 9,
        "children": [{"name": "a", "when": "2026-06-08T12:00:00+00:00"}],
        "blob": "hi",
        "tags": ["x", "y"],
    }
    # The whole structure must be JSON-serializable with no custom ``default``.
    json.dumps(result)


def test_str_enum_unwraps_to_plain_str_value() -> None:
    result = to_jsonable(Color.RED)

    assert result == "red"
    # Must be a *plain* str, not the enum member (which is also a str instance).
    assert type(result) is str
    assert not isinstance(result, Enum)


def test_int_enum_unwraps_to_plain_int_value() -> None:
    result = to_jsonable(Priority.LOW)

    assert result == 1
    assert type(result) is int
    assert not isinstance(result, Enum)


def test_datetime_becomes_isoformat() -> None:
    dt = datetime(2026, 6, 8, 9, 30, 15, tzinfo=timezone.utc)

    assert to_jsonable(dt) == "2026-06-08T09:30:15+00:00"


def test_date_becomes_isoformat() -> None:
    assert to_jsonable(date(2026, 6, 8)) == "2026-06-08"


def test_bytes_decode_to_text() -> None:
    assert to_jsonable(b"hello") == "hello"
    # Non-UTF-8 bytes degrade to replacement chars rather than raising.
    assert to_jsonable(b"\xff\xfe") == "��"


def test_primitives_and_none_pass_through() -> None:
    assert to_jsonable(None) is None
    assert to_jsonable(True) is True
    assert to_jsonable(7) == 7
    assert to_jsonable(3.5) == 3.5
    assert to_jsonable("plain") == "plain"


def test_mapping_keys_coerced_and_values_recursed() -> None:
    result = to_jsonable({1: Color.RED, "k": [Priority.HIGH]})

    assert result == {1: "red", "k": [9]}


def test_set_becomes_sorted_list_of_values() -> None:
    result = to_jsonable({Color.RED, Color.GREEN})

    assert sorted(result) == ["green", "red"]


def test_real_notebooklm_type_round_trips() -> None:
    nb = Notebook(
        id="nb-1",
        title="My Notebook",
        created_at=datetime(2026, 6, 8, 0, 0, 0, tzinfo=timezone.utc),
        sources_count=3,
        is_owner=True,
    )

    result = to_jsonable(nb)

    assert result == {
        "id": "nb-1",
        "title": "My Notebook",
        "created_at": "2026-06-08T00:00:00+00:00",
        "sources_count": 3,
        "is_owner": True,
    }
    json.dumps(result)


def test_unknown_object_falls_back_to_str() -> None:
    class Opaque:
        def __str__(self) -> str:
            return "opaque-repr"

    assert to_jsonable(Opaque()) == "opaque-repr"


def test_dataclass_class_itself_is_not_treated_as_instance() -> None:
    # A dataclass *type* (not an instance) must not be field-walked; it falls
    # through to the ``str()`` last resort.
    result = to_jsonable(Inner)

    assert isinstance(result, str)
    assert "Inner" in result
