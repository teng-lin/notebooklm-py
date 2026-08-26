"""Focused tests for transport-neutral notebook guide generation."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from notebooklm._deadline import RuntimeDeadline
from notebooklm._operations import Operation
from notebooklm._semantic.records import (
    NOTEBOOK_DESCRIBE_DEF,
    NOTEBOOK_SUMMARIZE_DEF,
    NotebookDescriptionRecord,
    NotebookGuideInput,
    NotebookGuideResult,
    SuggestedTopicRecord,
)
from notebooklm._semantic.services.notebook_guide import NotebookGuideService
from tests._fixtures.recording_backend import BackendInvocation, RecordingBackend


@pytest.mark.asyncio
async def test_guide_service_preserves_distinct_operations_deadline_and_projections() -> None:
    description = NotebookDescriptionRecord(
        summary="A concise summary",
        suggested_topics=(SuggestedTopicRecord("Question?", "Ask this"),),
    )
    backend = RecordingBackend()
    backend.set_result(NOTEBOOK_SUMMARIZE_DEF, NotebookGuideResult(description))
    backend.set_result(NOTEBOOK_DESCRIBE_DEF, NotebookGuideResult(description))
    service = NotebookGuideService(backend)
    deadline = RuntimeDeadline(timeout=5.0, started_at=10.0, monotonic=lambda: 11.0)

    assert await service.get_summary("notebook-id", deadline=deadline) == "A concise summary"
    projected = await service.get_description("notebook-id", deadline=deadline)

    assert projected.summary == "A concise summary"
    assert [(topic.question, topic.prompt) for topic in projected.suggested_topics] == [
        ("Question?", "Ask this")
    ]
    assert backend.invocations == [
        BackendInvocation(
            Operation.NOTEBOOK_SUMMARIZE,
            NotebookGuideInput("notebook-id"),
            deadline,
        ),
        BackendInvocation(
            Operation.NOTEBOOK_DESCRIBE,
            NotebookGuideInput("notebook-id"),
            deadline,
        ),
    ]


def test_guide_service_is_transport_neutral() -> None:
    path = (
        Path(__file__).resolve().parents[2]
        / "src"
        / "notebooklm"
        / "_semantic"
        / "services"
        / "notebook_guide.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_modules = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}

    assert not any(
        forbidden in module
        for module in imported_modules
        for forbidden in ("httpx", "rpc", "cli", "mcp", "server", "_row_adapters")
    )
    assert names.isdisjoint({"RPCMethod", "RpcCaller", "NotebookLMClient"})
    assert not any(isinstance(node, ast.Subscript) for node in ast.walk(tree))
