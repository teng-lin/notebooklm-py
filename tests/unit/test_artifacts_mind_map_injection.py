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
   ``_web.mind_maps.NoteBackedMindMapService.list_mind_maps`` adapter.
2. Both ``mind_maps`` and ``note_service`` are required and
   keyword-only — the legacy ``mind_map_service`` kwarg is gone.
3. Constructing without the new kwargs (or with the old name) raises
   ``TypeError``.

``WebArtifactsAPI`` consumes its Web and neutral collaborators directly per
ADR-0014 Rule 2; the tests here
do not exercise RPC traffic — they pin the constructor contract — so
the collaborator stubs only need to silently accept the calls
``ArtifactsAPI.__init__`` makes (``drain.register_drain_hook``).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._web.artifacts import WebArtifactsAPI
from notebooklm._web.mind_maps import NoteBackedMindMapService
from notebooklm._web.notes import NoteService


def _make_collaborators() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return ``(rpc, drain, lifecycle)`` stubs for constructor-contract tests.

    ``drain`` must accept ``register_drain_hook`` (called by
    :meth:`ArtifactsAPI.__init__` to register the polling-service
    close-time cleanup hook); the other collaborators are inert.
    """
    rpc = MagicMock()
    drain = MagicMock()
    lifecycle = MagicMock()
    return rpc, drain, lifecycle


@pytest.mark.asyncio
async def test_list_mind_maps_delegates_to_injected_facade():
    """``_list_mind_maps`` calls the injected ``mind_maps`` facade.

    Phase 6 (refactor-history.md Step 9, ADR-0013) removed the module-level
    ``_mind_map.list_mind_maps`` wrapper that previously needed to be
    monkeypatched as a guard; the only path now is through the
    injected adapter. Confirming the adapter sees the call still pins
    the contract.
    """
    rpc, drain, lifecycle = _make_collaborators()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_mind_maps.list_mind_maps = AsyncMock(return_value=["sentinel-row"])
    fake_note_service = MagicMock(spec=NoteService)

    api = WebArtifactsAPI(
        rpc=rpc,
        drain=drain,
        lifecycle=lifecycle,
        notebooks=MagicMock(),
        mind_maps=fake_mind_maps,
        note_service=fake_note_service,
    )
    result = await api._list_mind_maps("nb_abc")

    assert result == ["sentinel-row"]
    fake_mind_maps.list_mind_maps.assert_awaited_once_with("nb_abc")


def test_mind_maps_and_note_service_are_required():
    """Both new kwargs are required — no implicit fallback installs them."""
    rpc, drain, lifecycle = _make_collaborators()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_note_service = MagicMock(spec=NoteService)
    kw = {"rpc": rpc, "drain": drain, "lifecycle": lifecycle, "notebooks": MagicMock()}

    # Missing both.
    with pytest.raises(TypeError):
        WebArtifactsAPI(**kw)  # type: ignore[call-arg]

    # Missing note_service.
    with pytest.raises(TypeError):
        WebArtifactsAPI(**kw, mind_maps=fake_mind_maps)  # type: ignore[call-arg]

    # Missing mind_maps.
    with pytest.raises(TypeError):
        WebArtifactsAPI(**kw, note_service=fake_note_service)  # type: ignore[call-arg]


def test_mind_maps_and_note_service_are_keyword_only():
    """All ``ArtifactsAPI`` parameters remain keyword-only."""
    rpc, drain, lifecycle = _make_collaborators()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_note_service = MagicMock(spec=NoteService)
    with pytest.raises(TypeError):
        WebArtifactsAPI(rpc, drain, lifecycle, MagicMock(), fake_mind_maps, fake_note_service)  # type: ignore[misc]


def test_legacy_mind_map_service_kwarg_is_rejected():
    """The Phase 3 ``mind_map_service=`` kwarg was renamed in Phase 5.

    Passing it must raise ``TypeError`` so silent breakage on partial
    upgrades surfaces immediately.
    """
    rpc, drain, lifecycle = _make_collaborators()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    fake_note_service = MagicMock(spec=NoteService)
    with pytest.raises(TypeError):
        WebArtifactsAPI(  # type: ignore[call-arg]
            rpc=rpc,
            drain=drain,
            lifecycle=lifecycle,
            notebooks=MagicMock(),
            mind_map_service=fake_mind_maps,
            note_service=fake_note_service,
        )


def test_artifacts_no_longer_exposes_core_property_alias():
    """Phase 5 removes the ``_core`` ``@property`` alias on ArtifactsAPI.

    The transitional ``_core`` shim added in Phase 3 is dead code; after
    the runtime-adapter inlining the three runtime collaborators are
    stored on ``ArtifactsAPI`` directly as ``_rpc`` / ``_drain`` /
    ``_lifecycle`` rather than behind a single ``_runtime`` attribute.
    """
    rpc, drain, lifecycle = _make_collaborators()
    api = WebArtifactsAPI(
        rpc=rpc,
        drain=drain,
        lifecycle=lifecycle,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    # The descriptor must be gone — not just empty, not just delegating.
    assert not hasattr(api, "_core")
    assert api._rpc is rpc
    assert api._drain is drain
    assert api._lifecycle is lifecycle
