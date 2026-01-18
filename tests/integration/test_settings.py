"""Integration tests for SettingsAPI."""

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


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
        response = build_rpc_response(RPCMethod.SET_OUTPUT_LANGUAGE, response_data)
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
        response = build_rpc_response(RPCMethod.SET_OUTPUT_LANGUAGE, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.set_output_language("en")

        assert result == "en"

    @pytest.mark.asyncio
    async def test_get_output_language(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """Test getting output language uses empty string."""
        response_data = [
            None,
            [100, 50, 10],
            [True, None, None, True, ["ja"]],
        ]
        response = build_rpc_response(RPCMethod.SET_OUTPUT_LANGUAGE, response_data)
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
            None,
            [100, 50, 10],
            [True, None, None, True, [""]],  # Empty string
        ]
        response = build_rpc_response(RPCMethod.SET_OUTPUT_LANGUAGE, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.get_output_language()

        assert result is None

    @pytest.mark.asyncio
    async def test_get_output_language_returns_none_on_malformed_response(
        self, httpx_mock: HTTPXMock, auth_tokens, build_rpc_response
    ):
        """Test getting output language returns None on unexpected response structure."""
        # Malformed response - missing expected structure
        response_data = [None, None]  # Missing settings element
        response = build_rpc_response(RPCMethod.SET_OUTPUT_LANGUAGE, response_data)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.settings.get_output_language()

        assert result is None
