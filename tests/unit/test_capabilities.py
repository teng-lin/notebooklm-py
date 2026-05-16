"""Unit tests for private feature capability adapters."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import httpx
import pytest

from notebooklm._capabilities import ClientCoreCapabilities
from notebooklm._core_polling import PollRegistry
from notebooklm.auth import authuser_query, format_authuser_value


class _ExplodingCore:
    def __getattribute__(self, name: str) -> object:
        raise AssertionError(f"core attribute read during construction: {name}")


def test_client_core_capabilities_construction_is_lazy() -> None:
    adapter = ClientCoreCapabilities(_ExplodingCore())

    assert isinstance(adapter, ClientCoreCapabilities)


def test_client_core_capabilities_returns_existing_poll_registry_and_pending_mapping() -> None:
    pending = {}
    registry = PollRegistry(pending)
    core = SimpleNamespace(poll_registry=registry)
    adapter = ClientCoreCapabilities(core)

    assert adapter.poll_registry is registry
    assert adapter.poll_registry.pending is pending


@pytest.mark.parametrize(
    ("authuser", "account_email"),
    [
        (2, None),
        (2, "user+test@example.com"),
        (3, " selected.account@example.com "),
    ],
)
def test_client_core_capabilities_exposes_auth_route_helpers(
    authuser: int,
    account_email: str | None,
) -> None:
    core = SimpleNamespace(auth=SimpleNamespace(authuser=authuser, account_email=account_email))
    adapter = ClientCoreCapabilities(core)

    assert adapter.authuser == authuser
    assert adapter.account_email == account_email
    assert adapter.authuser_query() == authuser_query(authuser, account_email)
    assert adapter.authuser_header() == format_authuser_value(authuser, account_email)


def test_client_core_capabilities_authuser_query_url_encodes_account_email() -> None:
    core = SimpleNamespace(auth=SimpleNamespace(authuser=2, account_email="user+test@example.com"))
    adapter = ClientCoreCapabilities(core)

    assert adapter.authuser_header() == "user+test@example.com"
    assert adapter.authuser_query() == "authuser=user%2Btest%40example.com"


def test_client_core_capabilities_live_cookies_come_from_http_client() -> None:
    live_cookies = httpx.Cookies()
    auth_cookies = httpx.Cookies()
    core = MagicMock()
    core.auth.cookie_jar = auth_cookies
    core.get_http_client.return_value.cookies = live_cookies
    adapter = ClientCoreCapabilities(core)

    assert adapter.live_cookies() is live_cookies
    assert adapter.live_cookies() is not auth_cookies


def test_capabilities_module_does_not_import_client_core_at_runtime() -> None:
    source = (Path(__file__).resolve().parents[2] / "src/notebooklm/_capabilities.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)

    forbidden_imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in {"_core", "notebooklm._core"}:
            forbidden_imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            forbidden_imports.extend(
                alias.name for alias in node.names if alias.name == "notebooklm._core"
            )

    assert forbidden_imports == []
