"""Unit tests for sharing types and API."""

import pytest

from notebooklm.rpc.types import ShareAccess, SharePermission, ShareViewLevel
from notebooklm.types import SharedUser, ShareStatus


class TestSharedUser:
    """Tests for SharedUser dataclass."""

    def test_from_api_response_full(self):
        """Test parsing with all fields present."""
        data = ["user@example.com", 3, [], ["Test User", "https://avatar.url"]]
        user = SharedUser.from_api_response(data)

        assert user.email == "user@example.com"
        assert user.permission == SharePermission.VIEWER
        assert user.display_name == "Test User"
        assert user.avatar_url == "https://avatar.url"

    def test_from_api_response_editor(self):
        """Test parsing editor permission."""
        data = ["editor@example.com", 2, [], ["Editor Name", "https://editor.avatar"]]
        user = SharedUser.from_api_response(data)

        assert user.email == "editor@example.com"
        assert user.permission == SharePermission.EDITOR
        assert user.display_name == "Editor Name"

    def test_from_api_response_owner(self):
        """Test parsing owner permission."""
        data = ["owner@example.com", 1, [], ["Owner Name", "https://owner.avatar"]]
        user = SharedUser.from_api_response(data)

        assert user.email == "owner@example.com"
        assert user.permission == SharePermission.OWNER

    def test_from_api_response_minimal(self):
        """Test parsing with minimal fields."""
        data = ["user@example.com", 2, []]
        user = SharedUser.from_api_response(data)

        assert user.email == "user@example.com"
        assert user.permission == SharePermission.EDITOR
        assert user.display_name is None
        assert user.avatar_url is None

    def test_from_api_response_unknown_permission(self):
        """Test parsing with unknown permission value defaults to VIEWER."""
        data = ["user@example.com", 99, []]
        user = SharedUser.from_api_response(data)

        assert user.permission == SharePermission.VIEWER

    def test_from_api_response_empty(self):
        """Test parsing with empty data."""
        data = []
        user = SharedUser.from_api_response(data)

        assert user.email == ""
        assert user.permission == SharePermission.VIEWER

    def test_from_api_response_partial_user_info(self):
        """Test parsing with partial user info (only name, no avatar)."""
        data = ["user@example.com", 3, [], ["Just Name"]]
        user = SharedUser.from_api_response(data)

        assert user.display_name == "Just Name"
        assert user.avatar_url is None


class TestShareStatus:
    """Tests for ShareStatus dataclass."""

    def test_from_api_response_public(self):
        """Test parsing public notebook."""
        data = [
            [["owner@example.com", 1, [], ["Owner", "https://avatar"]]],
            [True],
            1000,
        ]
        status = ShareStatus.from_api_response(data, "notebook-123")

        assert status.notebook_id == "notebook-123"
        assert status.is_public is True
        assert status.access == ShareAccess.ANYONE_WITH_LINK
        assert status.view_level == ShareViewLevel.FULL_NOTEBOOK
        assert len(status.shared_users) == 1
        assert status.shared_users[0].email == "owner@example.com"
        assert status.share_url == "https://notebooklm.google.com/notebook/notebook-123"

    def test_from_api_response_private(self):
        """Test parsing private/restricted notebook."""
        data = [
            [["owner@example.com", 1, [], ["Owner", "https://avatar"]]],
            [False],
            1000,
        ]
        status = ShareStatus.from_api_response(data, "notebook-456")

        assert status.notebook_id == "notebook-456"
        assert status.is_public is False
        assert status.access == ShareAccess.RESTRICTED
        assert status.share_url is None

    def test_from_api_response_multiple_users(self):
        """Test parsing with multiple shared users."""
        data = [
            [
                ["owner@example.com", 1, [], ["Owner", "https://owner.avatar"]],
                ["editor@example.com", 2, [], ["Editor", "https://editor.avatar"]],
                ["viewer@example.com", 3, [], ["Viewer", "https://viewer.avatar"]],
            ],
            [True],
            1000,
        ]
        status = ShareStatus.from_api_response(data, "notebook-789")

        assert len(status.shared_users) == 3
        assert status.shared_users[0].permission == SharePermission.OWNER
        assert status.shared_users[1].permission == SharePermission.EDITOR
        assert status.shared_users[2].permission == SharePermission.VIEWER

    def test_from_api_response_empty_users(self):
        """Test parsing with no users."""
        data = [[], [False], 1000]
        status = ShareStatus.from_api_response(data, "notebook-empty")

        assert status.shared_users == []
        assert status.is_public is False

    def test_from_api_response_empty_is_public(self):
        """Test parsing when is_public list is empty."""
        data = [[], [], 1000]
        status = ShareStatus.from_api_response(data, "notebook-empty")

        assert status.is_public is False
        assert status.access == ShareAccess.RESTRICTED


class TestShareEnums:
    """Tests for share-related enums."""

    def test_share_access_values(self):
        """Test ShareAccess enum values."""
        assert ShareAccess.RESTRICTED.value == 0
        assert ShareAccess.ANYONE_WITH_LINK.value == 1

    def test_share_view_level_values(self):
        """Test ShareViewLevel enum values."""
        assert ShareViewLevel.FULL_NOTEBOOK.value == 0
        assert ShareViewLevel.CHAT_ONLY.value == 1

    def test_share_permission_values(self):
        """Test SharePermission enum values."""
        assert SharePermission.OWNER.value == 1
        assert SharePermission.EDITOR.value == 2
        assert SharePermission.VIEWER.value == 3
        assert SharePermission._REMOVE.value == 4

    def test_share_access_is_int_enum(self):
        """Test ShareAccess can be used as int."""
        assert int(ShareAccess.RESTRICTED) == 0
        assert int(ShareAccess.ANYONE_WITH_LINK) == 1

    def test_share_view_level_is_int_enum(self):
        """Test ShareViewLevel can be used as int."""
        assert int(ShareViewLevel.FULL_NOTEBOOK) == 0
        assert int(ShareViewLevel.CHAT_ONLY) == 1


class TestSharingAPIValidation:
    """Tests for SharingAPI input validation."""

    @pytest.mark.asyncio
    async def test_add_user_rejects_owner_permission(self):
        """Test that add_user rejects OWNER permission."""
        from unittest.mock import AsyncMock, MagicMock

        from notebooklm._sharing import SharingAPI

        mock_core = MagicMock()
        mock_core.rpc_call = AsyncMock()
        api = SharingAPI(mock_core)

        with pytest.raises(ValueError, match="Cannot assign OWNER permission"):
            await api.add_user("nb_123", "test@example.com", SharePermission.OWNER)

        # Verify no RPC call was made
        mock_core.rpc_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_user_rejects_remove_permission(self):
        """Test that add_user rejects _REMOVE permission."""
        from unittest.mock import AsyncMock, MagicMock

        from notebooklm._sharing import SharingAPI

        mock_core = MagicMock()
        mock_core.rpc_call = AsyncMock()
        api = SharingAPI(mock_core)

        with pytest.raises(ValueError, match="Use remove_user"):
            await api.add_user("nb_123", "test@example.com", SharePermission._REMOVE)

        mock_core.rpc_call.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_user_accepts_editor_permission(self):
        """Test that add_user accepts EDITOR permission."""
        from unittest.mock import AsyncMock, MagicMock

        from notebooklm._sharing import SharingAPI

        mock_core = MagicMock()
        # Return empty list for share call, then mock get_status
        mock_core.rpc_call = AsyncMock(
            side_effect=[
                [],  # SHARE_NOTEBOOK response
                [  # GET_SHARE_STATUS response
                    [["test@example.com", 2, [], ["Test", "https://avatar"]]],
                    [False],
                    1000,
                ],
            ]
        )
        api = SharingAPI(mock_core)

        status = await api.add_user("nb_123", "test@example.com", SharePermission.EDITOR)

        assert mock_core.rpc_call.call_count == 2
        assert len(status.shared_users) == 1
        assert status.shared_users[0].permission == SharePermission.EDITOR

    @pytest.mark.asyncio
    async def test_add_user_accepts_viewer_permission(self):
        """Test that add_user accepts VIEWER permission (default)."""
        from unittest.mock import AsyncMock, MagicMock

        from notebooklm._sharing import SharingAPI

        mock_core = MagicMock()
        mock_core.rpc_call = AsyncMock(
            side_effect=[
                [],  # SHARE_NOTEBOOK response
                [  # GET_SHARE_STATUS response
                    [["test@example.com", 3, [], ["Test", "https://avatar"]]],
                    [False],
                    1000,
                ],
            ]
        )
        api = SharingAPI(mock_core)

        # Use default permission (VIEWER)
        status = await api.add_user("nb_123", "test@example.com")

        assert mock_core.rpc_call.call_count == 2
        assert status.shared_users[0].permission == SharePermission.VIEWER


class TestShareStatusDefaultValues:
    """Test ShareStatus default values and edge cases."""

    def test_default_view_level_is_full_notebook(self):
        """ShareStatus defaults view_level to FULL_NOTEBOOK."""
        data = [[], [True], 1000]
        status = ShareStatus.from_api_response(data, "nb_123")
        assert status.view_level == ShareViewLevel.FULL_NOTEBOOK

    def test_share_url_format(self):
        """Test share URL is correctly formatted."""
        data = [[], [True], 1000]
        status = ShareStatus.from_api_response(data, "abc-123-xyz")
        assert status.share_url == "https://notebooklm.google.com/notebook/abc-123-xyz"

    def test_share_url_none_when_private(self):
        """Test share URL is None when notebook is private."""
        data = [[], [False], 1000]
        status = ShareStatus.from_api_response(data, "nb_123")
        assert status.share_url is None

    def test_shared_users_is_mutable_list(self):
        """Test that shared_users default is a mutable list."""
        data = [[], [False], 1000]
        status1 = ShareStatus.from_api_response(data, "nb_1")
        status2 = ShareStatus.from_api_response(data, "nb_2")

        # Modifying one should not affect the other
        status1.shared_users.append(
            SharedUser(email="test@example.com", permission=SharePermission.VIEWER)
        )
        assert len(status1.shared_users) == 1
        assert len(status2.shared_users) == 0
