from __future__ import annotations

import pytest

from notebooklm.server.external_kb.providers.openapi import OpenApiConnector
from notebooklm.server.external_kb.providers.dify import DifyConnector


class TestOpenApiConnector:

    @pytest.fixture
    def config(self) -> dict:
        return {
            "api_base_url": "https://fake-api.example.com",
            "auth_type": "api_key",
            "auth_credentials": {"api_key": "test-key-123"},
        }

    def test_initialization(self, config: dict) -> None:
        c = OpenApiConnector(config=config)
        assert c.api_base_url == "https://fake-api.example.com"
        assert c.auth_type == "api_key"

    def test_build_headers_api_key(self, config: dict) -> None:
        c = OpenApiConnector(config=config)
        headers = c._build_headers()
        assert headers["Authorization"] == "Bearer test-key-123"

    def test_build_headers_basic(self) -> None:
        cfg = {
            "api_base_url": "https://example.com",
            "auth_type": "basic",
            "auth_credentials": {"username": "user", "password": "pass"},
        }
        c = OpenApiConnector(config=cfg)
        headers = c._build_headers()
        assert headers["Authorization"].startswith("Basic ")

    def test_build_headers_bearer(self) -> None:
        cfg = {
            "api_base_url": "https://example.com",
            "auth_type": "bearer",
            "auth_credentials": {"token": "my-token"},
        }
        c = OpenApiConnector(config=cfg)
        headers = c._build_headers()
        assert headers["Authorization"] == "Bearer my-token"


class TestDifyConnector:

    @pytest.fixture
    def config(self) -> dict:
        return {
            "api_base_url": "https://dify.example.com/v1",
            "auth_credentials": {"api_key": "dify-key"},
        }

    def test_initialization(self, config: dict) -> None:
        c = DifyConnector(config=config)
        assert c.api_base_url == "https://dify.example.com/v1"
        assert c.api_key == "dify-key"

    def test_headers(self, config: dict) -> None:
        c = DifyConnector(config=config)
        headers = c._headers()
        assert headers["Authorization"] == "Bearer dify-key"
        assert headers["Accept"] == "application/json"
