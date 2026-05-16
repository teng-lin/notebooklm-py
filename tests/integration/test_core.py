"""Integration tests for client initialization and core functionality."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from notebooklm import AuthTokens, NotebookLMClient
from notebooklm._core import MAX_CONVERSATION_CACHE_SIZE, ClientCore, is_auth_error
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

# T8.D11 — httpx-mock + MagicMock based core-layer tests; no real HTTP, no
# cassette. Opt out of the tier-enforcement hook in tests/integration/conftest.py.
pytestmark = pytest.mark.allow_no_vcr


class TestClientInitialization:
    @pytest.mark.asyncio
    async def test_client_initialization(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            assert client._core.auth == auth_tokens
            assert client._core._http_client is not None

    @pytest.mark.asyncio
    async def test_client_context_manager_closes(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            assert client._core._http_client is not None  # client is open
        assert client._core._http_client is None  # closed after exit

    @pytest.mark.asyncio
    async def test_close_does_not_sync_in_memory_auth_to_default_storage(self):
        auth = AuthTokens(cookies={"SID": "scratch"}, csrf_token="csrf", session_id="session")
        core = ClientCore(auth)
        await core.open()

        with patch("notebooklm._core.save_cookies_to_storage") as mock_save:
            await core.close()

        mock_save.assert_not_called()
        assert core._http_client is None

    @pytest.mark.asyncio
    async def test_close_closes_http_client_when_cookie_sync_fails(self, auth_tokens, tmp_path):
        auth_tokens.storage_path = tmp_path / "storage_state.json"
        core = ClientCore(auth_tokens)
        await core.open()

        with patch("notebooklm._core.save_cookies_to_storage", side_effect=RuntimeError("boom")):
            await core.close()

        assert core._http_client is None

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

    def test_returns_true_for_rpc_error_with_auth_message(self):
        assert is_auth_error(RPCError("authentication expired")) is True

    def test_returns_false_for_rpc_error_with_generic_message(self):
        assert is_auth_error(RPCError("some generic error")) is False

    def test_returns_false_for_plain_exception(self):
        assert is_auth_error(ValueError("not an rpc error")) is False


class TestRPCCallHTTPErrors:
    """Tests for HTTP error handling in rpc_call()."""

    @pytest.mark.asyncio
    async def test_rate_limit_429_with_retry_after_header(self, auth_tokens):
        # Pin ``rate_limit_max_retries=0`` to exercise the raise-immediately
        # path. T7.H2 raised the default to 3 — the post-retries raise is
        # covered by ``tests/integration/concurrency/test_rate_limit_default.py``;
        # this test documents the explicit-disable contract.
        async with NotebookLMClient(auth_tokens, rate_limit_max_retries=0) as client:
            core = client._core

            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.headers = {"retry-after": "60"}
            mock_response.reason_phrase = "Too Many Requests"
            error = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_response)

            with (
                patch.object(core._http_client, "post", side_effect=error),
                pytest.raises(RateLimitError) as exc_info,
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
            assert exc_info.value.retry_after == 60

    @pytest.mark.asyncio
    async def test_rate_limit_429_without_retry_after_header(self, auth_tokens):
        # See ``test_rate_limit_429_with_retry_after_header`` for why this
        # pins ``rate_limit_max_retries=0``.
        async with NotebookLMClient(auth_tokens, rate_limit_max_retries=0) as client:
            core = client._core

            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.headers = {}
            mock_response.reason_phrase = "Too Many Requests"
            error = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_response)

            with (
                patch.object(core._http_client, "post", side_effect=error),
                pytest.raises(RateLimitError) as exc_info,
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
            assert exc_info.value.retry_after is None

    @pytest.mark.asyncio
    async def test_rate_limit_429_with_invalid_retry_after_header(self, auth_tokens):
        # See ``test_rate_limit_429_with_retry_after_header`` for why this
        # pins ``rate_limit_max_retries=0``.
        async with NotebookLMClient(auth_tokens, rate_limit_max_retries=0) as client:
            core = client._core

            mock_response = MagicMock()
            mock_response.status_code = 429
            mock_response.headers = {"retry-after": "not-a-number"}
            mock_response.reason_phrase = "Too Many Requests"
            error = httpx.HTTPStatusError("429", request=MagicMock(), response=mock_response)

            with (
                patch.object(core._http_client, "post", side_effect=error),
                pytest.raises(RateLimitError) as exc_info,
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])
            assert exc_info.value.retry_after is None

    @pytest.mark.asyncio
    async def test_client_error_400(self, auth_tokens):
        # With the stale-CSRF fix, HTTP 400 is treated as an auth error and
        # routed through _try_refresh_and_retry first. To exercise the raw
        # 400 → ClientError mapping (back-compat for callers that don't opt
        # in to auto-refresh), clear the refresh callback so is_auth_error's
        # gate in rpc_call short-circuits and the status mapping runs.
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core
            core._refresh_callback = None

            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.reason_phrase = "Bad Request"
            error = httpx.HTTPStatusError("400", request=MagicMock(), response=mock_response)

            with (
                patch.object(core._http_client, "post", side_effect=error),
                pytest.raises(ClientError),
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_server_error_500(self, auth_tokens):
        # Pin ``server_error_max_retries=0`` to exercise the raise-immediately
        # mapping path. Retry/backoff behavior is covered in core transport tests.
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client._core

            mock_response = MagicMock()
            mock_response.status_code = 500
            mock_response.reason_phrase = "Internal Server Error"
            error = httpx.HTTPStatusError("500", request=MagicMock(), response=mock_response)

            with (
                patch.object(core._http_client, "post", side_effect=error),
                pytest.raises(ServerError),
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_connect_timeout_raises_network_error(self, auth_tokens):
        # Network errors flow through the same retry loop as 5xx responses;
        # pin to 0 so these mapping tests don't pay backoff sleeps.
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client._core

            with (
                patch.object(
                    core._http_client,
                    "post",
                    side_effect=httpx.ConnectTimeout("connect timeout"),
                ),
                pytest.raises(NetworkError),
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_read_timeout_raises_rpc_timeout_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client._core

            with (
                patch.object(
                    core._http_client,
                    "post",
                    side_effect=httpx.ReadTimeout("read timeout"),
                ),
                pytest.raises(RPCTimeoutError),
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_connect_error_raises_network_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client._core

            with (
                patch.object(
                    core._http_client,
                    "post",
                    side_effect=httpx.ConnectError("connection refused"),
                ),
                pytest.raises(NetworkError),
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

    @pytest.mark.asyncio
    async def test_generic_request_error_raises_network_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens, server_error_max_retries=0) as client:
            core = client._core

            with (
                patch.object(
                    core._http_client,
                    "post",
                    side_effect=httpx.RequestError("something went wrong"),
                ),
                pytest.raises(NetworkError),
            ):
                await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])


class TestRPCCallAuthRetry:
    """Tests for auth retry path after decode_response raises RPCError."""

    @pytest.mark.asyncio
    async def test_auth_retry_on_decode_rpc_error(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core

            refresh_callback = AsyncMock()
            core._refresh_callback = refresh_callback
            import asyncio

            core._refresh_lock = asyncio.Lock()

            success_response = MagicMock()
            success_response.status_code = 200
            success_response.text = "some_valid_response"

            with (
                patch.object(core._http_client, "post", return_value=success_response),
                patch(
                    "notebooklm._core.decode_response",
                    side_effect=[
                        RPCError("authentication expired"),
                        ["result_data"],
                    ],
                ),
            ):
                result = await core.rpc_call(RPCMethod.LIST_NOTEBOOKS, [])

            assert result == ["result_data"]
            refresh_callback.assert_called_once()


class TestGetHttpClient:
    """Tests for get_http_client() RuntimeError when not initialized."""

    def test_get_http_client_raises_when_not_initialized(self, auth_tokens):
        core = ClientCore(auth_tokens)
        with pytest.raises(RuntimeError, match="not initialized"):
            core.get_http_client()

    @pytest.mark.asyncio
    async def test_get_http_client_returns_client_when_initialized(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            http_client = client._core.get_http_client()
            assert isinstance(http_client, httpx.AsyncClient)


class TestConversationCacheFIFOEviction:
    """Tests for FIFO eviction when conversation cache exceeds MAX_CONVERSATION_CACHE_SIZE."""

    def test_fifo_eviction_when_cache_is_full(self, auth_tokens):
        core = ClientCore(auth_tokens)

        # Fill the cache to capacity
        for i in range(MAX_CONVERSATION_CACHE_SIZE):
            core.cache_conversation_turn(f"conv_{i}", f"q{i}", f"a{i}", i)

        assert len(core._conversation_cache) == MAX_CONVERSATION_CACHE_SIZE

        # Adding one more should evict the oldest (conv_0)
        core.cache_conversation_turn("conv_new", "q_new", "a_new", 0)

        assert len(core._conversation_cache) == MAX_CONVERSATION_CACHE_SIZE
        assert "conv_0" not in core._conversation_cache
        assert "conv_new" in core._conversation_cache

    def test_fifo_eviction_preserves_order(self, auth_tokens):
        core = ClientCore(auth_tokens)

        # Fill cache to capacity
        for i in range(MAX_CONVERSATION_CACHE_SIZE):
            core.cache_conversation_turn(f"conv_{i}", f"q{i}", f"a{i}", i)

        # Add two new conversations - should evict conv_0 then conv_1
        core.cache_conversation_turn("conv_new_1", "q1", "a1", 0)
        core.cache_conversation_turn("conv_new_2", "q2", "a2", 0)

        assert "conv_0" not in core._conversation_cache
        assert "conv_1" not in core._conversation_cache
        assert "conv_new_1" in core._conversation_cache
        assert "conv_new_2" in core._conversation_cache

    def test_adding_turns_to_existing_conversation_does_not_evict(self, auth_tokens):
        core = ClientCore(auth_tokens)

        # Fill cache to capacity
        for i in range(MAX_CONVERSATION_CACHE_SIZE):
            core.cache_conversation_turn(f"conv_{i}", f"q{i}", f"a{i}", i)

        # Adding a second turn to an EXISTING conversation should NOT evict anything
        core.cache_conversation_turn("conv_0", "q_extra", "a_extra", 1)

        assert len(core._conversation_cache) == MAX_CONVERSATION_CACHE_SIZE
        assert len(core._conversation_cache["conv_0"]) == 2


class TestClearConversationCacheNotFound:
    """Tests for clear_conversation_cache() returning False when ID not found."""

    def test_clear_nonexistent_conversation_returns_false(self, auth_tokens):
        core = ClientCore(auth_tokens)
        result = core.clear_conversation_cache("nonexistent_id")
        assert result is False

    def test_clear_existing_conversation_returns_true(self, auth_tokens):
        core = ClientCore(auth_tokens)
        core.cache_conversation_turn("conv_abc", "question", "answer", 1)
        result = core.clear_conversation_cache("conv_abc")
        assert result is True
        assert "conv_abc" not in core._conversation_cache

    def test_clear_all_conversations_returns_true(self, auth_tokens):
        core = ClientCore(auth_tokens)
        core.cache_conversation_turn("conv_1", "q1", "a1", 1)
        core.cache_conversation_turn("conv_2", "q2", "a2", 1)
        result = core.clear_conversation_cache()
        assert result is True
        assert len(core._conversation_cache) == 0


class TestGetSourceIds:
    """Tests for get_source_ids() extracting source IDs from notebook data."""

    @pytest.mark.asyncio
    async def test_returns_source_ids_from_nested_data(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core

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
                core, "rpc_call", new_callable=AsyncMock, return_value=mock_notebook_data
            ):
                ids = await core.get_source_ids("nb_123")

            assert ids == ["src_id_1", "src_id_2"]

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_data_is_none(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core

            with patch.object(core, "rpc_call", new_callable=AsyncMock, return_value=None):
                ids = await core.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_data_is_empty_list(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core

            with patch.object(core, "rpc_call", new_callable=AsyncMock, return_value=[]):
                ids = await core.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_sources_list_is_empty(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core

            # Notebook with no sources
            mock_notebook_data = [["notebook_title", []]]

            with patch.object(
                core, "rpc_call", new_callable=AsyncMock, return_value=mock_notebook_data
            ):
                ids = await core.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_data_is_not_list(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core

            with patch.object(
                core, "rpc_call", new_callable=AsyncMock, return_value="unexpected_string"
            ):
                ids = await core.get_source_ids("nb_123")

            assert ids == []

    @pytest.mark.asyncio
    async def test_returns_empty_list_when_notebook_info_missing_sources(self, auth_tokens):
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core

            # notebook_data[0] exists but notebook_info[1] is missing
            mock_notebook_data = [["notebook_title_only"]]

            with patch.object(
                core, "rpc_call", new_callable=AsyncMock, return_value=mock_notebook_data
            ):
                ids = await core.get_source_ids("nb_123")

            assert ids == []


class TestCrossDomainCookiePreservation:
    """Tests for cookie preservation during cross-domain redirects."""

    @pytest.mark.asyncio
    async def test_cookies_preserved_on_cross_domain_redirect(self, auth_tokens):
        """Verify cookies persist when redirecting from notebooklm to accounts.google.com."""
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core
            http_client = core._http_client

            # Set initial sentinel cookie in the jar
            http_client.cookies.set("REDIRECT_SENTINEL", "survives_refresh", domain=".google.com")

            # Simulate what happens during a redirect: update_auth_headers merges new cookies
            # without wiping existing ones (like refreshed SID from accounts.google.com)
            core.update_auth_headers()

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
            core = client._core
            http_client = core._http_client

            # Simulate a live cookie received from accounts.google.com redirect
            http_client.cookies.set(
                "__Secure-1PSIDRTS", "redirect_refreshed_value", domain=".google.com"
            )

            # Now update auth headers (simulating a token refresh)
            core.update_auth_headers()

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
            core = client._core
            http = core._http_client

            # The .googleusercontent.com cookie must remain on its original domain
            assert http.cookies.get("download_token", domain=".googleusercontent.com") == "abc123"
            # It must NOT appear on .google.com
            assert http.cookies.get("download_token", domain=".google.com") is None

    @pytest.mark.asyncio
    async def test_update_auth_headers_preserves_redirect_cookies(self, auth_tokens):
        """update_auth_headers must merge, not replace, preserving redirect cookies."""
        async with NotebookLMClient(auth_tokens) as client:
            core = client._core
            http = core._http_client

            # Simulate Google setting a cookie during a redirect
            http.cookies.set("__Secure-1PSIDCC", "from_redirect", domain=".google.com")

            # Now update auth headers
            core.update_auth_headers()

            # The redirect cookie must survive
            assert http.cookies.get("__Secure-1PSIDCC", domain=".google.com") == "from_redirect"


class TestBuildUrlHL:
    """_build_url() must thread NOTEBOOKLM_HL into the batchexecute URL.

    This is the load-bearing site for setting the interface language on
    every RPC call.

    T7.F2: ``_build_url`` now requires an ``_AuthSnapshot`` (consumes
    ``session_id`` / ``authuser`` / ``account_email`` from it rather
    than reading ``self.auth`` live). Tests construct a snapshot inline
    from the fixture's ``AuthTokens`` so the URL-construction logic is
    exercised without spinning up ``_perform_authed_post``.
    """

    @staticmethod
    def _snapshot_for(core):
        from notebooklm._core import _AuthSnapshot

        return _AuthSnapshot(
            csrf_token=core.auth.csrf_token,
            session_id=core.auth.session_id,
            authuser=core.auth.authuser,
            account_email=core.auth.account_email,
        )

    def test_build_url_defaults_hl_to_en(self, auth_tokens, monkeypatch):
        monkeypatch.delenv("NOTEBOOKLM_HL", raising=False)
        core = ClientCore(auth_tokens)
        url = core._build_url(RPCMethod.LIST_NOTEBOOKS, self._snapshot_for(core))
        assert "hl=en" in url

    def test_build_url_includes_hl_from_env(self, auth_tokens, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_HL", "ja")
        core = ClientCore(auth_tokens)
        url = core._build_url(RPCMethod.LIST_NOTEBOOKS, self._snapshot_for(core))
        assert "hl=ja" in url

    def test_build_url_empty_env_falls_back_to_en(self, auth_tokens, monkeypatch):
        monkeypatch.setenv("NOTEBOOKLM_HL", "")
        core = ClientCore(auth_tokens)
        url = core._build_url(RPCMethod.LIST_NOTEBOOKS, self._snapshot_for(core))
        assert "hl=en" in url
