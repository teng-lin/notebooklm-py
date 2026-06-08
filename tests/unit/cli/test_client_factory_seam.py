"""Tests for the ``ctx.obj`` client-factory seam (issue #1481, U1).

These cover the dual-path resolver (``cli.auth_runtime.resolve_client_factory``)
and the ``inject_client`` test helper that replaces the per-command-module
``patch("...X_cmd.NotebookLMClient")`` seam. The end-to-end test drives a real
command through ``CliRunner.invoke(obj=...)`` -- no module patching -- to prove
the injected factory reaches client construction.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock

import click

from notebooklm.cli.auth_runtime import resolve_client_factory
from notebooklm.client import NotebookLMClient
from notebooklm.notebooklm_cli import cli
from notebooklm.types import Label, Source

from .conftest import create_mock_client, inject_client


def _ctx(obj):
    ctx = click.Context(click.Command("x"))
    ctx.obj = obj
    return ctx


# ---------------------------------------------------------------------------
# resolve_client_factory: resolution order
# ---------------------------------------------------------------------------


def test_resolve_prefers_injected_factory():
    sentinel = lambda *a, **k: "client"  # noqa: E731 - identity marker
    assert resolve_client_factory(_ctx({"client_factory": sentinel}), default=str) is sentinel


def test_resolve_falls_back_to_call_site_default():
    # No injected factory -> the module-level default (still-patchable name).
    assert resolve_client_factory(_ctx({}), default=str) is str


def test_resolve_lazy_real_client_when_no_default():
    # Post-cleanup shape: no default supplied -> lazy real NotebookLMClient.
    assert resolve_client_factory(_ctx({}), default=None) is NotebookLMClient


def test_resolve_null_safe_when_ctx_obj_none():
    # A bare ``click.Context`` has ``obj is None`` -- must not raise.
    assert resolve_client_factory(_ctx(None), default=str) is str


def test_resolve_null_safe_when_ctx_none():
    assert resolve_client_factory(None, default=str) is str


def test_resolve_ignores_none_injected_value():
    # The root group seeds ``client_factory=None`` via setdefault; that must fall
    # through to the default, not be returned as the factory.
    assert resolve_client_factory(_ctx({"client_factory": None}), default=str) is str


# ---------------------------------------------------------------------------
# inject_client helper
# ---------------------------------------------------------------------------


def test_inject_client_returns_obj_payload_with_factory():
    client = object()
    obj = inject_client(client)
    assert obj["client_factory"](AsyncMock(), timeout=5) is client


def test_inject_client_records_calls():
    client = object()
    calls: list = []
    factory = inject_client(client, recorder=calls)["client_factory"]
    assert factory("auth-token", timeout=5, chat_timeout=5) is client
    assert calls == [("auth-token", {"timeout": 5, "chat_timeout": 5})]


# ---------------------------------------------------------------------------
# End-to-end: the injected factory reaches construction with no module patch
# ---------------------------------------------------------------------------


def test_inject_client_reaches_command_without_module_patch(
    runner, mock_auth, mock_fetch_tokens
) -> None:
    client = create_mock_client()
    client.labels = AsyncMock()
    client.labels.list = AsyncMock(
        return_value=[Label(id="lblaaa111", name="Papers", emoji="📄", source_ids=["s1"])]
    )
    client.sources.list = AsyncMock(return_value=[Source(id="s1", title="First")])

    result = runner.invoke(
        cli,
        ["label", "list", "-n", "nb_123", "--json"],
        obj=inject_client(client),
        catch_exceptions=False,
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["labels"][0]["id"] == "lblaaa111"
    # The injected client -- not a real NotebookLMClient -- did the RPC.
    client.labels.list.assert_awaited()


def test_inject_client_records_kwargs_passthrough(runner, mock_auth, mock_fetch_tokens) -> None:
    """``source add`` builds ``client_kwargs={'timeout': ...}``; the factory sees it."""
    calls: list = []
    client = create_mock_client()
    client.sources.add_url = AsyncMock(return_value=None)

    # The downstream result handling is irrelevant here -- we only assert that the
    # factory received the timeout the command assembled into ``client_kwargs``
    # (construction happens before result processing, so the recorder is populated).
    runner.invoke(
        cli,
        ["source", "add", "https://example.com/a", "-n", "nb_123", "--timeout", "7", "--json"],
        obj=inject_client(client, recorder=calls),
        catch_exceptions=False,
    )

    assert any(kwargs.get("timeout") == 7 for _auth, kwargs in calls), calls
