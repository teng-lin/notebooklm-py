"""Tests for ``MindMapService`` injection into ``ArtifactsAPI``.

``ArtifactsAPI`` and ``NotesAPI`` both depend on the mind-map service
through a constructor seam rather than the module-level
``_mind_map.list_mind_maps()`` wrapper. These tests pin three contracts:

1. ``_list_mind_maps()`` delegates to the injected service and does not
   re-enter the module-level ``_mind_map.list_mind_maps`` wrapper.
2. When no ``mind_map_service`` is injected, ``ArtifactsAPI`` installs a
   default ``MindMapService(core)``.
3. ``mind_map_service`` is keyword-only so the positional constructor
   contract stays unchanged.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm import _mind_map
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._capabilities import ClientCoreCapabilities


@pytest.mark.asyncio
async def test_list_mind_maps_delegates_to_injected_service(monkeypatch):
    """``_list_mind_maps`` calls the injected service and does not re-enter
    the module-level ``_mind_map.list_mind_maps`` wrapper."""
    core = ClientCoreCapabilities(MagicMock())
    fake_service = MagicMock(spec=_mind_map.MindMapService)
    fake_service.list_mind_maps = AsyncMock(return_value=["sentinel-row"])

    module_seam = AsyncMock(return_value=["should-not-see-this"])
    monkeypatch.setattr(_mind_map, "list_mind_maps", module_seam)

    api = ArtifactsAPI(core, mind_map_service=fake_service)
    result = await api._list_mind_maps("nb_abc")

    assert result == ["sentinel-row"]
    fake_service.list_mind_maps.assert_awaited_once_with("nb_abc")
    module_seam.assert_not_awaited()


def test_default_mind_map_service_installed_when_not_injected():
    """``ArtifactsAPI(core)`` installs a default ``MindMapService(core)``."""
    core = ClientCoreCapabilities(MagicMock())
    api = ArtifactsAPI(core)
    assert isinstance(api._mind_map_service, _mind_map.MindMapService)


def test_mind_map_service_is_keyword_only():
    """``mind_map_service`` is keyword-only so the positional constructor
    contract (``core, notes_api, storage_path``) stays unchanged."""
    core = ClientCoreCapabilities(MagicMock())
    fake_service = MagicMock(spec=_mind_map.MindMapService)
    with pytest.raises(TypeError):
        # Attempting to pass mind_map_service positionally must fail because
        # the constructor declares it after ``*``.
        ArtifactsAPI(core, None, None, fake_service)  # type: ignore[misc]
