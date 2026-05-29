"""Unit tests for the confirmed mutation CLI service."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from notebooklm.cli.services.confirming_mutation import MutationPlan, run_confirmed_mutation


def _plan(
    *,
    calls: list[tuple[str, Any]],
    resolved: SimpleNamespace | None = None,
) -> MutationPlan[SimpleNamespace]:
    target = resolved or SimpleNamespace(id="thing_123", title="Target", executed=False)

    async def resolve(client: object) -> SimpleNamespace:
        calls.append(("resolve", client))
        return target

    async def execute(client: object, resolved: SimpleNamespace) -> None:
        calls.append(("execute", client))
        resolved.executed = True

    return MutationPlan(
        entity_label="thing",
        resolve=resolve,
        confirm_message="Delete {resolved.title} ({resolved.id})?",
        execute=execute,
        serialize_success=lambda resolved: {"id": resolved.id, "deleted": True},
        serialize_cancel=lambda resolved: {"id": resolved.id, "deleted": False},
    )


@pytest.mark.asyncio
async def test_cancels_after_resolution_and_skips_execution() -> None:
    calls: list[tuple[str, Any]] = []
    confirm_messages: list[str] = []
    client = object()

    result = await run_confirmed_mutation(
        _plan(calls=calls),
        client,
        yes=False,
        json_output=False,
        confirmer=lambda message: confirm_messages.append(message) or False,
    )

    assert result.entity_label == "thing"
    assert result.status == "cancelled"
    assert result.payload == {"id": "thing_123", "deleted": False}
    assert result.resolved.executed is False
    assert confirm_messages == ["Delete Target (thing_123)?"]
    assert calls == [("resolve", client)]


@pytest.mark.asyncio
async def test_yes_skips_confirmation_and_executes_mutation() -> None:
    calls: list[tuple[str, Any]] = []
    client = object()

    result = await run_confirmed_mutation(
        _plan(calls=calls),
        client,
        yes=True,
        json_output=False,
        confirmer=lambda message: pytest.fail(f"unexpected confirmation: {message}"),
    )

    assert result.status == "completed"
    assert result.payload == {"id": "thing_123", "deleted": True}
    assert result.resolved.executed is True
    assert calls == [("resolve", client), ("execute", client)]


@pytest.mark.asyncio
async def test_returns_success_payload() -> None:
    calls: list[tuple[str, Any]] = []

    result = await run_confirmed_mutation(
        _plan(calls=calls),
        object(),
        yes=True,
        json_output=True,
        confirmer=lambda message: pytest.fail(f"unexpected confirmation: {message}"),
    )

    assert result.status == "completed"
    assert result.payload == {"id": "thing_123", "deleted": True}


@pytest.mark.asyncio
async def test_json_output_without_yes_does_not_prompt() -> None:
    calls: list[tuple[str, Any]] = []

    result = await run_confirmed_mutation(
        _plan(calls=calls),
        object(),
        yes=False,
        json_output=True,
        confirmer=lambda message: pytest.fail(f"unexpected confirmation: {message}"),
    )

    assert result.status == "completed"
    assert result.resolved.executed is True
    assert result.payload == {"id": "thing_123", "deleted": True}
