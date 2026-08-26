"""Constructor-contract tests for ``ArtifactsAPI``.

After Phase 5 (refactor-history.md Migration Plan steps 6-7), ``ArtifactsAPI``
took explicit services through its constructor. P10 R1.1 retired ``rpc=`` and
``note_service=`` together with the partial ``WebRpcBackend`` they built, and
P10 R4.2 retired the last one — ``mind_maps=``, the note-backed adapter the
download path used — when the mind-map workflows moved above the semantic port
and its class was deleted. What is left is:

* ``_backend: BackendAdapter`` — the client-assembled semantic backend, now
  the only construction path.

These tests pin two contracts:

1. every retired kwarg (``rpc=``, ``note_service=``, ``mind_maps=`` and the
   Phase 3 ``mind_map_service=``) raises ``TypeError`` rather than being
   silently accepted, so a caller can never believe it still selects a
   construction path;
2. the surviving parameters stay keyword-only, and the retired ``_core`` /
   ``_rpc`` attribute aliases stay gone.

``ArtifactsAPI`` consumes its runtime collaborators (``drain`` +
``lifecycle``) directly per ADR-0014 Rule 2; the tests here do not exercise
RPC traffic — they pin the constructor contract — so the collaborator stubs
only need to silently accept the calls ``ArtifactsAPI.__init__`` makes
(``drain.register_drain_hook``).
"""

from unittest.mock import MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
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


def test_retired_service_kwargs_are_rejected():
    """``rpc=``, ``note_service=`` and ``mind_maps=`` must all raise.

    Silently accepting any of them would let a caller believe it still selects
    a construction path or supplies a collaborator, when the composition
    root's ``_backend`` is the only one left.
    """
    rpc, drain, lifecycle = _make_collaborators()
    kw = {"drain": drain, "lifecycle": lifecycle, "notebooks": MagicMock()}

    with pytest.raises(TypeError):
        ArtifactsAPI(**kw, rpc=rpc)  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        ArtifactsAPI(**kw, note_service=MagicMock(spec=NoteService))  # type: ignore[call-arg]

    with pytest.raises(TypeError):
        ArtifactsAPI(**kw, mind_maps=MagicMock())  # type: ignore[call-arg]


def test_constructor_parameters_are_keyword_only():
    """All ``ArtifactsAPI`` parameters remain keyword-only."""
    _, drain, lifecycle = _make_collaborators()
    with pytest.raises(TypeError):
        ArtifactsAPI(drain, lifecycle, MagicMock())  # type: ignore[misc]


def test_legacy_mind_map_service_kwarg_is_rejected():
    """The Phase 3 ``mind_map_service=`` kwarg was renamed in Phase 5.

    Passing it must raise ``TypeError`` so silent breakage on partial
    upgrades surfaces immediately.
    """
    rpc, drain, lifecycle = _make_collaborators()
    with pytest.raises(TypeError):
        ArtifactsAPI(  # type: ignore[call-arg]
            drain=drain,
            lifecycle=lifecycle,
            notebooks=MagicMock(),
            mind_map_service=MagicMock(),
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
        _backend=build_web_backend(rpc),
    )
    # The descriptor must be gone — not just empty, not just delegating.
    assert not hasattr(api, "_core")
    assert not hasattr(api, "_rpc")
    assert not hasattr(api, "_mind_maps")
    assert api._backend is not None
    assert api._drain is drain
    assert api._lifecycle is lifecycle
