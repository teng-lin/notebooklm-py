"""CLI integration tests for ``notebooklm source discover`` (VCR replay).

``source discover`` is an RPC-backed command: it opens a real
:class:`~notebooklm.client.NotebookLMClient` and issues the synchronous
``DiscoverSources`` (``Es3dTe``) RPC over ``batchexecute``. These tests drive
the full CLI -> Client -> RPC path through Click's ``CliRunner`` while VCR
replays the recorded ``discover_sources.yaml`` cassette -- no live auth.

The matcher keys on ``rpcids`` + decoded body shape (never the notebook id), so
the placeholder id ``mock_context`` injects replays cleanly against a cassette
recorded against a different notebook.
"""

from __future__ import annotations

import json

import pytest

from notebooklm.notebooklm_cli import cli

from .conftest import notebooklm_vcr, skip_no_cassettes

pytestmark = [pytest.mark.vcr, skip_no_cassettes]


class TestSourceDiscoverCommand:
    def test_discover_text(self, runner, mock_auth_for_vcr, mock_context) -> None:
        """Text-mode discover replays the RPC, exits 0, and lists candidates."""
        with notebooklm_vcr.use_cassette("discover_sources.yaml") as cassette:
            result = runner.invoke(cli, ["source", "discover", "quantum computing advances"])

        assert result.exit_code == 0, result.output
        assert "Traceback" not in result.output
        assert "http" in result.output, "expected at least one candidate URL"
        assert cassette.play_count == 1, "expected exactly one DiscoverSources RPC"

    def test_discover_json_envelope(self, runner, mock_auth_for_vcr, mock_context) -> None:
        """``source discover --json`` emits a candidate-source list envelope."""
        with notebooklm_vcr.use_cassette("discover_sources.yaml"):
            result = runner.invoke(
                cli,
                ["source", "discover", "quantum computing advances", "--json"],
            )

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert isinstance(payload, dict)
        sources = payload.get("sources")
        assert isinstance(sources, list) and sources, "expected candidate sources"
        for entry in sources:
            assert isinstance(entry["url"], str) and entry["url"].startswith("http")
            assert isinstance(entry["title"], str)
            assert "why_relevant" in entry
            assert isinstance(entry["source_type"], int)
