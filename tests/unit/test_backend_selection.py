"""Public backend-preference resolution and B8 assembly contract."""

from __future__ import annotations

import inspect
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm._auth.profile_store import ProfileStore
from notebooklm._client_assembly import BackendPreference, resolve_backend_preference
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient


def _auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "sid"},
        csrf_token="csrf",
        session_id="session",
    )


@pytest.mark.parametrize(
    ("explicit", "env", "expected"),
    [
        ("android", "web", BackendPreference("android", "explicit")),
        ("web", "android", BackendPreference("web", "explicit")),
        (None, "android", BackendPreference("android", "env")),
        (None, "web", BackendPreference("web", "env")),
        (None, None, BackendPreference("web", "default")),
    ],
)
def test_resolver_precedence(
    explicit: str | None,
    env: str | None,
    expected: BackendPreference,
) -> None:
    assert resolve_backend_preference(explicit=explicit, env=env) == expected


@pytest.mark.parametrize("value", ["", "mobile", "auto", "Android", " android "])
def test_resolver_rejects_every_unrecognised_spelling(value: str) -> None:
    with pytest.raises(ValueError, match="expected 'web' or 'android'"):
        resolve_backend_preference(explicit=value, env=None)


@pytest.mark.parametrize("value", ["", "mobile", "auto", "Android", " android "])
def test_from_storage_rejects_unrecognised_spelling_before_auth_io(value: str) -> None:
    with pytest.raises(ValueError, match="expected 'web' or 'android'"):
        NotebookLMClient.from_storage(backend=value)  # type: ignore[arg-type]


def test_direct_constructor_explicit_value_beats_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    client = NotebookLMClient(_auth(), backend="web")
    assert client._backend_preference == BackendPreference("web", "explicit")


def test_direct_constructor_explicit_android_beats_web_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")
    client = NotebookLMClient(_auth(), backend="android")
    assert client._backend_preference == BackendPreference("android", "explicit")


def test_direct_constructor_reads_environment_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    client = NotebookLMClient(_auth())
    assert client._backend_preference == BackendPreference("android", "env")


def test_invalid_environment_fails_during_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "mobile")
    with pytest.raises(ValueError, match="aliases 'mobile' and 'auto' are not supported"):
        NotebookLMClient(_auth())


def test_android_preference_does_not_promote_unqualified_namespaces() -> None:
    client = NotebookLMClient(_auth(), backend="android")
    assert isinstance(client.backends, Mapping)
    assert list(client.backends) == [
        "notebooks",
        "sources",
        "artifacts",
        "chat",
        "research",
        "notes",
        "mind_maps",
        "settings",
        "sharing",
        "labels",
        "collections",
    ]
    assert set(client.backends.values()) == {"web"}
    for namespace, backend in client.backends.items():
        installed = getattr(client, namespace)
        assert backend == "web"
        assert type(installed).__module__.startswith("notebooklm._web.")


def test_android_preference_logs_unqualified_namespaces_once(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.INFO, logger="notebooklm.backend"):
        NotebookLMClient(_auth(), backend="android")

    records = [record for record in caplog.records if record.name == "notebooklm.backend"]
    assert [record.levelno for record in records] == [logging.INFO]
    assert [record.getMessage() for record in records] == [
        "Android backend preference selected; unqualified namespaces remain web: "
        "notebooks, sources, artifacts, chat, research, notes, mind_maps, settings, sharing, "
        "labels, collections"
    ]


def test_backends_mapping_is_read_only() -> None:
    client = NotebookLMClient(_auth())
    with pytest.raises(TypeError):
        client.backends["notebooks"] = "android"  # type: ignore[index]


def test_public_backend_parameters_are_optional_keyword_only() -> None:
    constructor = inspect.signature(NotebookLMClient.__init__).parameters
    from_storage = inspect.signature(NotebookLMClient.from_storage).parameters
    for parameters in (constructor, from_storage):
        backend = parameters["backend"]
        assert backend.kind is inspect.Parameter.KEYWORD_ONLY
        assert backend.default is None
        assert "master_token" not in parameters

    assert constructor["backend"].annotation == from_storage["backend"].annotation
    assert constructor["import_research_timeout"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert from_storage["allow_headless"].kind is inspect.Parameter.KEYWORD_ONLY


def test_selection_construction_reads_no_files_tokens_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = MagicMock(side_effect=AssertionError("construction performed I/O"))
    monkeypatch.setattr(Path, "read_text", forbidden)
    monkeypatch.setattr(Path, "read_bytes", forbidden)
    monkeypatch.setattr(Path, "write_text", forbidden)
    monkeypatch.setattr(Path, "write_bytes", forbidden)
    monkeypatch.setattr(ProfileStore, "read_master_token", forbidden)
    monkeypatch.setattr(httpx.AsyncClient, "send", forbidden)

    direct = NotebookLMClient(_auth(), backend="android")
    wrapper = NotebookLMClient.from_storage(path="does-not-exist.json", backend="android")

    assert direct._backend_preference.preferred == "android"
    assert wrapper._client is None
    forbidden.assert_not_called()


async def test_unpromoted_android_preference_does_not_require_token_at_open(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_read = MagicMock(side_effect=AssertionError("unselected Android token was read"))
    monkeypatch.setattr(ProfileStore, "read_master_token", token_read)

    async with NotebookLMClient(_auth(), backend="android") as client:
        assert client.is_connected
        assert set(client.backends.values()) == {"web"}

    token_read.assert_not_called()


def test_from_storage_freezes_environment_preference_at_wrapper_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    wrapper = NotebookLMClient.from_storage()
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")

    assert wrapper._kwargs["backend_preference"] == BackendPreference("android", "env")


async def test_from_storage_threads_explicit_backend(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
) -> None:
    storage = tmp_path / "storage_state.json"
    storage.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "sid", "domain": ".google.com"},
                    {"name": "HSID", "value": "hsid", "domain": ".google.com"},
                    {"name": "SSID", "value": "ssid", "domain": ".google.com"},
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "psidts",
                        "domain": ".google.com",
                    },
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    httpx_mock.add_response(
        url="https://notebook.google.com/",
        content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
    )
    client = await NotebookLMClient.from_storage(path=str(storage), backend="android")._build()
    assert client._backend_preference.preferred == "android"
    assert set(client.backends.values()) == {"web"}
