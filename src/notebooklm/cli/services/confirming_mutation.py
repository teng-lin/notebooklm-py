"""Shared confirmed-mutation pipeline for CLI resources."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Generic, Literal, TypeVar

from ...client import NotebookLMClient

ResolvedT = TypeVar("ResolvedT")
MutationStatus = Literal["cancelled", "completed"]


@dataclass(frozen=True)
class MutationPlan(Generic[ResolvedT]):
    """Command-specific configuration for :func:`run_confirmed_mutation`."""

    entity_label: str
    resolve: Callable[[NotebookLMClient], Awaitable[ResolvedT]]
    confirm_message: str
    execute: Callable[[NotebookLMClient, ResolvedT], Awaitable[None]]
    serialize_success: Callable[[ResolvedT], dict[str, Any]]
    serialize_cancel: Callable[[ResolvedT], dict[str, Any]]


@dataclass(frozen=True)
class MutationResult(Generic[ResolvedT]):
    """Result of a confirmed mutation workflow."""

    entity_label: str
    resolved: ResolvedT
    status: MutationStatus
    payload: dict[str, Any]


async def run_confirmed_mutation(
    plan: MutationPlan[ResolvedT],
    client: NotebookLMClient,
    *,
    yes: bool,
    json_output: bool,
    confirmer: Callable[[str], bool],
) -> MutationResult[ResolvedT]:
    """Resolve an entity, optionally confirm, execute, and serialize the result."""
    resolved = await plan.resolve(client)

    if not yes and not json_output:
        confirm_message = plan.confirm_message.format(resolved=resolved)
        if not confirmer(confirm_message):
            payload = plan.serialize_cancel(resolved)
            return MutationResult(
                entity_label=plan.entity_label,
                resolved=resolved,
                status="cancelled",
                payload=payload,
            )

    await plan.execute(client, resolved)
    payload = plan.serialize_success(resolved)
    return MutationResult(
        entity_label=plan.entity_label,
        resolved=resolved,
        status="completed",
        payload=payload,
    )


__all__ = ["MutationPlan", "MutationResult", "run_confirmed_mutation"]
