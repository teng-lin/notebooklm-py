"""Integration tests for SettingsAPI.

Moved from ``tests/unit/`` to ``tests/integration/``.
Mock-backed (``pytest_httpx``); ``allow_no_vcr`` opts out of the
integration-tree VCR enforcement hook in ``tests/integration/conftest.py``.
Cassette-backed coverage lives in ``tests/integration/test_vcr_comprehensive.py``.
"""

import json
from contextlib import asynccontextmanager
from unittest.mock import MagicMock, patch

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.exceptions import UnknownRPCMethodError
from notebooklm.rpc import RPCMethod

pytestmark = pytest.mark.allow_no_vcr


def _from_storage_cm(client):
    """Async-context-manager stand-in for ``_FromStorageContext`` over ``client``."""

    @asynccontextmanager
    async def _cm():
        async with client as opened:
            yield opened

    return _cm()


class TestSettingsAPI:
    """Tests for the SettingsAPI."""

    @pytest.mark.asyncio
    async def test_set_output_language(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """Test setting output language returns the language code."""
        # Mock response: result[2][4][0] contains the language code
        response_data = [
            None,
            [100, 50, 10],  # Limits
            [True, None, None, True, ["zh_Hans"]],  # Settings with language
        ]
        response = build_rpc_response(RPCMethod.SET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.set_output_language("zh_Hans")

        assert result == "zh_Hans"

    @pytest.mark.asyncio
    async def test_set_output_language_english(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """Test setting English returns the language code."""
        response_data = [
            None,
            [100, 50, 10],
            [True, None, None, True, ["en"]],
        ]
        response = build_rpc_response(RPCMethod.SET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.set_output_language("en")

        assert result == "en"

    @pytest.mark.asyncio
    async def test_get_output_language(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """Test getting output language from user settings."""
        # Response structure for GET_USER_SETTINGS: result[0][2][4][0]
        response_data = [
            [
                None,
                [100, 50, 10],  # Limits
                [True, None, None, True, ["ja"]],  # Settings with language
            ]
        ]
        response = build_rpc_response(RPCMethod.GET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.get_output_language()

        assert result == "ja"

    @pytest.mark.asyncio
    async def test_get_output_language_returns_none_when_not_set(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """Test getting output language returns None when not set on server."""
        # Server returns empty string when language not set
        response_data = [
            [
                None,
                [100, 50, 10],
                [True, None, None, True, [""]],  # Empty string
            ]
        ]
        response = build_rpc_response(RPCMethod.GET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.get_output_language()

        assert result is None

    @pytest.mark.asyncio
    async def test_get_output_language_returns_none_when_language_slot_absent(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """An intact envelope whose flags block omits the trailing language slot.

        The mandatory envelope (``result[0][2]``) is present; only the optional
        language slot ([4]) is absent — the legitimate "user never set a
        language" shape. This must degrade to ``None``, not raise.
        """
        response_data = [
            [
                None,
                [100, 50, 10],
                [True, None, None, True],  # flags block present, language slot omitted
            ]
        ]
        response = build_rpc_response(RPCMethod.GET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.get_output_language()

        assert result is None

    @pytest.mark.asyncio
    async def test_get_output_language_raises_on_envelope_drift(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """Genuine drift in the mandatory settings envelope raises (ADR-0011).

        ``[[None, None]]`` has no flags block at ``result[0][2]`` — the
        structurally-mandatory envelope moved, so this is real schema drift and
        surfaces as ``UnknownRPCMethodError`` rather than a silent ``None``.
        """
        response_data = [[None, None]]  # Missing settings/flags element
        response = build_rpc_response(RPCMethod.GET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(UnknownRPCMethodError):
                await client.settings.get_output_language()

    @pytest.mark.asyncio
    async def test_get_account_limits(self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response):
        """Test getting account limits from user settings."""
        response_data = [
            [
                None,
                [6, 500, 300, 500000, 2],
                [True, None, None, True, ["en"]],
            ]
        ]
        response = build_rpc_response(RPCMethod.GET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.get_account_limits()

        assert result.notebook_limit == 500
        assert result.source_limit == 300
        assert result.raw_limits == (6, 500, 300, 500000, 2)


class TestLoginLanguageSync:
    """Integration test for syncing server language to local config after login."""

    def test_login_syncs_server_language_to_config(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response, tmp_path
    ):
        """Full flow: login -> fetch server language via RPC -> persist to local config."""
        import importlib

        from notebooklm.cli.session_cmd import _sync_server_language_to_config

        config_path = tmp_path / "config.json"
        # Use importlib to bypass Click group shadowing on Python 3.10
        language_mod = importlib.import_module("notebooklm.cli.language_cmd")

        # Mock the RPC response for GET_USER_SETTINGS returning "zh_Hans"
        response_data = [
            [
                None,
                [100, 50, 10],
                [True, None, None, True, ["zh_Hans"]],
            ]
        ]
        response = build_rpc_response(RPCMethod.GET_USER_SETTINGS, response_data)
        httpx_mock.add_response(content=response.encode())

        # `from_storage` is now sync and returns a `_FromStorageContext`
        # (async context manager). Mock as a sync MagicMock returning a
        # stand-in context manager wrapping a real ``NotebookLMClient``.
        real_client = NotebookLMClient(auth_tokens)
        with (
            patch.object(
                NotebookLMClient,
                "from_storage",
                new_callable=MagicMock,
                return_value=_from_storage_cm(real_client),
            ),
            patch.object(language_mod, "get_config_path", return_value=config_path),
            patch.object(language_mod, "get_home_dir"),
        ):
            _sync_server_language_to_config()

        # Verify language was persisted through the full RPC -> config flow
        config = json.loads(config_path.read_text())
        assert config["language"] == "zh_Hans"
