"""Integration tests for SharingAPI."""

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient, SharePermission, ShareViewLevel
from notebooklm.rpc import RPCMethod


class TestGetShareStatus:
    """Tests for SharingAPI.get_status()."""

    @pytest.mark.asyncio
    async def test_get_status_public_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting status for a public notebook."""
        response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [
                [
                    ["owner@example.com", 1, [], ["Owner Name", "https://avatar.url"]],
                    ["viewer@example.com", 3, [], ["Viewer Name", "https://viewer.url"]],
                ],
                [True],
                1000,
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.get_status("nb_123")

        assert status.notebook_id == "nb_123"
        assert status.is_public is True
        assert len(status.shared_users) == 2
        assert status.shared_users[0].email == "owner@example.com"
        assert status.share_url == "https://notebooklm.google.com/notebook/nb_123"

    @pytest.mark.asyncio
    async def test_get_status_private_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting status for a private notebook."""
        response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [
                [["owner@example.com", 1, [], ["Owner", "https://avatar"]]],
                [False],
                1000,
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.get_status("nb_123")

        assert status.is_public is False
        assert status.share_url is None

    @pytest.mark.asyncio
    async def test_get_status_request_format(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test get_status sends correct RPC request."""
        response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [[], [False], 1000],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.sharing.get_status("nb_123")

        request = httpx_mock.get_request()
        assert RPCMethod.GET_SHARE_STATUS.value in str(request.url)
        assert "source-path=%2Fnotebook%2Fnb_123" in str(request.url)


class TestSetPublic:
    """Tests for SharingAPI.set_public()."""

    @pytest.mark.asyncio
    async def test_set_public_true(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test enabling public sharing."""
        # First call: SHARE_NOTEBOOK (returns empty)
        share_response = build_rpc_response(RPCMethod.SHARE_NOTEBOOK, [])
        httpx_mock.add_response(content=share_response.encode())

        # Second call: GET_SHARE_STATUS (returns updated status)
        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [[["owner@example.com", 1, [], ["Owner", "https://avatar"]]], [True], 1000],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.set_public("nb_123", True)

        assert status.is_public is True
        assert status.share_url is not None

        # Verify the SHARE_NOTEBOOK request
        requests = httpx_mock.get_requests()
        assert len(requests) == 2
        assert RPCMethod.SHARE_NOTEBOOK.value in str(requests[0].url)

    @pytest.mark.asyncio
    async def test_set_public_false(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test disabling public sharing."""
        share_response = build_rpc_response(RPCMethod.SHARE_NOTEBOOK, [])
        httpx_mock.add_response(content=share_response.encode())

        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [[["owner@example.com", 1, [], ["Owner", "https://avatar"]]], [False], 1000],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.set_public("nb_123", False)

        assert status.is_public is False
        assert status.share_url is None


class TestSetViewLevel:
    """Tests for SharingAPI.set_view_level()."""

    @pytest.mark.asyncio
    async def test_set_view_level_chat_only(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test setting view level to chat only."""
        # First call: RENAME_NOTEBOOK (to set view level)
        rename_response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=rename_response.encode())

        # Second call: GET_SHARE_STATUS (to get current status)
        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [[["owner@example.com", 1, [], ["Owner", "https://avatar"]]], [False], 1000],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.set_view_level("nb_123", ShareViewLevel.CHAT_ONLY)

        # Verify the returned status has the correct view_level we set
        assert status.view_level == ShareViewLevel.CHAT_ONLY

        requests = httpx_mock.get_requests()
        assert len(requests) == 2
        assert RPCMethod.RENAME_NOTEBOOK.value in str(requests[0].url)
        assert RPCMethod.GET_SHARE_STATUS.value in str(requests[1].url)

    @pytest.mark.asyncio
    async def test_set_view_level_full_notebook(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test setting view level to full notebook."""
        # First call: RENAME_NOTEBOOK (to set view level)
        rename_response = build_rpc_response(RPCMethod.RENAME_NOTEBOOK, None)
        httpx_mock.add_response(content=rename_response.encode())

        # Second call: GET_SHARE_STATUS (to get current status)
        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [[["owner@example.com", 1, [], ["Owner", "https://avatar"]]], [False], 1000],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.set_view_level("nb_123", ShareViewLevel.FULL_NOTEBOOK)

        # Verify the returned status has the correct view_level we set
        assert status.view_level == ShareViewLevel.FULL_NOTEBOOK

        requests = httpx_mock.get_requests()
        assert len(requests) == 2
        assert RPCMethod.RENAME_NOTEBOOK.value in str(requests[0].url)


class TestAddUser:
    """Tests for SharingAPI.add_user()."""

    @pytest.mark.asyncio
    async def test_add_user_as_viewer(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test adding a user as viewer."""
        share_response = build_rpc_response(RPCMethod.SHARE_NOTEBOOK, [])
        httpx_mock.add_response(content=share_response.encode())

        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [
                [
                    ["owner@example.com", 1, [], ["Owner", "https://avatar"]],
                    ["new@example.com", 3, [], ["New User", "https://new.avatar"]],
                ],
                [False],
                1000,
            ],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.add_user(
                "nb_123",
                "new@example.com",
                SharePermission.VIEWER,
                notify=True,
            )

        assert len(status.shared_users) == 2
        assert status.shared_users[1].email == "new@example.com"
        assert status.shared_users[1].permission == SharePermission.VIEWER

    @pytest.mark.asyncio
    async def test_add_user_as_editor(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test adding a user as editor."""
        share_response = build_rpc_response(RPCMethod.SHARE_NOTEBOOK, [])
        httpx_mock.add_response(content=share_response.encode())

        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [
                [
                    ["owner@example.com", 1, [], ["Owner", "https://avatar"]],
                    ["editor@example.com", 2, [], ["Editor", "https://editor.avatar"]],
                ],
                [False],
                1000,
            ],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.add_user(
                "nb_123",
                "editor@example.com",
                SharePermission.EDITOR,
            )

        assert status.shared_users[1].permission == SharePermission.EDITOR

    @pytest.mark.asyncio
    async def test_add_user_with_welcome_message(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test adding a user with a welcome message."""
        share_response = build_rpc_response(RPCMethod.SHARE_NOTEBOOK, [])
        httpx_mock.add_response(content=share_response.encode())

        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [
                [
                    ["owner@example.com", 1, [], ["Owner", "https://avatar"]],
                    ["new@example.com", 3, [], ["New User", "https://avatar"]],
                ],
                [False],
                1000,
            ],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.add_user(
                "nb_123",
                "new@example.com",
                welcome_message="Welcome to my notebook!",
            )

        assert len(status.shared_users) == 2


class TestUpdateUser:
    """Tests for SharingAPI.update_user()."""

    @pytest.mark.asyncio
    async def test_update_user_permission(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test updating a user's permission."""
        share_response = build_rpc_response(RPCMethod.SHARE_NOTEBOOK, [])
        httpx_mock.add_response(content=share_response.encode())

        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [
                [
                    ["owner@example.com", 1, [], ["Owner", "https://avatar"]],
                    ["user@example.com", 2, [], ["User", "https://avatar"]],  # Now editor
                ],
                [False],
                1000,
            ],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.update_user(
                "nb_123",
                "user@example.com",
                SharePermission.EDITOR,
            )

        assert status.shared_users[1].permission == SharePermission.EDITOR


class TestRemoveUser:
    """Tests for SharingAPI.remove_user()."""

    @pytest.mark.asyncio
    async def test_remove_user(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test removing a user."""
        share_response = build_rpc_response(RPCMethod.SHARE_NOTEBOOK, [])
        httpx_mock.add_response(content=share_response.encode())

        # After removal, only owner remains
        status_response = build_rpc_response(
            RPCMethod.GET_SHARE_STATUS,
            [
                [["owner@example.com", 1, [], ["Owner", "https://avatar"]]],
                [False],
                1000,
            ],
        )
        httpx_mock.add_response(content=status_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            status = await client.sharing.remove_user("nb_123", "removed@example.com")

        assert len(status.shared_users) == 1
        assert status.shared_users[0].email == "owner@example.com"


class TestSharingAPIIntegration:
    """Additional integration tests for SharingAPI."""

    @pytest.mark.asyncio
    async def test_client_has_sharing_attribute(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ):
        """Test that NotebookLMClient has sharing API."""
        async with NotebookLMClient(auth_tokens) as client:
            assert hasattr(client, "sharing")
            assert hasattr(client.sharing, "get_status")
            assert hasattr(client.sharing, "set_public")
            assert hasattr(client.sharing, "set_view_level")
            assert hasattr(client.sharing, "add_user")
            assert hasattr(client.sharing, "update_user")
            assert hasattr(client.sharing, "remove_user")
