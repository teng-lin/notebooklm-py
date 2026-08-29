"""CLI backend option and real-factory threading contract."""

from __future__ import annotations

from typing import Any

import click

import notebooklm.client as client_module
from notebooklm.cli.auth_runtime import resolve_client_factory
from notebooklm.cli.completion import CompletionProvider
from notebooklm.notebooklm_cli import cli


def _ctx(obj: dict[str, Any]) -> click.Context:
    ctx = click.Context(click.Command("test"))
    ctx.obj = obj
    return ctx


def test_explicit_backend_is_merged_into_real_factory_kwargs() -> None:
    seen: dict[str, object] = {}

    def factory(auth: object, **kwargs: object) -> object:
        seen["auth"] = auth
        seen.update(kwargs)
        return object()

    resolved = resolve_client_factory(_ctx({"backend": "android"}), default=factory)
    resolved("auth", timeout=12)
    assert seen == {"auth": "auth", "timeout": 12, "backend": "android"}


def test_explicit_backend_does_not_reparameterize_injected_factory() -> None:
    calls: list[object] = []

    def injected(auth: object) -> object:
        calls.append(auth)
        return object()

    resolved = resolve_client_factory(
        _ctx({"backend": "android", "client_factory": injected}),
        default=str,
    )
    assert resolved is injected
    resolved("auth")
    assert calls == ["auth"]


def test_root_backend_choice_and_rejected_aliases(runner) -> None:  # type: ignore[no-untyped-def]
    accepted = runner.invoke(cli, ["--backend", "android", "completion", "bash"])
    assert accepted.exit_code == 0, accepted.output

    for rejected in ("mobile", "auto"):
        result = runner.invoke(cli, ["--backend", rejected, "completion", "bash"])
        assert result.exit_code == 2
        assert "Invalid value for '--backend'" in result.output


def test_shell_completion_threads_backend_only_to_default_factory(
    monkeypatch,
) -> None:  # type: ignore[no-untyped-def]
    seen: list[tuple[object, dict[str, object]]] = []

    def default_factory(auth: object, **kwargs: object) -> object:
        seen.append((auth, kwargs))
        return object()

    monkeypatch.setattr(client_module, "NotebookLMClient", default_factory)
    ctx = _ctx({"backend": "android"})
    CompletionProvider()._make_client("auth", ctx)
    assert seen == [("auth", {"backend": "android"})]

    injected_calls: list[object] = []
    provider = CompletionProvider(
        client_factory=lambda auth: injected_calls.append(auth) or object()
    )
    provider._make_client("injected-auth", ctx)
    assert injected_calls == ["injected-auth"]
