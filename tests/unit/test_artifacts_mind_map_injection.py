"""Tests for ``NoteBackedMindMapService`` injection into ``ArtifactsAPI``.

After Phase 5 (refactor-history.md Migration Plan steps 6-7), ``ArtifactsAPI``
takes two explicit services through its constructor:

* ``mind_maps: NoteBackedMindMapService`` — the mind-map-only adapter
  the download path uses (replaces the previous ``mind_map_service``
  parameter name).
* ``note_service: NoteService`` — the raw note-row primitives the
  mind-map generation path uses to persist a freshly generated mind map.

These tests pin three contracts:

1. ``_list_mind_maps()`` delegates to the injected ``mind_maps``
   facade and does not re-enter the legacy module-level
   ``_mind_map.NoteBackedMindMapService.list_mind_maps`` adapter.
2. Both ``mind_maps`` and ``note_service`` are required and
   keyword-only — the legacy ``mind_map_service`` kwarg is gone.
3. Constructing without the new kwargs (or with the old name) raises
   ``TypeError``.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from _fixtures.fake_core import make_fake_core
from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteService


@pytest.mark.asyncio
async def test_list_mind_maps_delegates_to_injected_facade():
    """``_list_mind_maps`` calls the injected ``mind_maps`` facade.

    Phase 6 (refactor-history.md Step 9, ADR-013) removed the module-level
    ``_mind_map.list_mind_maps`` wrapper that previously needed to be
    monkeypatched as a guard; the only path now is through the
    injected adapter. Confirming the adapter sees the call still pins
    the contract.
    """
    core = make_fake_core()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_mind_maps.list_mind_maps = AsyncMock(return_value=["sentinel-row"])
    fake_note_service = MagicMock(spec=NoteService)

    api = ArtifactsAPI(
        core,
        notebooks=MagicMock(),
        mind_maps=fake_mind_maps,
        note_service=fake_note_service,
    )
    result = await api._list_mind_maps("nb_abc")

    assert result == ["sentinel-row"]
    fake_mind_maps.list_mind_maps.assert_awaited_once_with("nb_abc")


def test_mind_maps_and_note_service_are_required():
    """Both new kwargs are required — no implicit fallback installs them."""
    core = make_fake_core()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_note_service = MagicMock(spec=NoteService)

    # Missing both.
    with pytest.raises(TypeError):
        ArtifactsAPI(core, notebooks=MagicMock())  # type: ignore[call-arg]

    # Missing note_service.
    with pytest.raises(TypeError):
        ArtifactsAPI(  # type: ignore[call-arg]
            core,
            notebooks=MagicMock(),
            mind_maps=fake_mind_maps,
        )

    # Missing mind_maps.
    with pytest.raises(TypeError):
        ArtifactsAPI(  # type: ignore[call-arg]
            core,
            notebooks=MagicMock(),
            note_service=fake_note_service,
        )


def test_mind_maps_and_note_service_are_keyword_only():
    """All non-``runtime`` parameters remain keyword-only."""
    core = make_fake_core()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_note_service = MagicMock(spec=NoteService)
    with pytest.raises(TypeError):
        ArtifactsAPI(core, MagicMock(), fake_mind_maps, fake_note_service)  # type: ignore[misc]


def test_legacy_mind_map_service_kwarg_is_rejected():
    """The Phase 3 ``mind_map_service=`` kwarg was renamed in Phase 5.

    Passing it must raise ``TypeError`` so silent breakage on partial
    upgrades surfaces immediately.
    """
    core = make_fake_core()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_note_service = MagicMock(spec=NoteService)
    with pytest.raises(TypeError):
        ArtifactsAPI(  # type: ignore[call-arg]
            core,
            notebooks=MagicMock(),
            mind_map_service=fake_mind_maps,
            note_service=fake_note_service,
        )


def test_artifacts_no_longer_exposes_core_property_alias():
    """Phase 5 removes the ``_core`` ``@property`` alias on ArtifactsAPI.

    After this PR ``ArtifactsAPI._runtime`` is the only attribute the
    helper modules read; the transitional ``_core`` shim added in Phase 3
    is dead code.
    """
    core = make_fake_core()
    api = ArtifactsAPI(
        core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    # The descriptor must be gone — not just empty, not just delegating.
    assert not hasattr(api, "_core")
    assert api._runtime is core
