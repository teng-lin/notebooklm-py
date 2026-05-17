"""Tests for ``MindMapService`` injection into ``NotesAPI``.

``NotesAPI`` and ``ArtifactsAPI`` both depend on the mind-map service
through a constructor seam. These tests pin four contracts:

1. ``_get_all_notes_and_mind_maps()`` delegates to the injected service.
2. When no ``mind_map_service`` is injected, ``NotesAPI`` installs a
   default ``MindMapService(core)``.
3. ``mind_map_service`` is keyword-only so the positional constructor
   contract stays unchanged.
4. A caller-supplied falsy ``mind_map_service`` instance is preserved
   verbatim (the ``is None`` defaulting idiom, not truthy-``or``).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import _mind_map
from notebooklm._capabilities import ClientCoreCapabilities
from notebooklm._notes import NotesAPI


@pytest.mark.asyncio
async def test_get_all_notes_delegates_to_injected_service():
    """``_get_all_notes_and_mind_maps`` calls the injected service."""
    core = ClientCoreCapabilities(MagicMock())
    fake_service = MagicMock(spec=_mind_map.MindMapService)
    fake_service.fetch_all_notes_and_mind_maps = AsyncMock(return_value=["sentinel-row"])

    api = NotesAPI(core, mind_map_service=fake_service)
    result = await api._get_all_notes_and_mind_maps("nb_abc")

    assert result == ["sentinel-row"]
    fake_service.fetch_all_notes_and_mind_maps.assert_awaited_once_with("nb_abc")


def test_default_mind_map_service_installed_when_not_injected():
    """``NotesAPI(core)`` installs a default ``MindMapService(core)``."""
    core = ClientCoreCapabilities(MagicMock())
    api = NotesAPI(core)
    assert isinstance(api._mind_map_service, _mind_map.MindMapService)


def test_mind_map_service_is_keyword_only():
    """``mind_map_service`` is keyword-only so the positional constructor
    contract (``core``) stays unchanged."""
    core = ClientCoreCapabilities(MagicMock())
    fake_service = MagicMock(spec=_mind_map.MindMapService)
    with pytest.raises(TypeError):
        # Passing mind_map_service positionally must fail because the
        # constructor declares it after ``*``.
        NotesAPI(core, fake_service)  # type: ignore[misc]


def test_falsy_mind_map_service_instance_is_preserved():
    """A caller-supplied falsy ``MindMapService`` instance is preserved.

    The ``is None`` defaulting idiom (vs. truthy-``or``) ensures that a
    caller passing a real but ``__bool__``-falsy service is not silently
    replaced by a fresh default. This mirrors the contract pinned for
    ``ArtifactsAPI`` after PR #774.
    """

    class _FalsyService(_mind_map.MindMapService):
        def __bool__(self) -> bool:
            return False

    core = ClientCoreCapabilities(MagicMock())
    falsy_service = _FalsyService(core)

    api = NotesAPI(core, mind_map_service=falsy_service)

    assert api._mind_map_service is falsy_service
