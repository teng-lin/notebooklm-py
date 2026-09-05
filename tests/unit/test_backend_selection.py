"""Public backend-preference resolution and complete assembly contract."""

from __future__ import annotations

import inspect
import json
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from pytest_httpx import HTTPXMock

import notebooklm._android.auth as android_auth
import notebooklm._auth.psidts_recovery as psidts_recovery
import notebooklm._auth.refresh as auth_refresh
import notebooklm.client as client_module
from notebooklm._android.artifacts import AndroidArtifactsAPI
from notebooklm._android.chat import AndroidChatAPI
from notebooklm._android.collections import AndroidCollectionsAPI
from notebooklm._android.labels import AndroidLabelsAPI
from notebooklm._android.mind_maps import AndroidMindMapsAPI
from notebooklm._android.notebooks import AndroidNotebooksAPI
from notebooklm._android.notes import AndroidNotesAPI
from notebooklm._android.research import AndroidResearchAPI
from notebooklm._android.settings import AndroidSettingsAPI
from notebooklm._android.sharing import AndroidSharingAPI
from notebooklm._android.sources import AndroidSourcesAPI
from notebooklm._auth.master_token_types import MasterToken
from notebooklm._auth.mint_service import MintedOAuthToken
from notebooklm._auth.profile_store import ProfileStore
from notebooklm._auth.tokens import InlineLoadedAuth
from notebooklm._client_assembly import BackendPreference, resolve_backend_preference
from notebooklm._web.transport.cookie_persistence import CookiePersistence
from notebooklm._web.transport.kernel import Kernel
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.exceptions import ConfigurationError, MissingDependencyError
from notebooklm.raw import AndroidRawAPI, WebRawAPI
from notebooklm.types import ConnectionLimits
from tests._helpers.client_factory import build_client_shell_for_tests

_SRC_ROOT = Path(__file__).resolve().parents[2] / "src"


def _backend_subprocess_env() -> dict[str, str]:
    """Prefer this worktree's sources over any concurrently relinked editable install."""
    env = os.environ.copy()
    env.pop("NOTEBOOKLM_BACKEND", None)
    inherited = env.get("PYTHONPATH")
    env["PYTHONPATH"] = os.pathsep.join(part for part in (str(_SRC_ROOT), inherited) if part)
    return env


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


def test_android_preference_promotes_every_namespace() -> None:
    client = NotebookLMClient(_auth(), backend="android")
    assert type(client.raw) is AndroidRawAPI
    assert client._android_runtime is not None
    assert client.raw._transport is client._android_runtime.session
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
    expected_types = {
        "notebooks": AndroidNotebooksAPI,
        "sources": AndroidSourcesAPI,
        "artifacts": AndroidArtifactsAPI,
        "chat": AndroidChatAPI,
        "research": AndroidResearchAPI,
        "notes": AndroidNotesAPI,
        "mind_maps": AndroidMindMapsAPI,
        "settings": AndroidSettingsAPI,
        "sharing": AndroidSharingAPI,
        "labels": AndroidLabelsAPI,
        "collections": AndroidCollectionsAPI,
    }
    for namespace, backend in client.backends.items():
        installed = getattr(client, namespace)
        assert backend == "android"
        assert isinstance(installed, expected_types[namespace])
        assert type(installed).__module__.startswith("notebooklm._android.")

    web_bindings: list[tuple[str, str]] = []
    for namespace in client.backends:
        installed = getattr(client, namespace)
        for attribute, value in vars(installed).items():
            owner = getattr(value, "__self__", None)
            module = type(owner).__module__ if owner is not None else type(value).__module__
            if module.startswith("notebooklm._web."):
                web_bindings.append((namespace, attribute))
    # An Android-selected client holds NO Web collaborator. Keep this empty:
    # a new entry means an Android namespace has quietly taken a dependency on
    # the Web graph, which drags the whole cookie/CSRF machinery back in.
    # History: ``notebooks._remove_from_recent_compat`` and
    # ``sharing._set_view_level_compat`` were retired once live probing showed
    # both routes work natively; ``sources._add_file_compat`` became the
    # Drive-staged upload path.
    assert web_bindings == []

    assert client.collections._list_notebooks.__self__ is client.notebooks
    assert client.collections._list_notebooks.__func__ is type(client.notebooks).list


def test_web_preference_installs_web_raw_namespace() -> None:
    client = NotebookLMClient(_auth(), backend="web")

    assert type(client.raw) is WebRawAPI
    assert client.raw._rpc is client._web_runtime.executor


def test_android_chat_receives_configured_response_byte_cap() -> None:
    client = NotebookLMClient(
        _auth(),
        backend="android",
        chat_response_max_bytes=123456,
    )

    assert client.chat._chat_response_max_bytes == 123456


def test_android_assembly_wires_no_web_file_upload_collaborator() -> None:
    """Files outside the mobile upload allowlist round-trip through Drive.

    ``_add_file_compat`` stays as an injection seam for direct adapter callers,
    but public assembly leaves it unset so nothing reaches back into the Web
    graph.
    """
    client = NotebookLMClient(_auth(), backend="android")

    assert client.sources._add_file_compat is None


def test_android_selected_public_callable_inventory_is_exact() -> None:
    client = NotebookLMClient(_auth(), backend="android")
    expected_names = {
        "notebooks": {
            "copy",
            "create",
            "delete",
            "get",
            "get_description",
            "get_metadata",
            "get_or_none",
            "get_raw",
            "get_share_url",
            "get_source_ids",
            "get_summary",
            "list",
            "remove_from_recent",
            "rename",
            "set_emoji",
            "suggest_prompts",
            "suggest_next_steps",
            "update",
        },
        "sources": {
            "add_drive",
            "add_drive_file",
            "add_file",
            "add_play_book",
            "list_play_books",
            "add_text",
            "add_url",
            "add_urls_async",
            "append_text",
            "copy",
            "check_freshness",
            "delete",
            "delete_many",
            "get",
            "get_fulltext",
            "get_guide",
            "get_or_none",
            "list",
            "refresh",
            "rename",
            "search",
            "wait_all_until_ready",
            "wait_for_sources",
            "wait_until_ready",
            "wait_until_registered",
        },
        "artifacts": {
            "delete",
            "download_audio",
            "download_data_table",
            "download_flashcards",
            "download_infographic",
            "download_mind_map",
            "download_quiz",
            "download_report",
            "download_slide_deck",
            "download_video",
            "export",
            "export_data_table",
            "export_report",
            "generate_audio",
            "generate_cinematic_video",
            "generate_data_table",
            "generate_flashcards",
            "generate_infographic",
            "generate_mind_map",
            "generate_quiz",
            "generate_report",
            "generate_slide_deck",
            "generate_study_guide",
            "generate_video",
            "get",
            "get_or_none",
            "get_prompt",
            "list",
            "list_audio",
            "list_data_tables",
            "list_flashcards",
            "list_infographics",
            "list_quizzes",
            "list_reports",
            "list_slide_decks",
            "list_video",
            "poll_status",
            "rename",
            "retry_failed",
            "revise_slide",
            "suggest_reports",
            "copy",
            "get_customization_choices",
            "wait_for_completion",
        },
        "chat": {
            "ask",
            "cache_size",
            "cancel",
            "clear_cache",
            "configure",
            "delete_conversation",
            "get_cached_turns",
            "get_conversation_id",
            "get_conversation_turns",
            "get_history",
            "get_settings",
            "save_answer_as_note",
            "session_status",
            "set_mode",
        },
        "research": {
            "cancel",
            "discover",
            "extract_report_urls",
            "import_sources",
            "import_sources_with_verification",
            "poll",
            "select_cited_sources",
            "start",
            "wait_for_completion",
        },
        "notes": {
            "create",
            "delete",
            "delete_mind_map",
            "get",
            "get_or_none",
            "list",
            "list_mind_maps",
            "update",
        },
        "mind_maps": {
            "delete",
            "generate",
            "get",
            "get_or_none",
            "get_tree",
            "list",
            "list_note_backed",
            "rename",
        },
        "settings": {
            "get_account_limits",
            "get_output_language",
            "get_usage",
            "get_user_settings",
            "set_output_language",
        },
        "sharing": {
            "add_user",
            "get_status",
            "remove_user",
            "set_public",
            "set_users",
            "set_view_level",
            "update_user",
        },
        "labels": {
            "add_sources",
            "create",
            "delete",
            "generate",
            "get",
            "get_or_none",
            "list",
            "remove_sources",
            "rename",
            "set_emoji",
            "sources",
            "update",
        },
        "collections": {
            "add_notebooks",
            "create",
            "delete",
            "get",
            "get_or_none",
            "list",
            "notebooks",
            "remove_notebooks",
            "rename",
        },
    }

    observed_names = {
        namespace: {
            name
            for name, member in inspect.getmembers(type(getattr(client, namespace)))
            if not name.startswith("_")
            and name not in {"reset_after_open", "set_bound_loop"}
            and callable(member)
        }
        for namespace in client.backends
    }

    assert observed_names == expected_names
    assert sum(map(len, observed_names.values())) == 159


@pytest.mark.parametrize("backend", [None, "web"])
def test_default_and_explicit_web_keep_every_namespace_on_web(backend: str | None) -> None:
    client = NotebookLMClient(_auth(), backend=backend)  # type: ignore[arg-type]
    assert set(client.backends.values()) == {"web"}
    assert client._android_runtime is None


def test_web_construction_avoids_android_and_optional_runtime_imports() -> None:
    script = """
import sys

before = set(sys.modules)
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

client = NotebookLMClient(
    AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session")
)
assert set(client.backends.values()) == {"web"}
delta = set(sys.modules) - before
assert not any(name.startswith("notebooklm._android") for name in delta)
assert not any(
    name == "grpc"
    or name.startswith("grpc.")
    or name == "gpsoauth"
    or name.startswith("gpsoauth.")
    or name == "google.protobuf"
    or name.startswith("google.protobuf.")
    for name in delta
)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=_backend_subprocess_env(),
    )
    assert completed.returncode == 0, completed.stderr


def test_android_construction_defers_optional_runtime_imports_to_open() -> None:
    script = """
import sys

before = set(sys.modules)
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

client = NotebookLMClient(
    AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
    backend="android",
)
assert client.backends["collections"] == "android"
delta = set(sys.modules) - before
assert not any(
    name == "grpc"
    or name.startswith("grpc.")
    or name == "gpsoauth"
    or name.startswith("gpsoauth.")
    or name == "google.protobuf"
    or name.startswith("google.protobuf.")
    for name in delta
)
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
        env=_backend_subprocess_env(),
    )
    assert completed.returncode == 0, completed.stderr


def test_android_construction_loads_no_web_module() -> None:
    script = (
        f"import sys\nsys.path.insert(0, {str(_SRC_ROOT)!r})\n"
        + """
import json
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

NotebookLMClient(
    AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
    backend="android",
)
after = {name for name in sys.modules if name.startswith("notebooklm._web")}
print(json.dumps(sorted(after)))
"""
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == []


def test_android_construction_retains_web_seam_overrides_without_resolving_defaults() -> None:
    client = NotebookLMClient(_auth(), backend="android")

    assert client._seams.decode_response is None
    assert client._seams.sleep is None
    assert client._seams.is_auth_error is None


def test_android_global_synthetic_error_guard_is_web_free() -> None:
    script = (
        f"import sys\nsys.path.insert(0, {str(_SRC_ROOT)!r})\n"
        + """
import os
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient

os.environ["NOTEBOOKLM_VCR_RECORD_ERRORS"] = "5xx"
os.environ.pop("PYTEST_CURRENT_TEST", None)
try:
    NotebookLMClient(
        AuthTokens(cookies={"SID": "sid"}, csrf_token="csrf", session_id="session"),
        backend="android",
    )
except RuntimeError as error:
    assert "NOTEBOOKLM_VCR_RECORD_ERRORS" in str(error)
else:
    raise AssertionError("global synthetic-error guard did not refuse construction")
assert not [name for name in sys.modules if name.startswith("notebooklm._web")]
"""
    )
    subprocess.run(
        [sys.executable, "-I", "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"rate_limit_max_retries": -1}, "rate_limit_max_retries must be >= 0, got -1"),
        ({"server_error_max_retries": -1}, "server_error_max_retries must be >= 0, got -1"),
        ({"max_concurrent_uploads": 0}, "max_concurrent_uploads must be >= 1, got 0"),
    ],
)
def test_android_owned_validation_preserves_exact_errors(
    kwargs: dict[str, int],
    message: str,
) -> None:
    with pytest.raises(ValueError) as raised:
        NotebookLMClient(_auth(), backend="android", **kwargs)  # type: ignore[arg-type]

    assert str(raised.value) == message


def test_android_preference_has_no_unqualified_namespace_log(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.INFO, logger="notebooklm.backend"):
        NotebookLMClient(_auth(), backend="android")

    records = [record for record in caplog.records if record.name == "notebooklm.backend"]
    assert records == []


@pytest.mark.parametrize(
    ("limits", "max_concurrent_rpcs"),
    [
        (ConnectionLimits(max_connections=1, max_keepalive_connections=1), 16),
        (None, 101),
    ],
)
def test_android_rpc_cap_ignores_absent_web_pool_width(
    limits: ConnectionLimits | None,
    max_concurrent_rpcs: int,
) -> None:
    client = NotebookLMClient(
        _auth(),
        backend="android",
        limits=limits,
        max_concurrent_rpcs=max_concurrent_rpcs,
    )

    assert client._collaborators.call_supervisor._max_concurrent_rpcs == max_concurrent_rpcs


def test_web_rpc_cap_remains_bounded_by_the_http_pool() -> None:
    with pytest.raises(ValueError, match="max_concurrent_rpcs must be <="):
        NotebookLMClient(
            _auth(),
            backend="web",
            limits=ConnectionLimits(max_connections=1, max_keepalive_connections=1),
            max_concurrent_rpcs=16,
        )


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
    assert client._android_runtime is not None
    session = client._android_runtime.session
    provider = client._android_runtime.bearer_provider
    token_read = MagicMock(return_value=None)
    provider._master_token_reader.read_master_token = token_read
    session._grpc_loader = lambda: object()
    session._protobuf_loader = lambda: object()
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())

    token_read.assert_not_called()
    with pytest.raises(ConfigurationError, match="master-token profile"):
        await client.__aenter__()
    token_read.assert_called_once_with()


async def test_selected_android_missing_dependency_fails_at_open_not_construction() -> None:
    client = NotebookLMClient(_auth(), backend="android")
    assert client._android_runtime is not None
    missing = MissingDependencyError("missing android runtime")
    session = client._android_runtime.session
    provider = client._android_runtime.bearer_provider
    token_read = MagicMock(side_effect=AssertionError("credential read preceded dependency check"))
    protobuf_load = MagicMock(side_effect=AssertionError("protobuf loaded after grpc failed"))
    provider._master_token_reader.read_master_token = token_read
    session._grpc_loader = MagicMock(side_effect=missing)
    session._protobuf_loader = protobuf_load

    with pytest.raises(MissingDependencyError, match="missing android runtime"):
        await client.__aenter__()

    protobuf_load.assert_not_called()
    token_read.assert_not_called()


def test_android_assembly_accepts_only_narrow_primary_credentials() -> None:
    from notebooklm._android.assembly import assemble_android_backend

    parameters = inspect.signature(assemble_android_backend).parameters

    assert "auth" not in parameters
    assert tuple(parameters) == ("shared", "config", "credentials", "deps")


async def test_selected_android_open_binds_auth_and_session_without_eager_channel(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage_state.json"
    client = NotebookLMClient(_auth(), storage_path=storage, backend="android")
    assert client._android_runtime is not None
    session = client._android_runtime.session
    provider = client._android_runtime.bearer_provider
    session._grpc_loader = lambda: object()
    session._protobuf_loader = lambda: object()
    token_read = MagicMock(
        return_value=MasterToken(email="test@example.com", android_id="1234", secret="secret")
    )
    provider._master_token_reader.read_master_token = token_read
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())

    await client.__aenter__()
    try:
        assert client.is_connected
        assert session.active_epoch is not None
        assert session._channel is None
        assert provider._master_token is not None
    finally:
        await client.close()

    assert session.active_epoch is None
    assert provider._master_token is None
    token_read.assert_called_once_with()


async def test_android_primary_auth_uses_only_explicit_reader_and_minter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Poisoned Web values cannot participate in Android open or typed refresh."""

    class PoisonedWebAuth:
        storage_path = tmp_path / "storage_state.json"

        def __getattr__(self, name: str) -> object:
            if name in {"cookies", "cookie_jar", "csrf_token", "session_id"}:
                raise AssertionError(f"Android read Web session field {name}")
            raise AttributeError(name)

    class RecordingReader:
        def __init__(self) -> None:
            self.reads = 0

        def read_master_token(self) -> MasterToken:
            self.reads += 1
            return MasterToken(
                email="android@example.com",
                android_id="1234",
                secret="master-secret",
            )

    class RecordingMinter:
        def __init__(self) -> None:
            self.calls = 0

        async def mint_oauth(self, master_token: MasterToken, spec: object) -> MintedOAuthToken:
            del spec
            assert master_token.email == "android@example.com"
            self.calls += 1
            return MintedOAuthToken(token="bearer-secret", expires_at=None)

    auth = PoisonedWebAuth()
    reader = RecordingReader()
    minter = RecordingMinter()
    client = build_client_shell_for_tests(
        auth,  # type: ignore[arg-type]
        backend="android",
        master_token_reader=reader,
        oauth_minter=minter,
    )
    assert client.auth is auth
    assert client._android_runtime is not None
    session = client._android_runtime.session
    session._grpc_loader = lambda: object()
    session._protobuf_loader = lambda: object()
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())

    assert reader.reads == 0
    assert minter.calls == 0
    await client.__aenter__()
    try:
        assert reader.reads == 1
        assert minter.calls == 0
        assert await client.refresh_auth() is auth
        assert minter.calls == 1
        assert client._web_sidecar is not None
        assert not client._web_sidecar.is_materialized
    finally:
        await client.close()


def test_android_selection_extends_the_frozen_lifecycle_ownership_graph() -> None:
    client = NotebookLMClient(_auth(), backend="android")
    lifecycle = client._lifecycle
    assert client._android_runtime is not None
    android = client._android_runtime
    assert client._web_runtime is None
    assert client._web_sidecar is not None
    assert lifecycle._transports == (
        android.session,
        android.asset_downloads,
        android.upload_pipeline,
        android.phenotype,
        client._web_sidecar,
    )
    assert lifecycle._loop_participants == (
        client._collaborators.call_supervisor,
        client.chat,
        android.bearer_provider,
        android.session,
        android.upload_pipeline,
        client._web_sidecar,
    )


async def test_android_open_without_deprecated_hatch_constructs_no_web_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The primary Android lifecycle is Web-allocation and cookie-write free."""

    forbidden = MagicMock(side_effect=AssertionError("Android constructed a Web owner"))
    monkeypatch.setattr(Kernel, "__init__", forbidden)
    monkeypatch.setattr(CookiePersistence, "_from_store", forbidden)
    monkeypatch.setattr(android_auth, "_require_gpsoauth", lambda: object())
    cookie_saver = MagicMock()
    auth = AuthTokens(
        cookies={},
        csrf_token="",
        session_id="",
        storage_path=tmp_path / "storage_state.json",
        cookie_jar=httpx.Cookies(),
    )
    client = NotebookLMClient(
        auth,
        backend="android",
        keepalive=1.0,
        cookie_saver=cookie_saver,
    )
    assert client._android_runtime is not None
    client._android_runtime.bearer_provider._master_token_reader.read_master_token = MagicMock(
        return_value=MasterToken(email="test@example.com", android_id="1234", secret="secret")
    )
    client._android_runtime.session._grpc_loader = lambda: object()
    client._android_runtime.session._protobuf_loader = lambda: object()

    await client.__aenter__()
    await client.close()

    assert client._web_runtime is None
    assert client._web_sidecar is not None
    assert not client._web_sidecar.is_materialized
    forbidden.assert_not_called()
    cookie_saver.assert_not_called()


def test_from_storage_freezes_environment_preference_at_wrapper_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    wrapper = NotebookLMClient.from_storage()
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")

    assert wrapper._kwargs["backend_preference"] == BackendPreference("android", "env")


async def test_from_storage_handoff_skips_nested_client_in_subclass_constructor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_auth = _auth()

    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(loaded_auth)

    class ClientWithNestedConstruction(NotebookLMClient):
        def __init__(self, auth: AuthTokens, **kwargs: object) -> None:
            self.nested = NotebookLMClient(_auth())
            super().__init__(auth, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    wrapper = ClientWithNestedConstruction.from_storage()
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")

    client = await wrapper._build()

    assert isinstance(client, ClientWithNestedConstruction)
    assert client._backend_preference == BackendPreference("android", "env")
    assert client.nested._backend_preference == BackendPreference("web", "env")


async def test_from_storage_handoff_supports_non_cooperative_subclass_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_auth = _auth()

    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(loaded_auth)

    class NonCooperativeNewClient(NotebookLMClient):
        def __new__(cls, auth: AuthTokens, **kwargs: object) -> NonCooperativeNewClient:
            instance = object.__new__(cls)
            instance.allocated_auth = auth
            instance.allocated_backend = kwargs["backend"]
            return instance

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")
    context = NonCooperativeNewClient.from_storage()
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")

    client = await context._build()

    assert isinstance(client, NonCooperativeNewClient)
    assert client.allocated_auth is loaded_auth
    assert client.allocated_backend == "web"
    assert client._backend_preference == BackendPreference("web", "env")


async def test_from_storage_handoff_survives_recursive_construction_inside_new(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_auth = _auth()

    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(loaded_auth)

    class RecursiveNewClient(NotebookLMClient):
        allocating_outer = False
        nested: RecursiveNewClient

        def __new__(cls, auth: AuthTokens, **kwargs: object) -> RecursiveNewClient:
            if not cls.allocating_outer:
                cls.allocating_outer = True
                try:
                    cls.nested = cls(auth, **kwargs)  # type: ignore[arg-type]
                finally:
                    cls.allocating_outer = False
            return object.__new__(cls)

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")
    context = RecursiveNewClient.from_storage()
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")

    client = await context._build()

    assert isinstance(client, RecursiveNewClient)
    assert client._backend_preference == BackendPreference("web", "env")
    assert RecursiveNewClient.nested._backend_preference == BackendPreference("web", "explicit")


async def test_from_storage_passes_auth_and_options_to_mi_allocator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_auth = _auth()

    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(loaded_auth)

    class RequiredArgsAllocator:
        def __new__(
            cls, auth: AuthTokens, timeout: float, **_kwargs: object
        ) -> RequiredArgsAllocator:
            instance = super().__new__(cls)
            instance.allocator_auth = auth
            instance.allocator_marker = timeout
            return instance

    class MultiInheritanceClient(NotebookLMClient, RequiredArgsAllocator):
        pass

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")
    context = MultiInheritanceClient.from_storage(timeout=47.0)
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")

    client = await context._build()

    assert isinstance(client, MultiInheritanceClient)
    assert client.allocator_auth is loaded_auth
    assert client.allocator_marker == 47.0
    assert client._backend_preference == BackendPreference("web", "env")


async def test_from_storage_mirrors_non_instance_new_and_non_none_init_semantics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loaded_auth = _auth()
    sentinel = object()

    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(loaded_auth)

    class NonInstanceClient(NotebookLMClient):
        def __new__(cls, auth: AuthTokens, **kwargs: object) -> object:
            del cls, auth, kwargs
            return sentinel

    class NonNoneInitClient(NotebookLMClient):
        def __init__(self, auth: AuthTokens, **kwargs: object) -> int:
            del self, auth, kwargs
            return 1

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)

    assert await NonInstanceClient.from_storage()._build() is sentinel
    with pytest.raises(TypeError, match="__init__.*should return None, not 'int'"):
        await NonNoneInitClient.from_storage()._build()


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
    assert set(client.backends.values()) == {"android"}
    assert client.auth.csrf_token == "csrf"
    assert client.auth.session_id == "session"
    assert [(request.method, str(request.url)) for request in httpx_mock.get_requests()] == [
        ("GET", "https://notebook.google.com/")
    ]


async def test_android_from_storage_uses_name_only_psidts_policy(
    tmp_path: Path,
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = tmp_path / "storage_state.json"
    stored_payload = json.dumps(
        {
            "cookies": [
                {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
                {
                    "name": "HSID",
                    "value": "hsid",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "SSID",
                    "value": "ssid",
                    "domain": ".google.com",
                    "path": "/",
                },
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "present-but-not-routable",
                    "domain": ".google.com",
                    "path": "/not-rotate-cookies",
                },
            ],
            "origins": [],
        }
    )
    storage.write_text(stored_payload, encoding="utf-8")
    recovery = MagicMock(side_effect=AssertionError("Android entered PSIDTS recovery"))
    poke = AsyncMock(side_effect=AssertionError("Android poked PSIDTS"))
    web_ladder = AsyncMock(side_effect=AssertionError("Android entered Web auth recovery"))
    merge = MagicMock(side_effect=AssertionError("Android persisted a cookie observation"))
    monkeypatch.setattr(psidts_recovery, "load_with_recovery", recovery)
    monkeypatch.setattr(auth_refresh, "_poke_session", poke)
    monkeypatch.setattr(auth_refresh, "_fetch_tokens_with_exact_baseline", web_ladder)
    monkeypatch.setattr(ProfileStore, "merge_cookie_observation", merge)
    httpx_mock.add_response(
        url="https://notebook.google.com/",
        content=b'"SNlM0e":"csrf" "FdrFJe":"session"',
        headers={"Set-Cookie": "SID=fresh; Domain=.google.com; Path=/"},
    )

    client = await NotebookLMClient.from_storage(path=str(storage), backend="android")._build()

    assert client._android_runtime is not None
    assert client._web_runtime is None
    assert client.auth.cookie_jar is not None
    assert client.auth.cookie_jar.get("SID", domain=".google.com", path="/") == "fresh"
    assert client.auth.csrf_token == "csrf"
    assert client.auth.session_id == "session"
    assert storage.read_text(encoding="utf-8") == stored_payload
    assert [(request.method, str(request.url)) for request in httpx_mock.get_requests()] == [
        ("GET", "https://notebook.google.com/")
    ]
    recovery.assert_not_called()
    poke.assert_not_called()
    web_ladder.assert_not_called()
    merge.assert_not_called()
