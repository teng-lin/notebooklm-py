"""Integration tests for client initialization and core functionality."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notebooklm import AuthTokens, NotebookLMClient
from notebooklm._runtime.helpers import is_auth_error
from notebooklm.rpc import (
    AuthError,
    ClientError,
    NetworkError,
    RateLimitError,
    RPCError,
    RPCMethod,
    RPCTimeoutError,
    ServerError,
)
from tests._helpers.client_factory import build_client_shell_for_tests
from tests.integration.conftest import install_post_as_stream

# httpx-mock + MagicMock based core-layer tests; no real HTTP, no
# cassette. Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


def _install_error_post(core: NotebookLMClient, error: Exception) -> AsyncMock:
    mock_post = AsyncMock(side_effect=error)
    install_post_as_stream(None, core._collaborators.kernel.get_http_client(), mock_post)
    return mock_post


class TestClientInitialization:
    @pytest.mark.asyncio
    async def test_client_initialization(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            assert client._auth == auth_tokens
            assert client._collaborators.kernel.http_client is not None

    @pytest.mark.asyncio
    async def test_client_context_manager_closes(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            assert client._collaborators.kernel.http_client is not None  # client is open
        assert client._collaborators.kernel.http_client is None  # closed after exit

    @pytest.mark.asyncio
    async def test_close_does_not_sync_in_memory_auth_to_default_storage(self):
        auth = AuthTokens(cookies={"SID": "scratch"}, csrf_token="csrf", session_id="session")
        # Inject the cookie-saver seam directly (Phase 2 PR 4 — replaces the
        # legacy ``_core.save_cookies_to_storage`` string-target monkeypatch
        # with a constructor-injection seam wired through
        # ``ClientLifecycle._cookie_saver``).
        mock_save = MagicMock(return_value=False)
        core = build_client_shell_for_tests(auth, cookie_saver=mock_save)
        await core.__aenter__()

        await core.close()

        mock_save.assert_not_called()
        assert core._collaborators.kernel.http_client is None

    @pytest.mark.asyncio
    async def test_close_closes_http_client_when_cookie_sync_fails(self, auth_tokens, tmp_path):
        auth_tokens.storage_path = tmp_path / "storage_state.json"
        # Inject a cookie-saver that raises so the test exercises the
        # close()-handles-saver-failure path without monkeypatching the
        # legacy ``_core.save_cookies_to_storage`` seam.
        boom_save = MagicMock(side_effect=RuntimeError("boom"))
        core = build_client_shell_for_tests(auth_tokens, cookie_saver=boom_save)
        await core.__aenter__()

        await core.close()

        # Assert the injected saver was actually invoked — otherwise the
        # test could pass via an early exit that never reaches the saver,
        # silently weakening the regression guard.
        boom_save.assert_called_once()
        assert core._collaborators.kernel.http_client is None

    @pytest.mark.asyncio
    async def test_client_raises_if_not_initialized(self, auth_tokens):
        client = NotebookLMClient(auth_tokens)
        with pytest.raises(RuntimeError, match="not initialized"):
            await client.notebooks.list()


class TestIsAuthError:
    """Tests for the is_auth_error() helper function."""

    def test_returns_true_for_auth_error(self):
        assert is_auth_error(AuthError("invalid credentials")) is True

    def test_returns_false_for_network_error(self):
        assert is_auth_error(NetworkError("network down")) is False

    def test_returns_false_for_rate_limit_error(self):
        assert is_auth_error(RateLimitError("rate limited")) is False

    def test_returns_false_for_server_error(self):
        assert is_auth_error(ServerError("500 error")) is False

    def test_returns_false_for_client_error(self):
        # ClientError subclass is explicitly excluded (already mapped, no retry).
        # Raw httpx 400 is treated as an auth error; see
        # test_returns_true_for_400_http_status_error.
        assert is_auth_error(ClientError("400 bad request")) is False

    def test_returns_true_for_400_http_status_error(self):
        # NotebookLM returns 400 (not 401/403) when the CSRF token in the at=
        # body param is stale. is_auth_error must include 400 so the layer-1
        # refresh_auth retry path fires for stale CSRF.
        mock_response = MagicMock()
        mock_response.status_code = 400
        error = httpx.HTTPStatusError("400", request=MagicMock(), response=mock_response)
        assert is_auth_error(error) is True

    def test_returns_false_for_rpc_timeout_error(self):
        assert is_auth_error(RPCTimeoutError("timed out")) is False

    def test_returns_true_for_401_http_status_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 401
        error = httpx.HTTPStatusError("401", request=MagicMock(), response=mock_response)
        assert is_auth_error(error) is True

    def test_returns_true_for_403_http_status_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 403
        error = httpx.HTTPStatusError("403", request=MagicMock(), response=mock_response)
        assert is_auth_error(error) is True

    def test_returns_false_for_500_http_status_error(self):
        mock_response = MagicMock()
        mock_response.status_code = 500
        error = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)
        assert is_auth_error(error) is False

    @pytest.mark.parametrize("rpc_code", [401, 403, 16, "UNAUTHENTICATED"])
    def test_returns_true_for_rpc_error_with_auth_code(self, rpc_code):
        assert is_auth_error(RPCError("authentication expired", rpc_code=rpc_code)) is True

    @pytest.mark.parametrize(
        "message",
        [
            "Unauthorized access to this notebook",
            "Session expired while rendering an unrelated artifact",
            "Authentication summary could not be generated",
        ],
    )
    def test_returns_false_for_rpc_error_with_auth_words_but_no_auth_signal(self, message):
        assert is_auth_error(RPCError(message)) is False

    def test_returns_false_for_rpc_error_with_generic_message(self):
        assert is_auth_error(RPCError("some generic error")) is False

    def test_returns_false_for_plain_exception(self):
        assert is_auth_error(ValueError("not an rpc error")) is False


class TestRPCCallHTTPErrors:
    """Tests for HTTP error handling in rpc_call()."""

    @pytest.mark.asyncio
    async def test_rate_limit_429_with_retry_after_header(self, auth_tokens):
        # Pin ``rate_limit_max_retries=0`` to exercise the raise-immediately
        # path. The rate-limit fix raised the default to 3 — the post-retries raise is
        # covered by ``tests/integration/concurrency/test_rate_limit_default.py``;
        # this test documents the explicit-disable contract.
        async with NotebookLMClient(auth_tokens, rate_limit_max_retries=0) as client:
            core = client
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.headers = {"retry-after": "60"}
            mock_response.reason_phrase = "Too Many Requests"
            error = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_response)

            _install_error_post(core, error)
            with pytest.raises(RateLimitError) as exc_info:
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
            assert exc_info.value.retry_after == 60

    @pytest.mark.asyncio
    async def test_rate_limit_429_without_retry_after_header(self, auth_tokens):
        # See ``test_rate_limit_429_with_retry_after_header`` for why this
        # pins ``rate_limit_max_retries=0``.
        async with NotebookLMClient(auth_tokens, rate_limit_max_retries=0) as client:
            core = client
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.headers = {}
            mock_response.reason_phrase = "Too Many Requests"
            error = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_response)

            _install_error_post(core, error)
            with pytest.raises(RateLimitError) as exc_info:
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
            assert exc_info.value.retry_after is None

    @pytest.mark.asyncio
    async def test_rate_limit_429_with_invalid_retry_after_header(self, auth_tokens):
        # See ``test_rate_limit_429_with_retry_after_header`` for why this
        # pins ``rate_limit_max_retries=0``.
        async with NotebookLMClient(auth_tokens, rate_limit_max_retries=0) as client:
            core = client
            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.headers = {"retry-after": "not-a-number"}
            mock_response.reason_phrase = "Too Many Requests"
            error = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_response)

            _install_error_post(core, error)
            with pytest.raises(RateLimitError) as exc_info:
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
            assert exc_info.value.retry_after is None

    @pytest.mark.asyncio
    async def test_client_error_400(self, auth_tokens):
        # With the stale-CSRF fix, HTTP 400 is treated as an auth error and
        # routed through _try_refresh_and_retry first. To exercise the raw
        # 400 → ClientError mapping (back-compat for callers that don't opt
        # in to auto-refresh), clear the refresh callback so is_auth_error's
        # gate in rpc_call short-circuits and the status mapping runs.
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            core._collaborators.auth_coord._refresh_callback = None

            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.reason_phrase = "Bad Request"
            error = httpx.HTTPStatusError("400", request=MagicMock(), response=mock_response)

            _install_error_post(core, error)
            with pytest.raises(ClientError):
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_server_error_500(self, auth_tokens):
        # Pin ``server_error_max_retries=0`` to exercise the raise-immediately
        # mapping path. Retry/backoff behavior is covered in core transport tests.
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client
            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.reason_phrase = "Internal Server Error"
            error = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)

            _install_error_post(core, error)
            with pytest.raises(ServerError):
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_connect_timeout_raises_network_error(self, auth_tokens):
        # Network errors flow through the same retry loop as 5xx responses;
        # pin to 0 so these mapping tests don't pay backoff sleeps.
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client
            _install_error_post(core, httpx.ConnectTimeout("connect timeout"))
            with pytest.raises(NetworkError):
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_read_timeout_raises_rpc_timeout_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client
            _install_error_post(core, httpx.ReadTimeout("read timeout"))
            with pytest.raises(RPCTimeoutError):
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_connect_error_raises_network_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client
            _install_error_post(core, httpx.ConnectError("connection refused"))
            with pytest.raises(NetworkError):
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_generic_request_error_raises_network_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client
            _install_error_post(core, httpx.RequestError("something went wrong"))
            with pytest.raises(NetworkError):
                await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])


class TestRPCCallAuthRetry:
    """Tests for auth retry path after decode_response raises RPCError."""

    @pytest.mark.asyncio
    async def test_auth_retry_on_decode_rpc_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            refresh_callback = AsyncMock()
            core._collaborators.auth_coord._refresh_callback = refresh_callback
            import asyncio

            # Pre-allocate the lock so the first refresh attempt doesn't
            # try to construct one (the coordinator's lazy-init runs at
            # the first ``await_refresh`` call site).
            core._collaborators.auth_coord._refresh_lock = asyncio.Lock()

            success_response = MagicMock()
            success_response.status_code = 200
            success_response.text = "some_valid_response"

            mock_post = AsyncMock(return_value=success_response)
            install_post_as_stream(None, core._collaborators.kernel.get_http_client(), mock_post)

            # Override the runtime decode-response seam before the RPC fires.
            decode_responses = iter(
                [
                    RPCError("authentication expired", rpc_code=401),
                    ["result_data"],
                ]
            )

            def fake_decode(*_a, **_kw):
                value = next(decode_responses)
                if isinstance(value, BaseException):
                    raise value
                return value

            core._seams.decode_response = fake_decode

            result = await core._rpc_executor.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

            assert result == ["result_data"]
            refresh_callback.assert_called_once()


class TestGetHttpClient:
    """Tests for get_http_client() RuntimeError when not initialized.

    Wave 11b of session-decoupling: the ``NotebookLMClient.get_http_client`` forward
    was deleted; the canonical home is ``Kernel.get_http_client`` (reached
    via ``core._collaborators.kernel`` / ``client._collaborators.kernel``).
    """

    def test_get_http_client_raises_when_not_initialized(self, auth_tokens):
        core = build_client_shell_for_tests(auth_tokens)
        with pytest.raises(RuntimeError, match="not initialized"):
            core._collaborators.kernel.get_http_client()

    @pytest.mark.asyncio
    async def test_get_http_client_returns_client_when_initialized(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            http_client = client._collaborators.kernel.get_http_client()
            assert isinstance(http_client, httpx.AsyncClient)


class TestGetSourceIds:
    """Tests for NotebooksAPI.get_source_ids() extracting source IDs from notebook data."""

    @pytest.mark.asyncio
    async def test_returns_source_ids_from_nested_data(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            notebooks = client.notebooks

            mock_notebook_data = [
                [
                    "notebook_title",
                    [
                        [["src_id_1", "extra"]],
                        [["src_id_2", "extra"]],
                    ],
                ]
            ]

            with patch.object(
                core._rpc_executor,
                "rpc_call",
                new_callable=AsyncMock,
                return_value=mock_notebook_data,
            ):
                ids = await notebooks.get_source_ids("nb_123")

            assert ids == ["src_id_1", "src_id_2"]

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_data_is_none(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            notebooks = client.notebooks

            with patch.object(
                core._rpc_executor, "rpc_call", new_callable=AsyncMock, return_value=None
            ):
                ids = await notebooks.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_data_is_empty_list(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            notebooks = client.notebooks

            with patch.object(
                core._rpc_executor, "rpc_call", new_callable=AsyncMock, return_value=[]
            ):
                ids = await notebooks.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_sources_list_is_empty(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            notebooks = client.notebooks

            # Notebook with no sources
            mock_notebook_data = [["notebook_title", []]]

            with patch.object(
                core._rpc_executor,
                "rpc_call",
                new_callable=AsyncMock,
                return_value=mock_notebook_data,
            ):
                ids = await notebooks.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_data_is_not_list(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            notebooks = client.notebooks

            with patch.object(
                core._rpc_executor,
                "rpc_call",
                new_callable=AsyncMock,
                return_value="unexpected_string",
            ):
                ids = await notebooks.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_notebook_info_missing_sources(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            notebooks = client.notebooks

            # notebook_data[0] exists but notebook_info[1] is missing
            mock_notebook_data = [["notebook_title_only"]]

            with patch.object(
                core._rpc_executor,
                "rpc_call",
                new_callable=AsyncMock,
                return_value=mock_notebook_data,
            ):
                ids = await notebooks.get_source_ids("nb_123")

            assert ids == []


class TestCrossDomainCookiePreservation:
    """Tests for cookie preservation during cross-domain redirects."""

    @pytest.mark.asyncio
    async def test_cookies_preserved_on_cross_domain_redirect(self, auth_tokens):
        """Verify cookies persist when redirecting from notebooklm to accounts.google.com."""
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            http_client = core._collaborators.kernel.get_http_client()

            # Set initial sentinel cookie in the jar
            http_client.cookies.set("REDIRECT_SENTINEL", "survives_refresh", domain=".google.com")

            # Simulate what happens during a redirect: update_auth_headers merges new cookies
            # without wiping existing ones (like refreshed SID from accounts.google.com).
            # Wave 3 of plan ``host-protocol-removal`` deleted the
            # NotebookLMClient-level ``update_auth_headers`` forward; call the
            # canonical coordinator method directly with explicit kwargs.
            core._collaborators.auth_coord.update_auth_headers(
                auth=core._auth, kernel=core._collaborators.kernel
            )

            # Verify original cookies are still present (not wiped)
            # httpx.Cookies.get() returns None if cookie not found
            assert (
                http_client.cookies.get("REDIRECT_SENTINEL", domain=".google.com")
                == "survives_refresh"
            )

    @pytest.mark.asyncio
    async def test_update_auth_headers_merges_not_replaces(self, auth_tokens):
        """Verify update_auth_headers merges new cookies, preserving live redirect cookies."""
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            http_client = core._collaborators.kernel.get_http_client()

            # Simulate a live cookie received from accounts.google.com redirect
            http_client.cookies.set(
                "__Secure-1PSIDRTS", "redirect_refreshed_value", domain=".google.com"
            )

            # Now update auth headers (simulating a token refresh).
            # Wave 3 of plan ``host-protocol-removal`` deleted the
            # NotebookLMClient-level ``update_auth_headers`` forward; call the
            # canonical coordinator method directly with explicit kwargs.
            core._collaborators.auth_coord.update_auth_headers(
                auth=core._auth, kernel=core._collaborators.kernel
            )

            # The EXACT value should still be there (merged, not replaced)
            assert (
                http_client.cookies.get("__Secure-1PSIDRTS", domain=".google.com")
                == "redirect_refreshed_value"
            )

    @pytest.mark.asyncio
    async def test_googleusercontent_cookies_not_reassigned(self, auth_tokens):
        """Cookies for .googleusercontent.com must not be forced to .google.com."""
        # Set a cookie with googleusercontent domain via the cookie_jar
        auth_tokens.cookie_jar = httpx.Cookies()
        auth_tokens.cookie_jar.set("download_token", "abc123", domain=".googleusercontent.com")
        auth_tokens.cookie_jar.set("SID", "test_sid", domain=".google.com")

        async with NotebookLMClient(auth_tokens) as client:
            core = client
            http = core._collaborators.kernel.get_http_client()

            # The .googleusercontent.com cookie must remain on its original domain
            assert http.cookies.get("download_token", domain=".googleusercontent.com") == "abc123"
            # It must NOT appear on .google.com
            assert http.cookies.get("download_token", domain=".google.com") is None

    @pytest.mark.asyncio
    async def test_update_auth_headers_preserves_redirect_cookies(self, auth_tokens):
        """update_auth_headers must merge, not replace, preserving redirect cookies."""
        async with NotebookLMClient(auth_tokens) as client:
            core = client
            http = core._collaborators.kernel.get_http_client()

            # Simulate Google setting a cookie during a redirect
            http.cookies.set("__Secure-1PSIDCC", "from_redirect", domain=".google.com")

            # Now update auth headers.
            # Wave 3 of plan ``host-protocol-removal`` deleted the
            # NotebookLMClient-level ``update_auth_headers`` forward; call the
            # canonical coordinator method directly with explicit kwargs.
            core._collaborators.auth_coord.update_auth_headers(
                auth=core._auth, kernel=core._collaborators.kernel
            )

            # The redirect cookie must survive
            assert http.cookies.get("__Secure-1PSIDCC", domain=".google.com") == "from_redirect"


class TestBuildUrlHL:
    """``RpcExecutor.build_url()`` must thread NOTEBOOKLM_HL into the
    batchexecute URL.

    This is the load-bearing site for setting the interface language on
    every RPC call. The NotebookLMClient-level ``_build_url`` thin wrapper was
    inlined in PR #4b — callers reach the canonical method through
    ``core._rpc_executor.build_url(...)``.

    ``RpcExecutor.build_url`` requires an ``AuthSnapshot`` (consumes
    ``session_id`` / ``authuser`` / ``account_email`` from it rather
    than reading ``self.auth`` live). Tests construct a snapshot inline
    from the fixture's ``AuthTokens`` so the URL-construction logic is
    exercised without spinning up the shared authed transport path.
    """

    @staticmethod
    def _snapshot_for(core):
        from notebooklm._request_types import AuthSnapshot

        return AuthSnapshot(
            csrf_token=core._auth.csrf_token,
            session_id=core._auth.session_id,
            authuser=core._auth.authuser,
            account_email=core._auth.account_email,
        )

    def test_build_url_defaults_hl_to_en(self, auth_tokens, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_HL", raising=False)
        core = build_client_shell_for_tests(auth_tokens)
        url = core._rpc_executor.build_url(RPCMethod.LIST_NOTEBOOKS, self._snapshot_for(core))
        assert "hl=en" in url

    def test_build_url_includes_hl_from_env(self, auth_tokens, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        core = build_client_shell_for_tests(auth_tokens)
        url = core._rpc_executor.build_url(RPCMethod.LIST_NOTEBOOKS, self._snapshot_for(core))
        assert "hl=ja" in url

    def test_build_url_empty_env_falls_back_to_en(self, auth_tokens, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_HL", "")
        core = build_client_shell_for_tests(auth_tokens)
        url = core._rpc_executor.build_url(RPCMethod.LIST_NOTEBOOKS, self._snapshot_for(core))
        assert "hl=en" in url
