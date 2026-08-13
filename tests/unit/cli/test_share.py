"""Tests for share CLI commands."""

import json
from datetime import datetime
from unittest.mock import AsyncMock, patch

import notebooklm.auth as auth_module
from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    Notebook,
    ShareAccess,
    SharedUser,
    SharePermission,
    ShareStatus,
    ShareViewLevel,
)

from .conftest import create_mock_client, inject_client


def create_mock_share_status(
    notebook_id: str = "nb_123",
    is_public: bool = False,
    shared_users: list | None = None,
    max_individuals_share_limit: int | None = None,
    is_public_sharing_allowed: bool | None = None,
) -> ShareStatus:
    """Create a mock ShareStatus for testing.

    The two #2130 fields default to ``None`` — the real parser's "backend made
    no claim" — so callers must opt in to a populated value rather than
    inheriting one the wire may not have sent.
    """
    return ShareStatus(
        notebook_id=notebook_id,
        is_public=is_public,
        access=ShareAccess.ANYONE_WITH_LINK if is_public else ShareAccess.RESTRICTED,
        view_level=ShareViewLevel.FULL_NOTEBOOK,
        shared_users=shared_users or [],
        share_url=f"https://notebooklm.google.com/notebook/{notebook_id}" if is_public else None,
        max_individuals_share_limit=max_individuals_share_limit,
        is_public_sharing_allowed=is_public_sharing_allowed,
    )


# =============================================================================
# SHARE STATUS TESTS
# =============================================================================


class TestShareStatus:
    def test_share_status_private(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.get_status = AsyncMock(
            return_value=create_mock_share_status(is_public=False)
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "status", "-n", "nb_123"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert "Private" in result.output
        mock_client.sharing.get_status.assert_called_once_with("nb_123")

    def test_share_status_public(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.get_status = AsyncMock(
            return_value=create_mock_share_status(is_public=True)
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "status", "-n", "nb_123"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert "Public" in result.output
        assert "Share URL" in result.output


# =============================================================================
# SHARE PUBLIC TESTS
# =============================================================================


class TestSharePublic:
    def test_share_public_enable(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.set_public = AsyncMock(
            return_value=create_mock_share_status(is_public=True)
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "public", "-n", "nb_123", "--enable"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert "Public sharing enabled" in result.output
        assert "Share URL" in result.output
        mock_client.sharing.set_public.assert_called_once_with("nb_123", True)

    def test_share_public_disable(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.set_public = AsyncMock(
            return_value=create_mock_share_status(is_public=False)
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "public", "-n", "nb_123", "--disable"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Public sharing disabled" in result.output
        mock_client.sharing.set_public.assert_called_once_with("nb_123", False)


# =============================================================================
# SHARE ADD USER TESTS
# =============================================================================


class TestShareAdd:
    def test_share_add_user_viewer(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.add_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "add", "user@example.com", "-n", "nb_123"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Shared with user@example.com" in result.output
        assert "Viewer" in result.output
        mock_client.sharing.add_user.assert_called_once_with(
            "nb_123",
            "user@example.com",
            permission=SharePermission.VIEWER,
            notify=True,
            welcome_message="",
        )

    def test_share_add_user_editor(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.add_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "add", "user@example.com", "-n", "nb_123", "-p", "editor"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Editor" in result.output
        mock_client.sharing.add_user.assert_called_once_with(
            "nb_123",
            "user@example.com",
            permission=SharePermission.EDITOR,
            notify=True,
            welcome_message="",
        )


# =============================================================================
# SHARE REMOVE USER TESTS
# =============================================================================


class TestShareRemove:
    def test_share_remove_user(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.remove_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "remove", "user@example.com", "-n", "nb_123", "-y"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Removed access for user@example.com" in result.output
        mock_client.sharing.remove_user.assert_called_once_with("nb_123", "user@example.com")

    def test_share_remove_cancelled(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.remove_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "remove", "user@example.com", "-n", "nb_123"],
                input="n\n",
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Remove access for user@example.com?" in result.output
        mock_client.sharing.remove_user.assert_not_called()

    def test_share_remove_json(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.remove_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "remove", "user@example.com", "-n", "nb_123", "--json"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert '"removed_user": "user@example.com"' in result.output

    def test_share_remove_json_without_yes_does_not_prompt(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.remove_user = AsyncMock(return_value=create_mock_share_status())

        with (
            patch.object(
                auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
            ) as mock_fetch,
            patch("click.confirm") as mock_confirm,
        ):
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "remove", "user@example.com", "-n", "nb_123", "--json"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert '"removed_user": "user@example.com"' in result.output
        mock_confirm.assert_not_called()
        mock_client.sharing.remove_user.assert_called_once_with("nb_123", "user@example.com")


# =============================================================================
# SHARE VIEW-LEVEL TESTS
# =============================================================================


class TestShareViewLevel:
    def test_share_view_level_full(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        # set_view_level now returns ShareStatus with the view_level that was set
        mock_status = ShareStatus(
            notebook_id="nb_123",
            is_public=False,
            access=ShareAccess.RESTRICTED,
            view_level=ShareViewLevel.FULL_NOTEBOOK,
            shared_users=[],
            share_url=None,
        )
        mock_client.sharing.set_view_level = AsyncMock(return_value=mock_status)

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "view-level", "full", "-n", "nb_123"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert "Full Notebook" in result.output
        mock_client.sharing.set_view_level.assert_called_once_with(
            "nb_123", ShareViewLevel.FULL_NOTEBOOK
        )

    def test_share_view_level_chat(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        # set_view_level now returns ShareStatus with the view_level that was set
        mock_status = ShareStatus(
            notebook_id="nb_123",
            is_public=False,
            access=ShareAccess.RESTRICTED,
            view_level=ShareViewLevel.CHAT_ONLY,
            shared_users=[],
            share_url=None,
        )
        mock_client.sharing.set_view_level = AsyncMock(return_value=mock_status)

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "view-level", "chat", "-n", "nb_123"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert "Chat Only" in result.output
        mock_client.sharing.set_view_level.assert_called_once_with(
            "nb_123", ShareViewLevel.CHAT_ONLY
        )

    def test_share_view_level_json(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        # set_view_level now returns ShareStatus with the view_level that was set
        mock_status = ShareStatus(
            notebook_id="nb_123",
            is_public=False,
            access=ShareAccess.RESTRICTED,
            view_level=ShareViewLevel.FULL_NOTEBOOK,
            shared_users=[],
            share_url=None,
        )
        mock_client.sharing.set_view_level = AsyncMock(return_value=mock_status)

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "view-level", "full", "-n", "nb_123", "--json"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert '"view_level": "full_notebook"' in result.output


# =============================================================================
# SHARE UPDATE TESTS
# =============================================================================


class TestShareUpdate:
    def test_share_update_to_editor(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.update_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "update", "user@example.com", "-n", "nb_123", "-p", "editor"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert "Updated user@example.com" in result.output
        assert "Editor" in result.output
        mock_client.sharing.update_user.assert_called_once_with(
            "nb_123", "user@example.com", SharePermission.EDITOR
        )

    def test_share_update_json(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.update_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                [
                    "share",
                    "update",
                    "user@example.com",
                    "-n",
                    "nb_123",
                    "-p",
                    "viewer",
                    "--json",
                ],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert '"updated_user": "user@example.com"' in result.output
        assert '"permission": "viewer"' in result.output


# =============================================================================
# JSON OUTPUT TESTS
# =============================================================================


class TestShareJsonOutput:
    def test_share_status_json(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.get_status = AsyncMock(
            return_value=create_mock_share_status(is_public=True)
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "status", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert '"notebook_id": "nb_123"' in result.output
        assert '"is_public": true' in result.output

    def _invoke_status(self, runner, mock_client, *args):
        mock_client.notebooks.list = AsyncMock(
            return_value=[Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1))]
        )
        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            return runner.invoke(
                cli,
                ["share", "status", "-n", "nb_123", *args],
                obj=inject_client(mock_client),
            )

    def test_share_status_json_carries_capacity_and_policy(self, runner, mock_auth):
        """``share status --json`` exposes the #2130 fields.

        This payload is hand-built rather than routed through
        ``_app.views.share_status_view``, so adding a field to the dataclass does
        NOT reach CLI users — the same narrow-serializer gap that hid two earlier
        additions from ``source list --json``.
        """
        mock_client = create_mock_client()
        mock_client.sharing.get_status = AsyncMock(
            return_value=create_mock_share_status(
                max_individuals_share_limit=1000, is_public_sharing_allowed=False
            )
        )

        result = self._invoke_status(runner, mock_client, "--json")

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["max_individuals_share_limit"] == 1000
        assert payload["is_public_sharing_allowed"] is False
        # The verdict reaches CLI JSON too, matching MCP/REST. Without it a CLI
        # consumer has to re-derive it as ``not is_public_sharing_allowed``,
        # which is wrong on the unknown case.
        assert payload["is_public_sharing_denied"] is True

    def test_share_status_json_reports_absent_fields_as_null(self, runner, mock_auth):
        """Keys stay present and ``null`` when the backend made no claim."""
        mock_client = create_mock_client()
        mock_client.sharing.get_status = AsyncMock(return_value=create_mock_share_status())

        result = self._invoke_status(runner, mock_client, "--json")

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["max_individuals_share_limit"] is None
        assert payload["is_public_sharing_allowed"] is None
        # No claim is not a denial.
        assert payload["is_public_sharing_denied"] is False

    def test_share_status_human_output_warns_when_policy_forbids_public(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.sharing.get_status = AsyncMock(
            return_value=create_mock_share_status(
                max_individuals_share_limit=1000, is_public_sharing_allowed=False
            )
        )

        result = self._invoke_status(runner, mock_client)

        assert result.exit_code == 0
        assert "Not allowed by policy" in result.output
        assert "1000" in result.output

    def test_share_status_human_output_reports_the_allowed_case(self, runner, mock_auth):
        """The ``True`` branch — what every real account hits (live: 10/10).

        Covered explicitly because deleting this branch outright left the whole
        suite green: the deny and silent cases were tested and the common one
        was not.
        """
        mock_client = create_mock_client()
        mock_client.sharing.get_status = AsyncMock(
            return_value=create_mock_share_status(
                max_individuals_share_limit=1000, is_public_sharing_allowed=True
            )
        )

        result = self._invoke_status(runner, mock_client)

        assert result.exit_code == 0
        assert "Allowed" in result.output
        assert "Not allowed by policy" not in result.output
        assert "1000" in result.output

    def test_share_status_human_output_is_silent_when_no_claim(self, runner, mock_auth):
        """``None`` must print nothing — not "no".

        Rendering a policy verdict the backend never stated would be a
        confidently-wrong claim, which is the whole reason the field is
        tri-state.
        """
        mock_client = create_mock_client()
        mock_client.sharing.get_status = AsyncMock(return_value=create_mock_share_status())

        result = self._invoke_status(runner, mock_client)

        assert result.exit_code == 0
        assert "Public Sharing:" not in result.output
        assert "Collaborator Limit:" not in result.output

    def test_share_public_json(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.set_public = AsyncMock(
            return_value=create_mock_share_status(is_public=True)
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "public", "-n", "nb_123", "--json"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert '"is_public": true' in result.output
        assert '"share_url"' in result.output

    def test_share_add_json(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        mock_client.sharing.add_user = AsyncMock(return_value=create_mock_share_status())

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli,
                ["share", "add", "user@example.com", "-n", "nb_123", "--json"],
                obj=inject_client(mock_client),
            )

        assert result.exit_code == 0
        assert '"added_user": "user@example.com"' in result.output
        assert '"permission": "viewer"' in result.output


# =============================================================================
# SHARED USERS DISPLAY TESTS
# =============================================================================


class TestShareStatusWithUsers:
    def test_share_status_with_shared_users(self, runner, mock_auth):
        mock_client = create_mock_client()
        mock_client.notebooks.list = AsyncMock(
            return_value=[
                Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
            ]
        )
        shared_users = [
            SharedUser(
                email="editor@example.com",
                permission=SharePermission.EDITOR,
                display_name="Editor User",
            ),
            SharedUser(
                email="viewer@example.com",
                permission=SharePermission.VIEWER,
                display_name=None,
            ),
        ]
        mock_client.sharing.get_status = AsyncMock(
            return_value=create_mock_share_status(is_public=False, shared_users=shared_users)
        )

        with patch.object(
            auth_module, "fetch_tokens_with_domains", new_callable=AsyncMock
        ) as mock_fetch:
            mock_fetch.return_value = ("csrf", "session")
            result = runner.invoke(
                cli, ["share", "status", "-n", "nb_123"], obj=inject_client(mock_client)
            )

        assert result.exit_code == 0
        assert "Shared Users" in result.output
        assert "editor@example.com" in result.output
        assert "viewer@example.com" in result.output
