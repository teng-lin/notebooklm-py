"""Owner-specific client configuration and the flat 0.x compatibility boundary."""

from __future__ import annotations

import dataclasses
import inspect
import warnings

import httpx
import pytest

from notebooklm import client as client_module
from notebooklm._auth.tokens import InlineLoadedAuth
from notebooklm._client_assembly import BackendPreference
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from notebooklm.options import (
    AUTO,
    AndroidBackendConfig,
    AutoReadWindow,
    ClientConfig,
    FeatureOptions,
    RetryOptions,
    RuntimeOptions,
    TimeoutOptions,
    TransferOptions,
    WebBackendConfig,
    WebSessionHooks,
    WebSessionOptions,
    WebTransportOptions,
)
from notebooklm.types import ConnectionLimits


@pytest.fixture()
def auth() -> AuthTokens:
    return AuthTokens(cookies={"SID": "secret"}, csrf_token="csrf", session_id="session")


def test_public_option_records_are_frozen_and_defaults_are_independent() -> None:
    first = ClientConfig()
    second = ClientConfig()
    assert first == second
    assert first.runtime is not second.runtime
    assert first.retry is not second.retry
    assert first.transfers is not second.transfers
    assert first.features is not second.features
    assert first.features.chat_timeout is AUTO is AutoReadWindow.AUTO
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.runtime = RuntimeOptions()  # type: ignore[misc]


def test_timeout_options_require_all_components_and_validate_values() -> None:
    assert str(inspect.signature(TimeoutOptions)) == (
        "(connect: 'float | None', read: 'float | None', write: 'float | None', "
        "pool: 'float | None') -> None"
    )
    assert TimeoutOptions(None, None, None, None).read is None
    with pytest.raises(ValueError, match="positive, finite"):
        TimeoutOptions(0, None, None, None)


def test_explicit_backend_config_wins_over_environment(
    auth: AuthTokens, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    web = NotebookLMClient(auth, config=ClientConfig(backend=WebBackendConfig()))
    assert set(web.backends.values()) == {"web"}
    assert web._backend_preference.reason == "explicit"


def test_operation_timeout_is_retained_by_the_shared_runtime_owner(auth: AuthTokens) -> None:
    client = NotebookLMClient(
        auth,
        config=ClientConfig(runtime=RuntimeOptions(operation_timeout=123.0)),
    )
    assert client._collaborators.config.operation_timeout == 123.0


def test_unspecified_typed_backend_follows_environment(
    auth: AuthTokens, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    android = NotebookLMClient(auth, config=ClientConfig())
    assert set(android.backends.values()) == {"android"}
    assert android._backend_preference.reason == "env"


def test_config_rejects_nondefault_legacy_tuning_but_allows_old_defaults(
    auth: AuthTokens,
) -> None:
    with pytest.raises(TypeError, match="timeout"):
        NotebookLMClient(auth, timeout=31.0, config=ClientConfig())
    client = NotebookLMClient(
        auth,
        timeout=30.0,
        max_concurrent_uploads=4,
        backend=None,
        config=ClientConfig(),
    )
    assert set(client.backends.values()) == {"web"}

    with pytest.raises(TypeError, match="timeout"):
        NotebookLMClient.from_storage(timeout=31.0, config=ClientConfig())


def test_default_from_storage_tuning_is_silent() -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        NotebookLMClient.from_storage(profile="work")


def test_client_config_in_timeout_position_gets_targeted_error(auth: AuthTokens) -> None:
    with pytest.raises(TypeError, match="keyword-only.*config="):
        NotebookLMClient(auth, ClientConfig())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="keyword-only.*config="):
        NotebookLMClient.from_storage(None, ClientConfig())  # type: ignore[arg-type]


def test_typed_web_values_reach_only_their_owners(auth: AuthTokens) -> None:
    phase = TimeoutOptions(connect=1.0, read=2.0, write=3.0, pool=4.0)
    limits = WebTransportOptions().limits
    client = NotebookLMClient(
        auth,
        config=ClientConfig(
            backend=WebBackendConfig(
                transport=WebTransportOptions(
                    read_timeout=11.0,
                    write_timeout=12.0,
                    pool_timeout=13.0,
                    limits=limits,
                ),
                session=WebSessionOptions(keepalive_interval=None),
            ),
            transfers=TransferOptions(
                max_concurrent_uploads=2,
                start_timeout=phase,
                finalize_timeout=TimeoutOptions(None, None, None, None),
                drive_timeout=phase,
            ),
            features=FeatureOptions(chat_timeout=7.0, import_research_timeout=8.0),
        ),
    )
    lifecycle = client._web_runtime.web_transport
    uploader = client._web_runtime.source_uploader
    assert (lifecycle._read_timeout, lifecycle._write_timeout, lifecycle._pool_timeout) == (
        11.0,
        12.0,
        13.0,
    )
    assert lifecycle._limits is limits
    assert uploader._start_timeout == httpx.Timeout(connect=1, read=2, write=3, pool=4)
    assert uploader._finalize_timeout == httpx.Timeout(None)
    assert uploader._drive_timeout == httpx.Timeout(connect=1, read=2, write=3, pool=4)
    assert client.sources._upload_timeout is None
    assert client.chat._chat_timeout == 7.0
    assert client.research._import_research_timeout == 8.0


def test_android_transfer_aggregates_and_phase_ownership(auth: AuthTokens) -> None:
    start = TimeoutOptions(100.0, 120.0, 130.0, 140.0)
    finalize = TimeoutOptions(5.0, None, 7.0, None)
    drive = TimeoutOptions(100.0, 110.0, None, None)
    client = NotebookLMClient(
        auth,
        config=ClientConfig(
            backend=AndroidBackendConfig(rpc_timeout=None),
            runtime=RuntimeOptions(max_concurrent_rpcs=None),
            retry=RetryOptions(),
            transfers=TransferOptions(
                max_concurrent_uploads=3,
                start_timeout=start,
                finalize_timeout=finalize,
                drive_timeout=drive,
            ),
        ),
    )
    pipeline = client._android_runtime.upload_pipeline
    assert pipeline._upload_timeout == 502.0
    assert pipeline._drive_timeout == 420.0
    assert pipeline._start_http_timeout == httpx.Timeout(connect=100, read=120, write=130, pool=140)
    assert pipeline._finalize_http_timeout == httpx.Timeout(
        connect=5, read=None, write=7, pool=None
    )
    assert pipeline._drive_http_timeout == httpx.Timeout(
        connect=100, read=110, write=None, pool=None
    )
    assert client._android_runtime.session._timeout is None


def test_legacy_upload_timeout_maps_losslessly_by_backend(auth: AuthTokens) -> None:
    legacy = httpx.Timeout(connect=1, read=None, write=3, pool=4)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        web = NotebookLMClient(auth, upload_timeout=legacy)
        android = NotebookLMClient(auth, upload_timeout=legacy, backend="android")
    assert web.sources._upload_timeout is legacy
    assert web._web_runtime.source_uploader._start_timeout == legacy
    assert web._web_runtime.source_uploader._finalize_timeout == legacy
    assert web._web_runtime.source_uploader._start_timeout is not legacy
    assert web._web_runtime.source_uploader._finalize_timeout is not legacy
    assert web._web_runtime.source_uploader._drive_timeout is None
    pipeline = android._android_runtime.upload_pipeline
    assert pipeline._start_http_timeout == legacy
    assert pipeline._finalize_http_timeout == legacy
    assert pipeline._drive_http_timeout == legacy
    assert pipeline._upload_timeout == pipeline._drive_timeout == 300.0


def test_all_none_transfer_components_disable_http_timers_but_keep_android_fence(
    auth: AuthTokens,
) -> None:
    unbounded = TimeoutOptions(None, None, None, None)
    client = NotebookLMClient(
        auth,
        config=ClientConfig(
            backend=AndroidBackendConfig(),
            transfers=TransferOptions(
                start_timeout=unbounded,
                finalize_timeout=unbounded,
                drive_timeout=unbounded,
            ),
        ),
    )
    pipeline = client._android_runtime.upload_pipeline
    assert pipeline._upload_timeout == pipeline._drive_timeout == 300.0
    assert pipeline._start_http_timeout == httpx.Timeout(None)
    assert pipeline._finalize_http_timeout == httpx.Timeout(None)
    assert pipeline._drive_http_timeout == httpx.Timeout(None)


def test_legacy_warning_is_one_caller_attributed_sorted_message(auth: AuthTokens) -> None:
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        line = inspect.currentframe().f_lineno + 1  # type: ignore[union-attr]
        NotebookLMClient(auth, timeout=31.0, max_concurrent_rpcs=None)
    assert len(caught) == 1
    assert caught[0].filename == __file__
    assert caught[0].lineno == line
    assert "max_concurrent_rpcs, timeout" in str(caught[0].message)


def test_from_storage_rejects_cookie_hooks_without_loading_auth() -> None:
    hooks = WebSessionHooks(cookie_saver=lambda *args, **kwargs: True)
    with pytest.raises(ValueError, match="direct NotebookLMClient constructor"):
        NotebookLMClient.from_storage(config=ClientConfig(backend=WebBackendConfig(hooks=hooks)))


@pytest.mark.asyncio
async def test_typed_from_storage_freezes_env_and_preserves_real_class_construction(
    auth: AuthTokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[object, object]] = []

    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(auth)

    class TrackingMeta(type):
        def __call__(cls, *args: object, **kwargs: object) -> object:
            calls.append((args[0], kwargs["config"]))
            return super().__call__(*args, **kwargs)

    class DerivedClient(NotebookLMClient, metaclass=TrackingMeta):
        pass

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")
    wrapper = DerivedClient.from_storage(config=ClientConfig())
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")

    built = await wrapper._build()

    assert type(built) is DerivedClient
    assert built.auth is auth
    assert built._backend_preference.preferred == "android"
    assert built._backend_preference.reason == "env"
    assert calls[0][0] is auth
    assert isinstance(calls[0][1], ClientConfig)
    assert isinstance(calls[0][1].backend, AndroidBackendConfig)


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", ["context", "await"])
async def test_legacy_from_storage_warns_once_at_each_public_surface_and_freezes_preference(
    usage: str,
    auth: AuthTokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    load_calls = 0
    opened: list[NotebookLMClient] = []

    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        nonlocal load_calls
        load_calls += 1
        return InlineLoadedAuth(auth)

    async def fake_open(client: NotebookLMClient) -> NotebookLMClient:
        opened.append(client)
        return client

    async def fake_close(
        _client: NotebookLMClient,
        _exc_type: object,
        _exc: object,
        _traceback: object,
    ) -> None:
        return None

    class ObservingClient(NotebookLMClient):
        def __init__(self, auth: AuthTokens, *args: object, **kwargs: object) -> None:
            super().__init__(auth, *args, **kwargs)  # type: ignore[arg-type]
            self.preference_after_super = self._backend_preference

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    monkeypatch.setattr(NotebookLMClient, "__aenter__", fake_open)
    monkeypatch.setattr(NotebookLMClient, "__aexit__", fake_close)
    monkeypatch.setenv("NOTEBOOKLM_BACKEND", "android")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        from_storage_line = inspect.currentframe().f_lineno + 1  # type: ignore[union-attr]
        wrapper = ObservingClient.from_storage(timeout=60.0)
        monkeypatch.setenv("NOTEBOOKLM_BACKEND", "web")
        await_line: int | None = None
        if usage == "context":
            async with wrapper as built:
                assert built in opened
        else:
            await_line = inspect.currentframe().f_lineno + 1  # type: ignore[union-attr]
            built = await wrapper

    assert load_calls == 1
    assert built._backend_preference == BackendPreference("android", "env")
    assert built.preference_after_super == BackendPreference("android", "env")
    assert caught[0].filename == __file__
    assert caught[0].lineno == from_storage_line
    assert "legacy NotebookLMClient.from_storage tuning arguments" in str(caught[0].message)
    if usage == "context":
        assert len(caught) == 1
    else:
        assert len(caught) == 2
        assert caught[1].filename == __file__
        assert caught[1].lineno == await_line
        assert "Awaiting NotebookLMClient.from_storage" in str(caught[1].message)


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["direct", "from_storage"])
@pytest.mark.parametrize(
    ("kwargs", "expected"),
    [
        pytest.param(
            {
                "limits": ConnectionLimits(max_connections=1),
                "max_concurrent_rpcs": 2,
                "chat_response_max_bytes": 0,
            },
            "max_concurrent_rpcs must be <= limits.max_connections",
            id="pool-before-response-cap",
        ),
        pytest.param(
            {"chat_response_max_bytes": 0, "chat_timeout": 0},
            "chat_response_max_bytes must be >= 1",
            id="response-cap-before-chat",
        ),
        pytest.param(
            {"chat_timeout": 0, "import_research_timeout": 0},
            "chat_timeout must be a positive, finite number",
            id="chat-before-research",
        ),
        pytest.param(
            {"import_research_timeout": 0, "max_concurrent_rpcs": 0},
            "import_research_timeout must be a positive, finite number",
            id="research-before-shared",
        ),
        pytest.param(
            {"max_concurrent_rpcs": 0, "rate_limit_max_retries": -1},
            "max_concurrent_rpcs must be >= 1",
            id="shared-before-backend",
        ),
        pytest.param(
            {"rate_limit_max_retries": -1, "server_error_max_retries": -1},
            "rate_limit_max_retries must be >= 0",
            id="rate-before-server",
        ),
        pytest.param(
            {"server_error_max_retries": -1, "max_concurrent_uploads": 0},
            "server_error_max_retries must be >= 0",
            id="server-before-upload",
        ),
    ],
)
async def test_legacy_pairwise_invalid_validation_precedence_is_stable(
    entrypoint: str,
    kwargs: dict[str, object],
    expected: str,
    auth: AuthTokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(auth)

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        if entrypoint == "direct":
            with pytest.raises(ValueError, match=expected):
                NotebookLMClient(auth, **kwargs)  # type: ignore[arg-type]
        else:
            wrapper = NotebookLMClient.from_storage(**kwargs)  # type: ignore[arg-type]
            with pytest.raises(ValueError, match=expected):
                await wrapper._build()


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["direct", "from_storage"])
async def test_android_legacy_construction_ignores_invalid_web_only_knobs(
    entrypoint: str,
    auth: AuthTokens,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def load_stored_auth(**_kwargs: object) -> InlineLoadedAuth:
        return InlineLoadedAuth(auth)

    monkeypatch.setattr(client_module._auth_tokens, "_load_stored_auth", load_stored_auth)
    kwargs: dict[str, object] = {
        "backend": "android",
        "keepalive": float("nan"),
        "keepalive_min_interval": -1.0,
        "limits": object(),
    }
    if entrypoint == "direct":
        kwargs.update(cookie_saver=object(), cookie_rotator=object())

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        if entrypoint == "direct":
            built = NotebookLMClient(auth, **kwargs)  # type: ignore[arg-type]
        else:
            built = await NotebookLMClient.from_storage(**kwargs)._build()  # type: ignore[arg-type]

    assert built._backend_preference == BackendPreference("android", "explicit")
    assert set(built.backends.values()) == {"android"}
