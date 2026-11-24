"""Golden tests pinning ``notebooklm._app.serialize.to_jsonable``."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum, IntEnum

from notebooklm._app.serialize import to_jsonable
from notebooklm._web.rows.notebooks import decode_notebook
from notebooklm.types import Notebook, SharePermission


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
        role=SharePermission.OWNER,
    )

    result = to_jsonable(nb)

    # ``role`` is an ``int`` enum, so ``to_jsonable`` unwraps it to its wire
    # value; adapters add the ``role_label`` string via ``_app.views``.
    assert result == {
        "id": "nb-1",
        "title": "My Notebook",
        "created_at": "2026-06-08T00:00:00+00:00",
        "sources_count": 3,
        "is_owner": True,
        "modified_at": None,
        "role": 1,
        "last_viewed_at": None,
        "emoji": None,
        "premium_features": None,
        "chat_sessions": [],
        "chat_settings": None,
    }
    json.dumps(result)


def test_notebook_view_adds_role_label() -> None:
    """``notebook_view`` labels the raw role code for agent consumers (#2125)."""
    from notebooklm._app.views import notebook_view

    view = notebook_view(Notebook(id="nb-1", title="N", role=SharePermission.VIEWER))

    assert view["role"] == SharePermission.VIEWER.value
    assert view["role_label"] == "viewer"
    # The established transport fields remain intact.
    assert view["is_owner"] is False
    assert view["id"] == "nb-1"


def test_notebook_view_does_not_auto_expand_for_project_only_fields() -> None:
    """Python Project metadata must not silently change MCP/REST contracts."""
    from notebooklm._app.views import notebook_view

    notebook = decode_notebook(
        Notebook,
        [
            "N",
            None,
            "nb-1",
            "📖",
            None,
            None,
            None,
            [[2, "persona"], [4]],
            None,
            [True, True, False],
            None,
            [["session-1"]],
        ],
        include_chat_settings=True,
    )

    view = notebook_view(notebook)

    assert {"emoji", "premium_features", "chat_sessions", "chat_settings"}.isdisjoint(view)


def test_notebook_viewed_keys_returns_the_new_key_and_its_alias() -> None:
    """``notebook_viewed_keys`` is the single home for the alias policy (#2126).

    The hand-built CLI ``--json`` envelopes pick individual keys instead of
    serializing the dataclass, so they need this helper to stay in step with the
    ``to_jsonable`` surfaces.
    """
    from notebooklm._app.views import notebook_viewed_keys

    nb = Notebook(id="nb-1", title="N", last_viewed_at=datetime(2026, 8, 12, 10, 0))

    assert notebook_viewed_keys(nb) == {
        "last_viewed_at": "2026-08-12T10:00:00",
        "modified_at": "2026-08-12T10:00:00",
    }
    assert notebook_viewed_keys(Notebook(id="nb-1", title="N")) == {
        "last_viewed_at": None,
        "modified_at": None,
    }


def test_notebook_view_role_label_is_none_for_unknown_role() -> None:
    """An unstated role stays ``None`` rather than becoming the "unknown" label."""
    from notebooklm._app.views import notebook_view

    view = notebook_view(Notebook(id="nb-1", title="N"))

    assert view["role"] is None
    assert view["role_label"] is None
    # ``role`` is unknown, so the historical optimistic default holds.
    assert view["is_owner"] is True


def test_source_view_labels_the_drive_status_code() -> None:
    """``source_view`` labels the raw Drive-health code for agents (#2111)."""
    from notebooklm._app.views import source_view
    from notebooklm.types import DriveSourceStatus, Source

    view = source_view(Source(id="src-1", drive_status=DriveSourceStatus.INACCESSIBLE))

    assert view["drive_status"] == DriveSourceStatus.INACCESSIBLE.value
    assert view["drive_status_label"] == "inaccessible"
    # The verdict rides along: ``is_drive_degraded`` is a property, so a bare
    # ``to_jsonable`` pass would drop it and leave every non-Python consumer to
    # re-derive the degraded set from the label string.
    assert view["is_drive_degraded"] is True
    # Additive: the ingestion labels are untouched by the Drive axis.
    assert view["status_label"] == "ready"


def test_source_view_drive_status_label_is_none_when_absent() -> None:
    """No Drive claim stays ``None`` rather than becoming the "unknown" label.

    ``None`` (no claim) and ``"unknown"`` (a code we could not read) are
    different answers, and an agent must be able to tell them apart.
    """
    from notebooklm._app.views import source_view
    from notebooklm.types import DriveSourceStatus, Source

    absent = source_view(Source(id="src-1"))
    assert absent["drive_status"] is None
    assert absent["drive_status_label"] is None
    assert absent["is_drive_degraded"] is False

    unreadable = source_view(Source(id="src-1", drive_status=DriveSourceStatus.UNKNOWN))
    assert unreadable["drive_status_label"] == "unknown"
    # A state we cannot name is not evidence of degradation.
    assert unreadable["is_drive_degraded"] is False


def test_source_view_does_not_publish_python_only_source_metadata() -> None:
    """Enriching ``Source`` must not implicitly widen the MCP/REST contract."""
    from notebooklm._app.views import source_view
    from notebooklm.types import Source

    source = Source(
        id="src-1",
        download_url="https://example.test/download",
        viewer_url="https://example.test/view",
        content_mime="text/markdown",
        word_count=123,
        revision_id="revision-1",
        revision_timestamp=datetime(2026, 8, 13, tzinfo=timezone.utc),
        last_modified_at=datetime(2026, 8, 14, tzinfo=timezone.utc),
    )

    view = source_view(source)

    assert (
        not {
            "download_url",
            "viewer_url",
            "content_mime",
            "word_count",
            "revision_id",
            "revision_timestamp",
            "last_modified_at",
        }
        & view.keys()
    )


def test_source_summary_stays_narrow_so_add_envelopes_do_not_widen() -> None:
    """``source_summary`` must NOT grow the Drive axis.

    It is the shape ``source add`` / ``source add-drive`` publish. The Drive
    fields reached the CLI by composition — the CLI-only spelling lives in
    ``cli.services.source_serializers.source_row_payload`` — precisely so those
    envelopes keep their pinned four keys; this guards that decision.
    """
    from notebooklm._app.serialize import source_summary
    from notebooklm.types import DriveSourceStatus, Source

    summary = source_summary(
        Source(
            id="src-1",
            title="Shared Doc",
            drive_document_id="1AbC",
            drive_status=DriveSourceStatus.DELETED,
        )
    )

    assert list(summary) == ["id", "title", "type", "url"]


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


def test_ask_result_view_carries_the_conversation_turn_key() -> None:
    """The turn key rides the shared MCP/REST view (#2122).

    It is a nested frozen dataclass, so this also pins that ``to_jsonable``
    flattens it rather than str()-ing it into an unusable blob — a caller has
    to be able to read the three parts back out to build a per-turn RPC.
    """
    from notebooklm._app.views import ask_result_view
    from notebooklm.types import AskResult, ConversationTurnKey

    # Deliberately DISTINCT from ``conversation_id``: the two are different wire
    # slots that happened to be equal in one live probe, so a fixture reusing
    # one value could not detect a serializer substituting one for the other.
    view = ask_result_view(
        AskResult(
            answer="a",
            conversation_id="conversation-1",
            turn_number=1,
            is_follow_up=False,
            raw_response="[[wire blob]]",
            turn_key=ConversationTurnKey("session-1", "turn-1", 2187103311),
        )
    )

    assert view["conversation_id"] == "conversation-1"
    assert view["turn_key"] == {
        "session_id": "session-1",
        "turn_id": "turn-1",
        "turn_code": 2187103311,
    }
    # Still stripped: the key is not an excuse to leak the debug blob.
    assert "raw_response" not in view


def test_ask_result_view_turn_key_is_none_when_the_stream_carried_none() -> None:
    from notebooklm._app.views import ask_result_view
    from notebooklm.types import AskResult

    view = ask_result_view(
        AskResult(answer="a", conversation_id="conv-1", turn_number=1, is_follow_up=False)
    )
    assert view["turn_key"] is None


def test_ask_result_view_carries_next_step_suggestions() -> None:
    from notebooklm._app.views import ask_result_view
    from notebooklm.types import AskResult, NextStepSuggestion

    view = ask_result_view(
        AskResult(
            answer="a",
            conversation_id="conv-1",
            turn_number=1,
            is_follow_up=False,
            next_steps=[NextStepSuggestion("What next?", 9)],
        )
    )

    assert view["next_steps"] == [{"question": "What next?", "type_code": 9}]


def test_ask_result_view_reports_unresolved_citations() -> None:
    """The JSON envelope reports raw markers in an answer with no references."""
    from notebooklm._app.views import ask_result_view
    from notebooklm.types import AskResult

    broken = ask_result_view(
        AskResult(
            answer="Claim [cite: 1] and claim [cite: 2, 3].",
            conversation_id="conv-1",
            turn_number=3,
            is_follow_up=True,
        )
    )
    healthy = ask_result_view(
        AskResult(answer="Claim [1].", conversation_id="conv-1", turn_number=1, is_follow_up=False)
    )

    assert broken["unresolved_citations"] == 2
    assert broken["references"] == []
    assert healthy["unresolved_citations"] == 0
