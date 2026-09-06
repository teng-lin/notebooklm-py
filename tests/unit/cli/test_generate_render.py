"""CLI ownership of generation wording and exit policy."""

from __future__ import annotations

from typing import cast

from notebooklm._app.generate_retry import GenerationOutcome, GenerationWaitStarted
from notebooklm._app.generation_requests import GenerationKind
from notebooklm.cli._generate_render import format_generation_wait, generation_exit_code


def test_known_kind_includes_typical_hint() -> None:
    message = format_generation_wait(
        GenerationWaitStarted(kind="cinematic-video", task_id="task", elapsed=0)
    )
    assert "video" in message
    assert "typically 30-40 min" in message
    assert message.endswith("...")


def test_unknown_kind_omits_hint() -> None:
    event = GenerationWaitStarted(
        kind=cast(GenerationKind, "unknown-kind"), task_id="task", elapsed=0
    )
    message = format_generation_wait(event)
    assert "unknown kind" in message
    assert "(" not in message


def test_elapsed_seconds_are_cli_wording() -> None:
    message = format_generation_wait(
        GenerationWaitStarted(kind="audio", task_id="task", elapsed=42.7)
    )
    assert "[42s elapsed]" in message


def test_cli_owns_generation_exit_policy() -> None:
    assert generation_exit_code(GenerationOutcome(status="rate_limited", kind="audio")) == 1
    assert generation_exit_code(GenerationOutcome(status="pending", kind="audio")) == 0
