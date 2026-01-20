"""Tests for share CLI commands."""

from datetime import datetime
from unittest.mock import AsyncMock, patch

from notebooklm.notebooklm_cli import cli
from notebooklm.types import (
    Notebook,
    ShareAccess,
    SharedUser,
    SharePermission,
    ShareStatus,
    ShareViewLevel,
)

from .conftest import create_mock_client, patch_main_cli_client


def create_mock_share_status(
    notebook_id: str = "nb_123",
    is_public: bool = False,
    shared_users: list | None = None,
) -> ShareStatus:
    """Create a mock ShareStatus for testing."""
    return ShareStatus(
        notebook_id=notebook_id,
        is_public=is_public,
        access=ShareAccess.ANYONE_WITH_LINK if is_public else ShareAccess.RESTRICTED,
        view_level=ShareViewLevel.FULL_NOTEBOOK,
        shared_users=shared_users or [],
        share_url=f"https://notebooklm.google.com/notebook/{notebook_id}" if is_public else None,
    )


# =============================================================================
# SHARE STATUS TESTS
# =============================================================================


class TestShareStatus:
    def test_share_status_private(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.get_status = AsyncMock(
                return_value=create_mock_share_status(is_public=False)
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "status", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Private" in result.output
            mock_client.sharing.get_status.assert_called_once_with("nb_123")

    def test_share_status_public(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.get_status = AsyncMock(
                return_value=create_mock_share_status(is_public=True)
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "status", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Public" in result.output
            assert "Share URL" in result.output


# =============================================================================
# SHARE PUBLIC TESTS
# =============================================================================


class TestSharePublic:
    def test_share_public_enable(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.set_public = AsyncMock(
                return_value=create_mock_share_status(is_public=True)
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "public", "-n", "nb_123", "--enable"])

            assert result.exit_code == 0
            assert "Public sharing enabled" in result.output
            assert "Share URL" in result.output
            mock_client.sharing.set_public.assert_called_once_with("nb_123", True)

    def test_share_public_disable(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.set_public = AsyncMock(
                return_value=create_mock_share_status(is_public=False)
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "public", "-n", "nb_123", "--disable"])

            assert result.exit_code == 0
            assert "Public sharing disabled" in result.output
            mock_client.sharing.set_public.assert_called_once_with("nb_123", False)


# =============================================================================
# SHARE ADD USER TESTS
# =============================================================================


class TestShareAdd:
    def test_share_add_user_viewer(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.add_user = AsyncMock(return_value=create_mock_share_status())
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "add", "user@example.com", "-n", "nb_123"])

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
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.add_user = AsyncMock(return_value=create_mock_share_status())
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["share", "add", "user@example.com", "-n", "nb_123", "-p", "editor"],
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
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.remove_user = AsyncMock(return_value=create_mock_share_status())
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["share", "remove", "user@example.com", "-n", "nb_123", "-y"]
                )

            assert result.exit_code == 0
            assert "Removed access for user@example.com" in result.output
            mock_client.sharing.remove_user.assert_called_once_with("nb_123", "user@example.com")

    def test_share_remove_json(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.remove_user = AsyncMock(return_value=create_mock_share_status())
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["share", "remove", "user@example.com", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            assert '"removed_user": "user@example.com"' in result.output


# =============================================================================
# SHARE VIEW-LEVEL TESTS
# =============================================================================


class TestShareViewLevel:
    def test_share_view_level_full(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
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
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "view-level", "full", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Full Notebook" in result.output
            mock_client.sharing.set_view_level.assert_called_once_with(
                "nb_123", ShareViewLevel.FULL_NOTEBOOK
            )

    def test_share_view_level_chat(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
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
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "view-level", "chat", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Chat Only" in result.output
            mock_client.sharing.set_view_level.assert_called_once_with(
                "nb_123", ShareViewLevel.CHAT_ONLY
            )

    def test_share_view_level_json(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
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
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["share", "view-level", "full", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            assert '"view_level": "full_notebook"' in result.output


# =============================================================================
# SHARE UPDATE TESTS
# =============================================================================


class TestShareUpdate:
    def test_share_update_to_editor(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.update_user = AsyncMock(return_value=create_mock_share_status())
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli,
                    ["share", "update", "user@example.com", "-n", "nb_123", "-p", "editor"],
                )

            assert result.exit_code == 0
            assert "Updated user@example.com" in result.output
            assert "Editor" in result.output
            mock_client.sharing.update_user.assert_called_once_with(
                "nb_123", "user@example.com", SharePermission.EDITOR
            )

    def test_share_update_json(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.update_user = AsyncMock(return_value=create_mock_share_status())
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
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
                )

            assert result.exit_code == 0
            assert '"updated_user": "user@example.com"' in result.output
            assert '"permission": "viewer"' in result.output


# =============================================================================
# JSON OUTPUT TESTS
# =============================================================================


class TestShareJsonOutput:
    def test_share_status_json(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.get_status = AsyncMock(
                return_value=create_mock_share_status(is_public=True)
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "status", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            assert '"notebook_id": "nb_123"' in result.output
            assert '"is_public": true' in result.output

    def test_share_public_json(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.set_public = AsyncMock(
                return_value=create_mock_share_status(is_public=True)
            )
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "public", "-n", "nb_123", "--json"])

            assert result.exit_code == 0
            assert '"is_public": true' in result.output
            assert '"share_url"' in result.output

    def test_share_add_json(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
            mock_client = create_mock_client()
            mock_client.notebooks.list = AsyncMock(
                return_value=[
                    Notebook(id="nb_123", title="Test", created_at=datetime(2024, 1, 1)),
                ]
            )
            mock_client.sharing.add_user = AsyncMock(return_value=create_mock_share_status())
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(
                    cli, ["share", "add", "user@example.com", "-n", "nb_123", "--json"]
                )

            assert result.exit_code == 0
            assert '"added_user": "user@example.com"' in result.output
            assert '"permission": "viewer"' in result.output


# =============================================================================
# SHARED USERS DISPLAY TESTS
# =============================================================================


class TestShareStatusWithUsers:
    def test_share_status_with_shared_users(self, runner, mock_auth):
        with patch_main_cli_client() as mock_client_cls:
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
            mock_client_cls.return_value = mock_client

            with patch("notebooklm.cli.helpers.fetch_tokens", new_callable=AsyncMock) as mock_fetch:
                mock_fetch.return_value = ("csrf", "session")
                result = runner.invoke(cli, ["share", "status", "-n", "nb_123"])

            assert result.exit_code == 0
            assert "Shared Users" in result.output
            assert "editor@example.com" in result.output
            assert "viewer@example.com" in result.output
