"""Integration tests for NotesAPI."""

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient
from notebooklm.rpc import RPCMethod


class TestNotesAPI:
    """Integration tests for the NotesAPI."""

    @pytest.mark.asyncio
    async def test_list_notes(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing notes in a notebook."""
        response = build_rpc_response(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            [
                [
                    ["note_001", ["note_001", "Note content 1", None, None, "My First Note"]],
                    ["note_002", ["note_002", "Note content 2", None, None, "My Second Note"]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notes = await client.notes.list("nb_123")

        assert len(notes) == 2
        assert notes[0].id == "note_001"
        assert notes[0].title == "My First Note"
        assert notes[0].content == "Note content 1"
        assert notes[1].id == "note_002"
        assert notes[1].title == "My Second Note"

    @pytest.mark.asyncio
    async def test_list_notes_empty(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing notes when notebook is empty."""
        response = build_rpc_response(RPCMethod.GET_NOTES_AND_MIND_MAPS, [[]])
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notes = await client.notes.list("nb_123")

        assert notes == []

    @pytest.mark.asyncio
    async def test_list_notes_excludes_mind_maps(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test that list() filters out mind maps."""
        response = build_rpc_response(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            [
                [
                    ["note_001", ["note_001", "Regular note content", None, None, "Regular Note"]],
                    [
                        "mm_001",
                        ["mm_001", '{"title":"Mind Map","children":[]}', None, None, "Mind Map"],
                    ],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            notes = await client.notes.list("nb_123")

        assert len(notes) == 1
        assert notes[0].id == "note_001"

    @pytest.mark.asyncio
    async def test_get_note(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting a specific note by ID."""
        response = build_rpc_response(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            [
                [
                    ["note_001", ["note_001", "Content 1", None, None, "Note 1"]],
                    ["note_002", ["note_002", "Content 2", None, None, "Note 2"]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            note = await client.notes.get("nb_123", "note_002")

        assert note is not None
        assert note.id == "note_002"
        assert note.title == "Note 2"
        assert note.content == "Content 2"

    @pytest.mark.asyncio
    async def test_get_note_not_found(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test getting a note that doesn't exist."""
        response = build_rpc_response(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            [
                [
                    ["note_001", ["note_001", "Content", None, None, "Title"]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            note = await client.notes.get("nb_123", "nonexistent")

        assert note is None

    @pytest.mark.asyncio
    async def test_create_note(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test creating a new note."""
        create_response = build_rpc_response(RPCMethod.CREATE_NOTE, [["new_note_id"]])
        httpx_mock.add_response(content=create_response.encode())

        update_response = build_rpc_response(RPCMethod.UPDATE_NOTE, None)
        httpx_mock.add_response(content=update_response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            note = await client.notes.create("nb_123", "My Title", "My Content")

        assert note.id == "new_note_id"
        assert note.title == "My Title"
        assert note.content == "My Content"

        requests = httpx_mock.get_requests()
        assert RPCMethod.CREATE_NOTE in str(requests[0].url)
        assert RPCMethod.UPDATE_NOTE in str(requests[1].url)

    @pytest.mark.asyncio
    async def test_update_note(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test updating an existing note."""
        response = build_rpc_response(RPCMethod.UPDATE_NOTE, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            await client.notes.update("nb_123", "note_001", "Updated content", "Updated title")

        request = httpx_mock.get_request()
        assert RPCMethod.UPDATE_NOTE in str(request.url)
        assert "source-path=%2Fnotebook%2Fnb_123" in str(request.url)

    @pytest.mark.asyncio
    async def test_delete_note(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test deleting a note."""
        response = build_rpc_response(RPCMethod.DELETE_NOTE, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notes.delete("nb_123", "note_001")

        assert result is True
        request = httpx_mock.get_request()
        assert RPCMethod.DELETE_NOTE in str(request.url)

    @pytest.mark.asyncio
    async def test_list_mind_maps(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test listing mind maps in a notebook."""
        response = build_rpc_response(
            RPCMethod.GET_NOTES_AND_MIND_MAPS,
            [
                [
                    ["note_001", ["note_001", "Regular note", None, None, "Note"]],
                    [
                        "mm_001",
                        ["mm_001", '{"title":"Mind Map 1","children":[]}', None, None, "MM1"],
                    ],
                    ["mm_002", ["mm_002", '{"nodes":[{"id":"1"}]}', None, None, "MM2"]],
                ]
            ],
        )
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            mind_maps = await client.notes.list_mind_maps("nb_123")

        assert len(mind_maps) == 2

    @pytest.mark.asyncio
    async def test_delete_mind_map(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ):
        """Test deleting a mind map."""
        response = build_rpc_response(RPCMethod.DELETE_NOTE, None)
        httpx_mock.add_response(content=response.encode())

        async with NotebookLMClient(auth_tokens) as client:
            result = await client.notes.delete_mind_map("nb_123", "mm_001")

        assert result is True
        request = httpx_mock.get_request()
        assert RPCMethod.DELETE_NOTE in str(request.url)
