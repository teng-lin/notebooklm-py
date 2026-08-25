"""Tests for ``NoteBackedMindMapService`` injection into ``ArtifactsAPI``.

After Phase 5 (refactor-history.md Migration Plan steps 6-7), ``ArtifactsAPI``
took two explicit services through its constructor. P10 R1.1 retired the
second one — ``note_service`` — together with the deprecated ``rpc=`` input
and the partial ``WebRpcBackend`` it built, leaving:

* ``mind_maps: NoteBackedMindMapService`` — the mind-map-only adapter
  the download path uses (replaces the previous ``mind_map_service``
  parameter name); retired in P10 R4.2.
* ``_backend: BackendAdapter`` — the client-assembled semantic backend,
  now the only construction path.

These tests pin three contracts:

1. ``_list_mind_maps()`` delegates to the injected ``mind_maps``
   facade and does not re-enter the legacy module-level
   ``_mind_map.NoteBackedMindMapService.list_mind_maps`` adapter.
2. ``mind_maps`` is required and keyword-only — the legacy
   ``mind_map_service`` kwarg is gone, and so are ``rpc=``/``note_service=``.
3. Constructing without the new kwargs (or with a retired name) raises
   ``TypeError``.

``ArtifactsAPI`` consumes its runtime collaborators (``drain`` +
``lifecycle``) directly per ADR-0014 Rule 2; the tests here do not exercise
RPC traffic — they pin the constructor contract — so the collaborator stubs
only need to silently accept the calls ``ArtifactsAPI.__init__`` makes
(``drain.register_drain_hook``).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._mind_map import NoteBackedMindMapService
from notebooklm._note_service import NoteService
from tests._fixtures.web_backend import build_web_backend


def _make_collaborators() -> tuple[MagicMock, MagicMock, MagicMock]:
    """Return ``(rpc, drain, lifecycle)`` stubs for constructor-contract tests.

    ``rpc`` is the fake execution runtime the test wraps in a real
    ``WebRpcBackend`` through ``build_web_backend``; ``drain`` must accept
    ``register_drain_hook`` (called by :meth:`ArtifactsAPI.__init__` to
    register the polling-service close-time cleanup hook); the other
    collaborators are inert.
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

    api = ArtifactsAPI(
        drain=drain,
        lifecycle=lifecycle,
        notebooks=MagicMock(),
        mind_maps=fake_mind_maps,
        _backend=build_web_backend(rpc),
    )
    result = await api._list_mind_maps("nb_abc")

    assert result == ["sentinel-row"]
    fake_mind_maps.list_mind_maps.assert_awaited_once_with("nb_abc")


def test_mind_maps_is_required():
    """``mind_maps`` is required — no implicit fallback installs it."""
    _, drain, lifecycle = _make_collaborators()
    kw = {"drain": drain, "lifecycle": lifecycle, "notebooks": MagicMock()}

    with pytest.raises(TypeError):
        ArtifactsAPI(**kw)  # type: ignore[call-arg]


def test_retired_rpc_and_note_service_kwargs_are_rejected():
    """P10 R1.1 deleted ``rpc=`` and ``note_service=``; both must now raise.

    Silently accepting either would let a caller believe it still selects a
    construction path, when the composition root's ``_backend`` is the only
    one left.
    """
    rpc, drain, lifecycle = _make_collaborators()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    kw = {
        "drain": drain,
        "lifecycle": lifecycle,
        "notebooks": MagicMock(),
        "mind_maps": fake_mind_maps,
    }

    with pytest.raises(TypeError):
        ArtifactsAPI(**kw, rpc=rpc)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        ArtifactsAPI(**kw, note_service=MagicMock(spec=NoteService))  # type: ignore[call-arg]


def test_constructor_parameters_are_keyword_only():
    """All ``ArtifactsAPI`` parameters remain keyword-only."""
    _, drain, lifecycle = _make_collaborators()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    with pytest.raises(TypeError):
        ArtifactsAPI(drain, lifecycle, MagicMock(), fake_mind_maps)  # type: ignore[misc]


def test_legacy_mind_map_service_kwarg_is_rejected():
    """The Phase 3 ``mind_map_service=`` kwarg was renamed in Phase 5.

    Passing it must raise ``TypeError`` so silent breakage on partial
    upgrades surfaces immediately.
    """
    rpc, drain, lifecycle = _make_collaborators()
    fake_mind_maps = MagicMock(spec=NoteBackedMindMapService)
    with pytest.raises(TypeError):
        ArtifactsAPI(  # type: ignore[call-arg]
            drain=drain,
            lifecycle=lifecycle,
            notebooks=MagicMock(),
            mind_map_service=fake_mind_maps,
            _backend=build_web_backend(rpc),
        )


def test_artifacts_no_longer_exposes_core_property_alias():
    """Phase 5 removes the ``_core`` ``@property`` alias on ArtifactsAPI.

    The transitional ``_core`` shim added in Phase 3 is dead code; after
    the runtime-adapter inlining the runtime collaborators are stored on
    ``ArtifactsAPI`` directly as ``_drain`` / ``_lifecycle`` rather than
    behind a single ``_runtime`` attribute.
    """
    rpc, drain, lifecycle = _make_collaborators()
    api = ArtifactsAPI(
        drain=drain,
        lifecycle=lifecycle,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        _backend=build_web_backend(rpc),
    )
    # The descriptor must be gone — not just empty, not just delegating.
    assert not hasattr(api, "_core")
    assert not hasattr(api, "_rpc")
    assert api._backend is not None
    assert api._drain is drain
    assert api._lifecycle is lifecycle
