"""E2E tests for SharingAPI."""

import pytest

from notebooklm import ShareAccess, SharePermission, ShareStatus, ShareViewLevel

from .conftest import requires_auth


@requires_auth
class TestSharingGetStatus:
    """Tests for SharingAPI.get_status() - read-only operations."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_get_status(self, client, temp_notebook):
        """Test getting share status for a notebook."""
        status = await client.sharing.get_status(temp_notebook.id)

        assert isinstance(status, ShareStatus)
        assert status.notebook_id == temp_notebook.id
        assert isinstance(status.is_public, bool)
        assert isinstance(status.access, ShareAccess)
        assert isinstance(status.view_level, ShareViewLevel)
        assert isinstance(status.shared_users, list)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_get_status_includes_owner(self, client, temp_notebook):
        """Test that share status includes the owner in shared_users."""
        status = await client.sharing.get_status(temp_notebook.id)

        # Owner should be in the list
        assert len(status.shared_users) >= 1

        # Check that owner has OWNER permission
        owners = [u for u in status.shared_users if u.permission == SharePermission.OWNER]
        assert len(owners) >= 1


@requires_auth
class TestSharingSetPublic:
    """Tests for SharingAPI.set_public() - modifies notebook state."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_set_public_true(self, client, temp_notebook):
        """Test enabling public sharing."""
        status = await client.sharing.set_public(temp_notebook.id, True)

        assert status.is_public is True
        assert status.access == ShareAccess.ANYONE_WITH_LINK
        assert status.share_url is not None
        assert temp_notebook.id in status.share_url

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_set_public_false(self, client, temp_notebook):
        """Test disabling public sharing."""
        # First enable it
        await client.sharing.set_public(temp_notebook.id, True)

        # Then disable it
        status = await client.sharing.set_public(temp_notebook.id, False)

        assert status.is_public is False
        assert status.access == ShareAccess.RESTRICTED
        assert status.share_url is None

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_set_public_idempotent(self, client, temp_notebook):
        """Test that setting public multiple times is idempotent."""
        # Enable twice
        status1 = await client.sharing.set_public(temp_notebook.id, True)
        status2 = await client.sharing.set_public(temp_notebook.id, True)

        assert status1.is_public == status2.is_public
        assert status1.share_url == status2.share_url


@requires_auth
class TestSharingSetViewLevel:
    """Tests for SharingAPI.set_view_level() - modifies notebook state."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_set_view_level_chat_only(self, client, temp_notebook):
        """Test setting view level to chat only."""
        # This should complete without error
        await client.sharing.set_view_level(temp_notebook.id, ShareViewLevel.CHAT_ONLY)

        # Note: GET_SHARE_STATUS doesn't return view_level, so we can't verify it directly
        # The test passes if no exception is raised

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_set_view_level_full_notebook(self, client, temp_notebook):
        """Test setting view level to full notebook."""
        await client.sharing.set_view_level(temp_notebook.id, ShareViewLevel.FULL_NOTEBOOK)

        # The test passes if no exception is raised


@requires_auth
class TestSharingWorkflow:
    """Tests for full sharing workflows - modifies notebook state."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_public_sharing_workflow(self, client, temp_notebook):
        """Test complete public sharing workflow."""
        # 1. Get initial status
        initial = await client.sharing.get_status(temp_notebook.id)
        assert initial.is_public is False

        # 2. Enable public sharing
        public_status = await client.sharing.set_public(temp_notebook.id, True)
        assert public_status.is_public is True
        assert public_status.share_url is not None

        # 3. Set view level to chat only
        await client.sharing.set_view_level(temp_notebook.id, ShareViewLevel.CHAT_ONLY)

        # 4. Verify status still shows public
        current = await client.sharing.get_status(temp_notebook.id)
        assert current.is_public is True

        # 5. Disable public sharing
        private_status = await client.sharing.set_public(temp_notebook.id, False)
        assert private_status.is_public is False
        assert private_status.share_url is None


@requires_auth
class TestSharingAPIAttributes:
    """Tests for SharingAPI availability on client."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_client_has_sharing_api(self, client):
        """Test that client has sharing API with all methods."""
        assert hasattr(client, "sharing")
        assert hasattr(client.sharing, "get_status")
        assert hasattr(client.sharing, "set_public")
        assert hasattr(client.sharing, "set_view_level")
        assert hasattr(client.sharing, "add_user")
        assert hasattr(client.sharing, "update_user")
        assert hasattr(client.sharing, "remove_user")
