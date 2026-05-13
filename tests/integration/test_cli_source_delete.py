"""Integration tests for source delete CLI flows."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner
from pytest_httpx import HTTPXMock

from notebooklm.notebooklm_cli import cli
from notebooklm.rpc import RPCMethod


@pytest.fixture
def runner() -> CliRunner:
    """Create a Click test runner."""
    return CliRunner()


@pytest.fixture
def mock_auth():
    """Mock authentication for CLI integration tests."""
    import httpx

    mock_jar = httpx.Cookies()
    mock_jar.set("SID", "test", domain=".google.com")

    with (
        patch(
            "notebooklm.cli.helpers.load_auth_from_storage",
            return_value={
                "SID": "test",
                "HSID": "test",
                "SSID": "test",
                "APISID": "test",
                "SAPISID": "test",
            },
        ),
        patch(
            "notebooklm.auth.fetch_tokens_with_domains",
            new_callable=AsyncMock,
        ) as mock_fetch,
        patch(
            "notebooklm.cli.helpers.build_cookie_jar",
            return_value=mock_jar,
        ),
    ):
        mock_fetch.return_value = ("csrf_token", "session_id")
        yield


@pytest.fixture
def mock_context(tmp_path: Path):
    """Provide a canonical notebook UUID so CLI skips notebook-list resolution."""
    context_file = tmp_path / "context.json"
    context_file.write_text(
        json.dumps({"notebook_id": "06f0c5bd-108f-4c8b-8911-34b2acc656de"}),
        encoding="utf-8",
    )

    with patch("notebooklm.cli.helpers.get_context_path", return_value=context_file):
        yield context_file


def _build_source_list_response(build_rpc_response, source_id: str, title: str) -> str:
    """Build a GET_NOTEBOOK response containing a single source."""
    return build_rpc_response(
        RPCMethod.GET_NOTEBOOK,
        [
            [
                "Test Notebook",
                [
                    [
                        [source_id],
                        title,
                        [None, 11, [1704067200, 0], None, 5, None, None, None],
                        [None, 2],
                    ]
                ],
                "06f0c5bd-108f-4c8b-8911-34b2acc656de",
                "📘",
                None,
                [None, None, None, None, None, [1704067200, 0]],
            ]
        ],
    )


class TestCliSourceDeleteIntegration:
    """Integration coverage for CLI source delete flows."""

    def test_source_delete_by_title(
        self, runner, mock_auth, mock_context, httpx_mock: HTTPXMock, build_rpc_response
    ):
        httpx_mock.add_response(
            content=_build_source_list_response(
                build_rpc_response,
                "ff503bfa-5e39-4281-a1d8-2a66c7b86724",
                "VCR Delete Test Source",
            ).encode()
        )
        httpx_mock.add_response(
            content=build_rpc_response(RPCMethod.DELETE_SOURCE, [True]).encode()
        )

        result = runner.invoke(
            cli,
            ["source", "delete-by-title", "VCR Delete Test Source", "-y"],
        )

        assert result.exit_code == 0
        assert "Deleted source" in result.output

    def test_source_delete_title_suggests_delete_by_title(
        self, runner, mock_auth, mock_context, httpx_mock: HTTPXMock, build_rpc_response
    ):
        httpx_mock.add_response(
            content=_build_source_list_response(
                build_rpc_response,
                "ff503bfa-5e39-4281-a1d8-2a66c7b86724",
                "VCR Delete Test Source",
            ).encode()
        )

        result = runner.invoke(
            cli,
            ["source", "delete", "VCR Delete Test Source", "-y"],
        )

        assert result.exit_code == 1
        assert "delete-by-title" in result.output
