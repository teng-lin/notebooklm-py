"""Tests for ``MindMapService`` injection into ``ArtifactsAPI``.

``ArtifactsAPI`` and ``NotesAPI`` both depend on the mind-map service
through a constructor seam rather than the module-level
``_mind_map.list_mind_maps()`` wrapper. These tests pin two contracts:

1. ``_list_mind_maps()`` delegates to the injected service and does not
   re-enter the module-level ``_mind_map.list_mind_maps`` wrapper.
2. ``mind_map_service`` is required and keyword-only — the previous
   ``MindMapService(session)`` fallback was removed in
   docs/refactor.md Step 4.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from _fixtures.fake_core import make_fake_core
from notebooklm import _mind_map
from notebooklm._artifacts import ArtifactsAPI


@pytest.mark.asyncio
async def test_list_mind_maps_delegates_to_injected_service(monkeypatch):
    """``_list_mind_maps`` calls the injected service and does not re-enter
    the module-level ``_mind_map.list_mind_maps`` wrapper."""
    core = make_fake_core()
    fake_service = MagicMock(spec=_mind_map.MindMapService)
    fake_service.list_mind_maps = AsyncMock(return_value=["sentinel-row"])

    module_seam = AsyncMock(return_value=["should-not-see-this"])
    monkeypatch.setattr(_mind_map, "list_mind_maps", module_seam)

    api = ArtifactsAPI(
        core,
        notebooks=MagicMock(),
        mind_map_service=fake_service,
    )
    result = await api._list_mind_maps("nb_abc")

    assert result == ["sentinel-row"]
    fake_service.list_mind_maps.assert_awaited_once_with("nb_abc")
    module_seam.assert_not_awaited()


def test_mind_map_service_is_required():
    """``mind_map_service`` is required — no implicit default is installed.

    Phase 3 (docs/refactor.md Step 4) removed the
    ``MindMapService(session)`` fallback so the construction site at
    ``NotebookLMClient`` must wire it explicitly.
    """
    core = make_fake_core()
    with pytest.raises(TypeError):
        ArtifactsAPI(core, notebooks=MagicMock())  # type: ignore[call-arg]


def test_mind_map_service_is_keyword_only():
    """``mind_map_service`` is keyword-only — passing positionally fails."""
    core = make_fake_core()
    fake_service = MagicMock(spec=_mind_map.MindMapService)
    with pytest.raises(TypeError):
        # All non-``runtime`` parameters are keyword-only.
        ArtifactsAPI(core, MagicMock(), fake_service)  # type: ignore[misc]
