"""Characterization tests for the notebook + partial-ID resolver.

These tests pin observable CLI behavior across the chat and download resolver
paths before the P2.T1 consolidation refactor. They are intentionally
end-to-end at the ``CliRunner`` level so
they capture exit codes, stdout/stderr structure, and side-effect counts
(e.g. "did the notebook listing fire?") that the lower-level unit tests in
``test_resolve.py`` and ``test_download_helpers.py`` do not cover holistically.

They must pass identically before AND after the refactor. Any divergence is
a behavior regression - not an opportunity to "fix" the old behavior.

Coverage matrix (resolver fallback paths):

| Path                                | chat ask  | download <type> |
|-------------------------------------|-----------|-----------------|
| Explicit `-n <full-id>` arg         | yes       | yes             |
| `NOTEBOOKLM_NOTEBOOK` env-var       | yes       | yes             |
| Context-file fallback               | yes       | yes             |
| Partial artifact id (download only) | n/a       | yes             |

The chat path also pins the conversation-id fallback order, co-located here
because it shares the same command workflow and can regress during resolver
refactors.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest
from click.testing import CliRunner

import notebooklm.auth as auth_module
import notebooklm.cli.helpers as helpers_module
from notebooklm.cli import resolve as resolve_helpers
from notebooklm.notebooklm_cli import cli
from notebooklm.types import AskResult

from .conftest import (
    create_mock_client,
    inject_client,
)

# ----------------------------------------------------------------------------
# Fixtures
# ----------------------------------------------------------------------------


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


@pytest.fixture
def mock_auth():
    with patch.object(helpers_module, "load_auth_from_storage") as m:
        m.return_value = {
            "SID": "test",
            "__Secure-1PSIDTS": "test_1psidts",
            "HSID": "test",
            "SSID": "test",
            "APISID": "test",
            "SAPISID": "test",
        }
        yield m


@pytest.fixture
def mock_fetch():
    with patch.object(auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock) as m:
        m.return_value = ("csrf", "session")
        yield m


def _ask_result() -> AskResult:
    return AskResult(
        answer="characterization answer",
        conversation_id="a1b2c3d4-0000-0000-0000-000000000001",
        turn_number=1,
        is_follow_up=False,
        references=[],
        raw_response="",
    )


# ----------------------------------------------------------------------------
# chat ask: notebook-resolution paths
# ----------------------------------------------------------------------------


class TestChatNotebookResolution:
    def test_explicit_full_id_skips_notebook_listing(self, runner, mock_auth, mock_fetch):
        """``-n <full-UUID>`` fast-paths past ``notebooks.list``.

        UUID-shaped IDs (canonical 8-4-4-4-12 layout) MUST NOT trigger a
        backend listing - the explicit arg is the authoritative source.
        """
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        mock_client = create_mock_client()
        mock_client.chat.ask = AsyncMock(return_value=_ask_result())
        mock_client.chat.get_conversation_id = AsyncMock(return_value=None)

        result = runner.invoke(
            cli, ["ask", "what?", "-n", full_uuid], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0, result.output
        # No notebook listing should happen with a full UUID-shaped id.
        mock_client.notebooks.list.assert_not_called()

    def test_env_var_fallback_when_no_flag(
        self, runner, mock_auth, mock_fetch, monkeypatch, tmp_path
    ):
        """``NOTEBOOKLM_NOTEBOOK`` env var fills in when ``-n`` is omitted.

        Precedence ladder: ``-n`` flag > ``NOTEBOOKLM_NOTEBOOK`` env > active
        context > error. This test pins the env-var rung.
        """
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        monkeypatch.setenv("NOTEBOOKLM_NOTEBOOK", full_uuid)
        # Force context-file fallback to be unset so env-var is the
        # observable resolution path.
        monkeypatch.setattr(
            resolve_helpers,
            "get_context_path",
            lambda *a, **kw: tmp_path / "nonexistent.json",
        )

        mock_client = create_mock_client()
        mock_client.chat.ask = AsyncMock(return_value=_ask_result())
        mock_client.chat.get_conversation_id = AsyncMock(return_value=None)

        result = runner.invoke(cli, ["ask", "what?"], obj=inject_client(mock_client))

        assert result.exit_code == 0, result.output
        # ``chat.ask`` was called with the env-var-resolved notebook id.
        assert mock_client.chat.ask.await_count == 1
        nb_arg = mock_client.chat.ask.await_args.args[0]
        assert nb_arg == full_uuid

    def test_context_file_fallback_when_no_flag_and_no_env(
        self, runner, mock_auth, mock_fetch, monkeypatch, tmp_path
    ):
        """Active-context notebook id fills in last."""
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        ctx_file = tmp_path / "context.json"
        ctx_file.write_text(json.dumps({"notebook_id": full_uuid}))
        monkeypatch.delenv("NOTEBOOKLM_NOTEBOOK", raising=False)
        monkeypatch.setattr(resolve_helpers, "get_context_path", lambda *a, **kw: ctx_file)

        mock_client = create_mock_client()
        mock_client.chat.ask = AsyncMock(return_value=_ask_result())
        mock_client.chat.get_conversation_id = AsyncMock(return_value=None)

        result = runner.invoke(cli, ["ask", "what?"], obj=inject_client(mock_client))

        assert result.exit_code == 0, result.output
        assert mock_client.chat.ask.await_count == 1
        assert mock_client.chat.ask.await_args.args[0] == full_uuid

    def test_no_notebook_anywhere_exits_with_helpful_error(
        self, runner, mock_auth, mock_fetch, monkeypatch, tmp_path
    ):
        """No -n + no env-var + no context = exit 1 with discoverability hint.

        The error names the user-facing ``-n/--notebook`` flag, the
        ``NOTEBOOKLM_NOTEBOOK`` env var, AND the ``notebooklm use`` context
        path. All three resolution mechanisms must remain discoverable.
        """
        monkeypatch.delenv("NOTEBOOKLM_NOTEBOOK", raising=False)
        monkeypatch.setattr(
            resolve_helpers,
            "get_context_path",
            lambda *a, **kw: tmp_path / "nonexistent.json",
        )

        result = runner.invoke(cli, ["ask", "what?"])

        assert result.exit_code == 1
        combined = result.output + (result.stderr if result.stderr_bytes else "")
        assert "-n/--notebook" in combined
        assert "NOTEBOOKLM_NOTEBOOK" in combined
        assert "notebooklm use" in combined


# ----------------------------------------------------------------------------
# download <type>: notebook + artifact resolution paths
# ----------------------------------------------------------------------------


def _make_artifact_list(items: list[tuple[str, str]]) -> list:
    """Build a list of ``Artifact``-like instances the download path consumes.

    The download path checks ``isinstance(a, Artifact) and a.kind == ... and
    a.is_completed``. The on-disk dataclass uses ``_artifact_type`` (int code)
    and ``status`` (int code) under the hood; matching ``cli/test_download.py``
    fixtures keeps the real Artifact dataclass happy.
    """
    from datetime import datetime

    from notebooklm.types import Artifact, ArtifactStatus, ArtifactTypeCode

    out: list[Artifact] = []
    for aid, title in items:
        # Audio kind + completed status so the download_audio command matches.
        out.append(
            Artifact(
                id=aid,
                title=title,
                _artifact_type=int(ArtifactTypeCode.AUDIO),
                status=int(ArtifactStatus.COMPLETED),
                created_at=datetime(2024, 1, 1),
            )
        )
    return out


class TestDownloadNotebookResolution:
    def test_explicit_full_id_uses_arg_for_notebook(self, runner, mock_auth, mock_fetch, tmp_path):
        """``-n <full-UUID>`` is honored by ``download audio``.

        Resolver does not call ``notebooks.list`` for a UUID-shaped id.
        """
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(
            return_value=_make_artifact_list(
                [(f"art-{i}-1234-4abc-def0-1234567890ab", f"a{i}") for i in range(1)]
            )
        )
        mock_client.artifacts.download_audio = AsyncMock(return_value=str(tmp_path / "out.mp3"))

        result = runner.invoke(
            cli,
            [
                "download",
                "audio",
                "-n",
                full_uuid,
                "--latest",
                str(tmp_path / "out.mp3"),  # positional output path
            ],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        # No notebook listing fires for a full-UUID id.
        mock_client.notebooks.list.assert_not_called()

    def test_partial_artifact_id_resolves_locally(self, runner, mock_auth, mock_fetch, tmp_path):
        """``--artifact <prefix>`` resolves to a full artifact id locally.

        The download path uses ``resolve_partial_artifact_id`` against a
        pre-fetched ``artifacts.list``. A unique 3-char prefix should pick
        the right entry.
        """
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        artifacts = _make_artifact_list(
            [
                ("abcde000-aaaa-4abc-def0-000000000001", "first"),
                ("xyz00000-aaaa-4abc-def0-000000000002", "second"),
            ]
        )
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(return_value=artifacts)
        mock_client.artifacts.download_audio = AsyncMock(return_value=str(tmp_path / "out.mp3"))

        result = runner.invoke(
            cli,
            [
                "download",
                "audio",
                "-n",
                full_uuid,
                "--artifact",
                "abc",  # unique prefix of the first artifact id
                str(tmp_path / "out.mp3"),  # positional output path
            ],
            obj=inject_client(mock_client),
        )

        assert result.exit_code == 0, result.output
        # download_audio signature: (notebook_id, output_path, artifact_id=None).
        # The full artifact id (resolved from the "abc" prefix) is passed in
        # the third positional slot.
        mock_client.artifacts.download_audio.assert_awaited_once()
        call = mock_client.artifacts.download_audio.await_args
        called_id = call.kwargs.get("artifact_id")
        if called_id is None and len(call.args) >= 3:
            called_id = call.args[2]
        assert called_id == "abcde000-aaaa-4abc-def0-000000000001"

    def test_ambiguous_partial_artifact_id_surfaces_error(
        self, runner, mock_auth, mock_fetch, tmp_path
    ):
        """Ambiguous artifact prefix surfaces the ambiguity in stdout JSON path.

        The download command currently maps ``ValueError`` from
        ``resolve_partial_artifact_id`` into a ``{"error": ...}`` envelope
        rather than exiting non-zero. This test pins that contract so the
        refactor cannot accidentally promote the error to an exit-1 path.
        """
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        artifacts = _make_artifact_list(
            [
                ("abc11111-aaaa-4abc-def0-000000000001", "first"),
                ("abc22222-aaaa-4abc-def0-000000000002", "second"),
            ]
        )
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(return_value=artifacts)
        mock_client.artifacts.download_audio = AsyncMock()

        result = runner.invoke(
            cli,
            [
                "download",
                "audio",
                "-n",
                full_uuid,
                "--artifact",
                "abc",  # ambiguous prefix
                "--json",
            ],
            obj=inject_client(mock_client),
        )

        # Full contract: --json failure exits 1 (post-#925 exit-code parity)
        # with a {"error": ...} envelope on stdout, and the download_audio
        # mock MUST NOT be awaited because the resolver short-circuited
        # before dispatch.
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert "Ambiguous partial ID" in payload["error"]
        mock_client.artifacts.download_audio.assert_not_awaited()

    def test_unknown_partial_artifact_id_surfaces_error(
        self, runner, mock_auth, mock_fetch, tmp_path
    ):
        """Unknown artifact prefix surfaces ``not found`` in the output."""
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        artifacts = _make_artifact_list([("aaaa1111-aaaa-4abc-def0-000000000001", "only")])
        mock_client = create_mock_client()
        mock_client.artifacts.list = AsyncMock(return_value=artifacts)
        mock_client.artifacts.download_audio = AsyncMock()

        result = runner.invoke(
            cli,
            [
                "download",
                "audio",
                "-n",
                full_uuid,
                "--artifact",
                "zzz",  # no match
                "--json",
            ],
            obj=inject_client(mock_client),
        )

        # Full contract: --json failure exits 1 (post-#925 exit-code parity)
        # with {"error": "Artifact 'zzz' not found"} on stdout, and
        # download_audio MUST NOT be awaited.
        assert result.exit_code == 1, result.output
        payload = json.loads(result.stdout)
        assert payload == {"error": "Artifact 'zzz' not found"}
        mock_client.artifacts.download_audio.assert_not_awaited()


# ----------------------------------------------------------------------------
# chat.py conversation-ID fallback (the OTHER fallback ladder in this file)
# ----------------------------------------------------------------------------


class TestChatConversationFallback:
    """Pin the explicit->cached->server conversation-ID fallback in chat.py.

    The plan's "fallback order: explicit notebook -> cached -> server" reference
    actually applies to the conversation-id lookup in
    ``_determine_conversation_id`` + ``_get_latest_conversation_from_server``,
    not the notebook-id resolver. Keeping a regression test here guards
    against accidental coupling during the resolver refactor.
    """

    def test_explicit_conversation_id_wins(self, runner, mock_auth, mock_fetch):
        full_uuid = "abc12345-6789-4abc-def0-1234567890ab"
        conv_id = "11111111-2222-3333-4444-555555555555"
        mock_client = create_mock_client()
        mock_client.chat.ask = AsyncMock(return_value=_ask_result())
        mock_client.chat.get_conversation_id = AsyncMock(return_value="zzz-different-conv")

        result = runner.invoke(
            cli, ["ask", "what?", "-n", full_uuid, "-c", conv_id], obj=inject_client(mock_client)
        )

        assert result.exit_code == 0, result.output
        # The conversation_id passed to ``chat.ask`` must be the explicit one.
        passed_conv_id = mock_client.chat.ask.await_args.kwargs.get("conversation_id")
        assert passed_conv_id == conv_id
        # The server fallback ``get_conversation_id`` must NOT have fired
        # because the explicit -c short-circuited the fallback ladder.
        mock_client.chat.get_conversation_id.assert_not_called()
