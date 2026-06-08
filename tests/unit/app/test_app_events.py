"""Tests for ``notebooklm._app.events`` (neutral progress seam).

``events`` is a small value type + a ``runtime_checkable`` Protocol; these pin
the :class:`ProgressEvent` field defaults and the structural
:class:`ProgressSink` contract a sink must satisfy.
"""

from __future__ import annotations

import dataclasses

import pytest

from notebooklm._app.events import ProgressEvent, ProgressSink


def test_progress_event_defaults() -> None:
    event = ProgressEvent(message="Polling")
    assert event.message == "Polling"
    assert event.kind is None
    assert event.pct is None


def test_progress_event_carries_all_fields() -> None:
    event = ProgressEvent(message="Downloading", kind="download", pct=0.5)
    assert (event.message, event.kind, event.pct) == ("Downloading", "download", 0.5)


def test_progress_event_is_frozen() -> None:
    event = ProgressEvent(message="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        event.message = "y"  # type: ignore[misc]


def test_progress_sink_protocol_is_structural() -> None:
    class _ListSink:
        def __init__(self) -> None:
            self.events: list[ProgressEvent] = []

        def emit(self, event: ProgressEvent) -> None:
            self.events.append(event)

    sink = _ListSink()
    assert isinstance(sink, ProgressSink)
    sink.emit(ProgressEvent(message="done", kind="done"))
    assert sink.events[0].kind == "done"


def test_non_emitting_object_is_not_a_progress_sink() -> None:
    assert not isinstance(object(), ProgressSink)
