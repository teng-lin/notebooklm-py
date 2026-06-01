"""Unit tests for the NotebookLM MCP server troubleshooting logic."""

import json

import pytest

from notebooklm.exceptions import (
    ArtifactTimeoutError,
    AuthError,
    NotebookLimitError,
    NotebookNotFoundError,
    RateLimitError,
)
from notebooklm_mcp.server import _handle_exception, notebooklm_troubleshoot


def test_handle_exception_auth_error():
    exc = AuthError("Unauthorized")
    result = _handle_exception(exc, "listing notebooks")
    assert "Authentication failed" in result
    assert "notebooklm login" in result


def test_handle_exception_rate_limit():
    exc = RateLimitError("Rate limit exceeded", retry_after=30)
    result = _handle_exception(exc, "generating audio")
    assert "Rate limit exceeded" in result
    assert "30 seconds" in result


def test_handle_exception_not_found():
    exc = NotebookNotFoundError("nb_123")
    result = _handle_exception(exc, "getting notebook")
    assert "Notebook not found: nb_123" in result


def test_handle_exception_artifact_timeout():
    exc = ArtifactTimeoutError("nb_1", "task_1", 360, last_status="pending")
    result = _handle_exception(exc, "generating podcast")
    data = json.loads(result)
    assert data["status"] == "timeout"
    assert data["task_id"] == "task_1"
    assert "pending" in data["message"]


def test_handle_exception_notebook_limit():
    exc = NotebookLimitError(current_count=100, limit=100)
    result = _handle_exception(exc, "creating notebook")
    assert "Notebook limit reached" in result
    assert "100/100" in result


@pytest.mark.asyncio
async def test_troubleshoot_auth():
    result = await notebooklm_troubleshoot("Unauthorized access")
    data = json.loads(result)
    assert "Authentication expired" in data["diagnosis"]
    assert any("notebooklm login" in step for step in data["action_steps"])


@pytest.mark.asyncio
async def test_troubleshoot_rate_limit():
    result = await notebooklm_troubleshoot("Rate limit exceeded [3]")
    data = json.loads(result)
    assert "rate limit" in data["diagnosis"].lower()
    assert any("5-10 minutes" in step for step in data["action_steps"])


@pytest.mark.asyncio
async def test_troubleshoot_x_com():
    result = await notebooklm_troubleshoot("Privacy error on x.com", operation="adding URL source")
    data = json.loads(result)
    assert "X.com (Twitter)" in data["diagnosis"]
    assert any("bird" in step for step in data["action_steps"])


@pytest.mark.asyncio
async def test_troubleshoot_html_upload():
    result = await notebooklm_troubleshoot("HTML files not supported", operation="adding source")
    data = json.loads(result)
    assert "HTML/XHTML file uploads" in data["diagnosis"]
    assert any("plain text" in step for step in data["action_steps"])


@pytest.mark.asyncio
async def test_troubleshoot_notebook_limit():
    result = await notebooklm_troubleshoot("Notebook limit reached (100/100)")
    data = json.loads(result)
    assert "maximum number of notebooks" in data["diagnosis"]
    assert any("delete" in step.lower() for step in data["action_steps"])
