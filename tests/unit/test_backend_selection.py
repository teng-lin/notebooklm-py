"""Public backend-preference resolution and B8 assembly contract."""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from pytest_httpx import HTTPXMock

import notebooklm._android.auth as android_auth
from notebooklm._android.artifacts import AndroidArtifactsAPI
from notebooklm._android.collections import AndroidCollectionsAPI
from notebooklm._android.mind_maps import AndroidMindMapsAPI
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._client_assembly import BackendPreference, resolve_backend_preference
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import ConfigurationError, MissingDependencyError


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


def test_android_preference_promotes_qualified_artifact_namespaces() -> None:
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
    assert client.backends["collections"] == "android"
    assert client.backends["artifacts"] == "android"
    assert client.backends["mind_maps"] == "android"
    assert isinstance(client.collections, AndroidCollectionsAPI)
    assert isinstance(client.artifacts, AndroidArtifactsAPI)
    assert isinstance(client.mind_maps, AndroidMindMapsAPI)
    for namespace, backend in client.backends.items():
        installed = getattr(client, namespace)
        expected = "android" if namespace in {"artifacts", "mind_maps", "collections"} else "web"
        assert backend == expected
        assert type(installed).__module__.startswith(f"notebooklm._{expected}.")

    assert client.collections._list_notebooks.__self__ is client.notebooks
    assert client.collections._list_notebooks.__func__ is type(client.notebooks).list


@pytest.mark.parametrize("backend", [None, "web"])
def test_default_and_explicit_web_keep_every_namespace_on_web(backend: str | None) -> None:
    client = NotebookLMClient(_auth(), backend=backend)  # type: ignore[arg-type]
    assert set(client.backends.values()) == {"web"}
    assert client._android_bearer_provider is None
    assert client._android_session is None


def test_default_web_construction_does_not_import_android_or_optional_runtime() -> None:
    script = """
import builtins

original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if (
        name == "grpc"
        or name == "gpsoauth"
        or name.startswith("google.protobuf")
        or name.startswith("notebooklm._android")
    ):
        raise AssertionError(f"default Web construction imported {name}")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

client = NotebookLMClient(
    AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session")
)
assert set(client.backends.values()) == {"web"}
"""
    env = os.environ.copy()
    env.pop("NOTEBOOKLM_BACKEND", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr


def test_android_construction_defers_optional_runtime_imports_to_open() -> None:
    script = """
import builtins

original_import = builtins.__import__

def guarded_import(name, *args, **kwargs):
    if name == "grpc" or name == "gpsoauth" or name.startswith("google.protobuf"):
        raise AssertionError(f"Android construction imported {name}")
    return original_import(name, *args, **kwargs)

builtins.__import__ = guarded_import
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

client = NotebookLMClient(
    AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
    backend="android",
)
assert client.backends["collections"] == "android"
"""
    env = os.environ.copy()
    env.pop("NOTEBOOKLM_BACKEND", None)
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )
    assert completed.returncode == 0, completed.stderr


def test_android_preference_logs_unqualified_namespaces_once(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.INFO, logger="notebooklm.backend"):
        NotebookLMClient(_auth(), backend="android")

    records = [record for record in caplog.records if record.name == "notebooklm.backend"]
    assert [record.levelno for record in records] == [logging.INFO]
    assert [record.getMessage() for record in records] == [
        "Android backend preference selected; unqualified namespaces remain web: "
        "notebooks, sources, chat, research, notes, settings, sharing, labels"
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
    assert isinstance(direct.collections, AndroidCollectionsAPI)
    assert wrapper._client is None
    forbidden.assert_not_called()


async def test_selected_android_reads_token_only_at_open_and_fails_without_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = NotebookLMClient(_auth(), backend="android")
    assert client._android_session is not None
    assert client._android_bearer_provider is not None
    token_read = MagicMock(return_value=None)
    client._android_bearer_provider._profile_store.read_master_token = token_read
    client._android_session._grpc_loader = lambda: object()
    client._android_session._protobuf_loader = lambda: object()
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())

    token_read.assert_not_called()
    with pytest.raises(ConfigurationError, match="master-token profile"):
        await client.__aenter__()
    token_read.assert_called_once_with()


async def test_selected_android_missing_dependency_fails_at_open_not_construction() -> None:
    client = NotebookLMClient(_auth(), backend="android")
    assert client._android_session is not None
    missing = MissingDependencyError("missing android runtime")
    client._android_session._grpc_loader = MagicMock(side_effect=missing)

    with pytest.raises(MissingDependencyError, match="missing android runtime"):
        await client.__aenter__()


async def test_selected_android_open_binds_auth_and_session_without_eager_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage_state.json"
    client = NotebookLMClient(_auth(), storage_path=storage, backend="android")
    assert client._android_session is not None
    assert client._android_bearer_provider is not None
    client._android_session._grpc_loader = lambda: object()
    client._android_session._protobuf_loader = lambda: object()
    token_read = MagicMock(
        return_value=MasterToken(email="test@example.com", android_id="1234", secret="secret")
    )
    client._android_bearer_provider._profile_store.read_master_token = token_read
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())

    await client.__aenter__()
    try:
        assert client.is_connected
        assert client._android_session.active_epoch is not None
        assert client._android_session._channel is None
        assert client._android_bearer_provider._master_token is not None
    finally:
        await client.close()

    assert client._android_session.active_epoch is None
    assert client._android_bearer_provider._master_token is None
    token_read.assert_called_once_with()


def test_android_selection_extends_the_frozen_lifecycle_ownership_graph() -> None:
    client = NotebookLMClient(_auth(), backend="android")
    lifecycle = client._collaborators.lifecycle
    assert client._android_session is not None
    assert client._android_bearer_provider is not None
    assert lifecycle._transports == (
        client._collaborators.web_transport,
        client._source_uploader,
        client._android_session,
        client.artifacts._asset_downloads,
    )
    assert lifecycle._loop_participants[-2:] == (
        client._android_bearer_provider,
        client._android_session,
    )


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
    assert client.backends["collections"] == "android"
    assert sum(backend == "android" for backend in client.backends.values()) == 3
