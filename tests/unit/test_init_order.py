"""Init-order / construction regression tests for ``ArtifactsAPI`` / ``NotesAPI``.

Before the fix, :class:`ArtifactsAPI` required ``notes_api=client.notes`` at
construction time, so :class:`NotesAPI` had to be built first. The shared
:mod:`_mind_map` module decouples the two APIs — these tests pin that
invariant down so the load-bearing init order can't silently come back.

The static AST reach-in / runtime-import boundary *gates* live in
``tests/_guardrails/test_no_facade_reach_in.py``. What remains here are the
tests that *exercise* the wired client (constructor-DI seams, seam wiring,
mind-map flows). The two boundary tests that still inspect production source
with the AST visitors import those helpers from the shared non-test module
``tests/_guardrails/_ast_reach_in.py`` (issue #1431) — the same module the
gate file imports them from — so neither test imports from the other.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._artifacts import ArtifactsAPI
from notebooklm._notes import NotesAPI
from notebooklm.auth import AuthTokens
from notebooklm.client import NotebookLMClient
from tests._fixtures.fake_core import FakeSession, make_fake_core
from tests._guardrails._ast_reach_in import (
    _assignment_value,
    _call_keyword_value,
    _facade_construction_lines,
    _module_function_body,
    _owned_attr_assignment,
    _owned_attr_name,
    _RuntimeImportVisitor,
)
from tests._helpers.client_factory import build_client_shell_for_tests

pytestmark = pytest.mark.repo_lint

SRC_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm"


# ---------------------------------------------------------------------------
# Constructor-DI seams (``docs/improvement.md`` §4.1 + §4.2)
#
# These pin tests guard the post-refactor wiring shape so a future
# refactor cannot silently re-introduce the retired module-level
# late-binding wrappers (``_decode_response_late_bound``,
# ``_sleep_late_bound``, ``_live_is_auth_error``) or the retired
# ``Kernel.http_client`` setter.
# ---------------------------------------------------------------------------


def test_compose_client_internals_exposes_constructor_di_seams() -> None:
    """``compose_client_internals`` MUST expose the four constructor-DI seams.

    Stage B1 PR 2 of the post-refactoring plan moved the composition
    root out of ``Session.__init__`` into
    ``notebooklm._runtime.init.compose_client_internals``. The seams live
    on the helper (and on the canonical test builder
    ``build_client_shell_for_tests``), NOT on ``NotebookLMClient.__init__``
    (which preserves the production surface).

    The seams replace the retired module-level late-binding wrappers
    (see ``docs/improvement.md`` §4.1) and the retired
    ``Kernel.http_client`` setter (§4.2). Each must be keyword-only and
    default to ``None`` so the helper can resolve the canonical seam via
    a fresh module-attribute lookup at construction time (preserving
    pre-construction monkeypatch propagation).
    """
    import inspect

    from notebooklm._runtime.init import compose_client_internals

    sig = inspect.signature(compose_client_internals)
    for name in ("decode_response", "sleep", "is_auth_error", "async_client_factory"):
        assert name in sig.parameters, (
            f"compose_client_internals must expose constructor-DI kwarg {name!r}"
        )
        param = sig.parameters[name]
        assert param.kind == inspect.Parameter.KEYWORD_ONLY, (
            f"{name!r} must be keyword-only; got {param.kind!r}"
        )
        assert param.default is None, (
            f"{name!r} must default to None (None-sentinel + fresh module "
            f"lookup); got default {param.default!r}"
        )


def test_session_wires_seam_attributes_for_executor_and_chain() -> None:
    """Constructor-injected seams MUST reach the executor and chain builder.

    The ``RpcExecutor`` resolves ``decode_response`` / ``is_auth_error`` /
    ``sleep`` through closures over ``ClientSeams`` etc., so that
    tests which rebind ``session._seams.decode_response = stub`` after
    ``NotebookLMClient.__init__`` (which eagerly builds the executor via
    the ``rpc_executor`` accessor wired into sub-APIs in Wave 7) still take
    effect. This test pins both halves: constructor-injected callables
    reach the executor, AND post-construction rebinds also take effect.
    """
    from notebooklm.auth import AuthTokens

    auth = AuthTokens(
        cookies={"SID": "x"},
        csrf_token="csrf",
        session_id="sid",
    )

    def custom_decode(*_a, **_kw):
        return ["custom"]

    async def custom_sleep(_seconds):
        return None

    def custom_is_auth_error(_exc):
        return True

    core = build_client_shell_for_tests(
        auth,
        decode_response=custom_decode,
        sleep=custom_sleep,
        is_auth_error=custom_is_auth_error,
    )

    assert core._seams.decode_response is custom_decode
    assert core._seams.sleep is custom_sleep
    assert core._seams.is_auth_error is custom_is_auth_error

    executor = core._rpc_executor
    # Constructor-injected callables propagate through the closure.
    assert executor._decode_response() == ["custom"]
    assert executor._is_auth_error(object()) is True

    # Post-construction rebind takes effect (late-binding contract).
    def rebound_decode(*_a, **_kw):
        return ["rebound"]

    core._seams.decode_response = rebound_decode
    assert executor._decode_response() == ["rebound"]


def test_kernel_http_client_is_read_only_property() -> None:
    """``Kernel.http_client`` MUST have no setter (``docs/improvement.md`` §4.2)."""
    from notebooklm._kernel import Kernel

    descriptor = Kernel.__dict__["http_client"]
    assert isinstance(descriptor, property)
    assert descriptor.fset is None, (
        "Kernel.http_client must remain read-only; the retired setter was a "
        "test-injection seam that constructor-time async_client_factory "
        "injection now replaces (see docs/improvement.md §4.2)."
    )


def test_phase8_source_listing_service_name_and_facade_wiring_are_current() -> None:
    """Downstream notebook-metadata work depends on the finalized lister name."""
    from notebooklm._source.listing import SourceLister
    from notebooklm._sources import SourcesAPI

    core = MagicMock()
    api = SourcesAPI(core, uploader=MagicMock())

    assert isinstance(api._lister, SourceLister)


def test_phase7_artifact_download_patch_seams_are_current() -> None:
    """Artifact downloads must use canonical helpers and collaborators.

    Phase 5 (refactor-history.md Migration Plan steps 6-7) moves the mind-map
    create/list/extract paths off the ``_mind_map`` module-level seams
    and onto the injected ``NoteService`` + ``NoteBackedMindMapService``
    instances. Downloads should now import their canonical helpers directly
    rather than resolving through ``notebooklm._artifacts`` at runtime or in
    type-checking-only imports.
    """
    import notebooklm._artifact.downloads as artifact_downloads
    import notebooklm._artifact.formatters as artifact_formatters
    import notebooklm._artifacts as artifacts
    import notebooklm._mind_map as mind_map
    import notebooklm.auth as auth

    tree = ast.parse((SRC_ROOT / "_artifact" / "downloads.py").read_text(encoding="utf-8"))
    artifact_facade_imports: list[str] = []
    artifact_facade_modules = {"_artifacts", "notebooklm._artifacts"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            artifact_facade_imports.extend(
                alias.name for alias in node.names if alias.name in artifact_facade_modules
            )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in artifact_facade_modules:
                artifact_facade_imports.extend(f"{module}.{alias.name}" for alias in node.names)
            elif module in {"", "notebooklm"}:
                artifact_facade_imports.extend(
                    alias.name for alias in node.names if alias.name == "_artifacts"
                )

    assert artifact_facade_imports == []
    assert artifacts._mind_map is mind_map
    assert not hasattr(artifact_downloads, "_artifact_seams")
    assert artifact_downloads.load_httpx_cookies is auth.load_httpx_cookies
    assert artifact_downloads._extract_app_data is artifact_formatters._extract_app_data
    assert (
        artifact_downloads._format_interactive_content
        is artifact_formatters._format_interactive_content
    )
    assert artifact_downloads._parse_data_table is artifact_formatters._parse_data_table


def test_notebooks_api_has_no_hidden_sources_api_runtime_dependency() -> None:
    """Notebook metadata must use a narrow lister, not hidden SourcesAPI construction."""
    notebooks_tree = ast.parse((SRC_ROOT / "_notebooks.py").read_text(encoding="utf-8"))
    visitor = _RuntimeImportVisitor(
        forbidden_names={"SourcesAPI"},
        forbidden_modules={"_sources", "notebooklm._sources"},
    )
    visitor.visit(notebooks_tree)

    assert visitor.forbidden == []
    assert _facade_construction_lines(notebooks_tree, {"SourcesAPI"}) == {}

    metadata_tree = ast.parse((SRC_ROOT / "_notebook_metadata.py").read_text(encoding="utf-8"))
    metadata_visitor = _RuntimeImportVisitor(
        forbidden_names={"SourcesAPI"},
        forbidden_modules={"_sources", "notebooklm._sources"},
    )
    metadata_visitor.visit(metadata_tree)

    assert metadata_visitor.forbidden == []
    assert _facade_construction_lines(metadata_tree, {"SourcesAPI"}) == {}


def test_client_constructs_sources_before_notebooks_and_injects_sources_api() -> None:
    """Client wiring must avoid hidden SourcesAPI construction inside NotebooksAPI.

    The wiring lives in :func:`notebooklm._client_assembly._assemble_client`
    (the single construction seam ``NotebookLMClient.__init__`` and the
    canonical test factory both run), where the client instance is bound to
    the ``client`` parameter — hence the ``owner="client"`` matchers.
    """
    assembly_tree = ast.parse((SRC_ROOT / "_client_assembly.py").read_text(encoding="utf-8"))
    assembly_body = _module_function_body(assembly_tree, "_assemble_client")
    sources_index, sources_assignment = _owned_attr_assignment(
        assembly_body, "sources", owner="client"
    )
    notebooks_index, notebook_assignment = _owned_attr_assignment(
        assembly_body, "notebooks", owner="client"
    )

    assert sources_index < notebooks_index

    sources_value = _assignment_value(sources_assignment)
    assert isinstance(sources_value, ast.Call)
    assert isinstance(sources_value.func, ast.Name)
    assert sources_value.func.id == "SourcesAPI"

    notebooks_value = _assignment_value(notebook_assignment)
    assert isinstance(notebooks_value, ast.Call)
    notebooks_call = notebooks_value
    assert isinstance(notebooks_call.func, ast.Name)
    assert notebooks_call.func.id == "NotebooksAPI"

    assert (
        _owned_attr_name(_call_keyword_value(notebooks_call, "sources_api"), owner="client")
        == "sources"
    )


@pytest.fixture
def mock_auth() -> AuthTokens:
    return AuthTokens(
        cookies={"SID": "test"},
        csrf_token="csrf",
        session_id="session",
    )


def test_client_exposes_artifacts_and_notes(mock_auth: AuthTokens) -> None:
    """The client should construct both APIs regardless of order."""
    client = NotebookLMClient(mock_auth)
    assert isinstance(client.artifacts, ArtifactsAPI)
    assert isinstance(client.notes, NotesAPI)


def test_artifacts_constructible_without_notes_api(mock_auth: AuthTokens) -> None:
    """``ArtifactsAPI`` no longer takes ``notes_api`` at all (per
    docs/refactor-history.md Step 4) — the parameter was removed in favor of
    explicit ``mind_maps`` + ``note_service`` (Phase 5). The mind-map
    decoupling is now structural."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = MagicMock()
    api = ArtifactsAPI(
        rpc=core,
        drain=core,
        lifecycle=core,
        notebooks=MagicMock(),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
        note_service=MagicMock(spec=NoteService),
    )
    assert api is not None
    # The legacy private attribute must not leak back: code that depends on
    # ``self._notes`` would re-introduce the coupling.
    assert not hasattr(api, "_notes")


def test_artifacts_rejects_legacy_notes_api_kwarg(mock_auth: AuthTokens) -> None:
    """The legacy ``notes_api=`` kwarg was removed in Phase 3
    (docs/refactor-history.md Step 4). Passing it must raise ``TypeError``."""
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = MagicMock()
    notes = NotesAPI(
        notes=MagicMock(spec=NoteService),
        mind_maps=MagicMock(spec=NoteBackedMindMapService),
    )
    with pytest.raises(TypeError):
        ArtifactsAPI(  # type: ignore[call-arg]
            core,
            notes_api=notes,
            notebooks=MagicMock(),
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
            note_service=MagicMock(spec=NoteService),
        )


def test_artifacts_before_notes_construction_order(mock_auth: AuthTokens) -> None:
    """Both construction orders must succeed and produce working APIs.

    ``ArtifactsAPI`` and ``NotesAPI`` have no construction-order
    dependency on each other; this test pins that building either one
    first still yields working APIs.
    """
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    core = MagicMock()

    def _make_artifacts() -> ArtifactsAPI:
        return ArtifactsAPI(
            rpc=core,
            drain=core,
            lifecycle=core,
            notebooks=MagicMock(),
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
            note_service=MagicMock(spec=NoteService),
        )

    def _make_notes() -> NotesAPI:
        return NotesAPI(
            notes=MagicMock(spec=NoteService),
            mind_maps=MagicMock(spec=NoteBackedMindMapService),
        )

    artifacts_first = _make_artifacts()
    notes_first = _make_notes()
    # Build in the opposite order too, just to make the symmetry explicit.
    notes_then = _make_notes()
    artifacts_then = _make_artifacts()
    assert artifacts_first is not None
    assert notes_first is not None
    assert artifacts_then is not None
    assert notes_then is not None


# ---------------------------------------------------------------------------
# Mind-map regression — ``generate_mind_map`` + ``list`` + ``download_mind_map``
# must keep working without an explicit ``NotesAPI`` injection.
# ---------------------------------------------------------------------------


def _make_core_for_mind_map_flow() -> tuple[FakeSession, list[tuple[Any, Any]]]:
    """Build a :class:`FakeSession` core whose ``rpc_call`` returns canned
    mind-map responses keyed on the RPC method.

    The core is built via ``make_fake_core(rpc_call=AsyncMock(...))`` — the
    sanctioned constructor-injection substrate (ADR-0007). The factory wires
    the injected mock onto ``fake.rpc_executor.rpc_call`` (the ``RpcCaller``
    surface the mind-map flow threads into ``ArtifactsAPI``) and supplies
    benign defaults for the ``assert_bound_loop`` / ``operation_scope`` /
    ``register_drain_hook`` surfaces the artifacts runtime touches.

    Returns ``(core, calls)`` where ``calls`` is a list of ``(method, params)``
    tuples populated as the test exercises the API.
    """
    calls: list[tuple[Any, Any]] = []

    mind_map_payload = {
        "name": "Mind Map Title",
        "children": [{"name": "child"}],
    }
    mind_map_json = json.dumps(mind_map_payload)

    async def fake_rpc_call(method: Any, params: Any, **_: Any) -> Any:
        calls.append((method, params))
        name = getattr(method, "name", str(method))
        if name == "GENERATE_MIND_MAP":
            return [[mind_map_json]]
        if name == "CREATE_NOTE":
            return [["note_abc"]]
        if name == "UPDATE_NOTE":
            return None
        if name == "GET_NOTES_AND_MIND_MAPS":
            return [
                [
                    [
                        "note_abc",
                        ["note_abc", mind_map_json, [], None, "Mind Map Title"],
                    ]
                ]
            ]
        if name == "LIST_ARTIFACTS":
            return [[]]
        return None

    core = make_fake_core(rpc_call=AsyncMock(side_effect=fake_rpc_call))
    return core, calls


def _build_artifacts_with_real_mind_map_service(core: FakeSession) -> ArtifactsAPI:
    """Build an ``ArtifactsAPI`` whose mind-map services are real
    instances backed by ``core.rpc_executor`` so the mind-map flow
    exercises the live RPC callbacks against the canned executor.
    """
    from notebooklm._mind_map import NoteBackedMindMapService
    from notebooklm._note_service import NoteService

    note_service = NoteService(core.rpc_executor)
    mind_maps = NoteBackedMindMapService(note_service)
    return ArtifactsAPI(
        rpc=core.rpc_executor,
        drain=core,
        lifecycle=core,
        notebooks=MagicMock(get_source_ids=AsyncMock(return_value=["src_1"])),
        mind_maps=mind_maps,
        note_service=note_service,
    )


@pytest.mark.asyncio
async def test_generate_mind_map_works_without_notes_injection() -> None:
    """``generate_mind_map`` must persist the mind map via ``_mind_map``
    primitives, not via an injected ``NotesAPI``."""
    core, calls = _make_core_for_mind_map_flow()
    api = _build_artifacts_with_real_mind_map_service(core)

    result = await api.generate_mind_map("nb_123", source_ids=["src_1"])

    assert result.note_id == "note_abc"
    assert result.mind_map["name"] == "Mind Map Title"

    # The constructor-injected RPC mock was actually exercised (ADR-0007
    # Form-1 bite-check: the injected collaborator is reached, not silently
    # bypassed by a stale auto-vivified attribute).
    core.rpc_executor.rpc_call.assert_awaited()

    # The flow must have gone GENERATE_MIND_MAP -> CREATE_NOTE -> UPDATE_NOTE
    method_names = [getattr(m, "name", str(m)) for m, _ in calls]
    assert "GENERATE_MIND_MAP" in method_names
    assert "CREATE_NOTE" in method_names
    assert "UPDATE_NOTE" in method_names


@pytest.mark.asyncio
async def test_artifacts_list_pulls_mind_maps_without_notes_injection(
    tmp_path: Any,
) -> None:
    """``ArtifactsAPI.list`` must read mind maps through ``_mind_map`` —
    no ``NotesAPI`` reference required."""
    core, _ = _make_core_for_mind_map_flow()
    api = _build_artifacts_with_real_mind_map_service(core)

    artifacts = await api.list("nb_123")
    # One mind map should surface from GET_NOTES_AND_MIND_MAPS.
    assert any(a.kind.name == "MIND_MAP" for a in artifacts)


@pytest.mark.asyncio
async def test_download_mind_map_works_without_notes_injection(
    tmp_path: Any,
) -> None:
    """``download_mind_map`` reaches into mind-map storage via ``_mind_map``
    rather than ``self._notes``."""
    core, _ = _make_core_for_mind_map_flow()
    api = _build_artifacts_with_real_mind_map_service(core)

    output = tmp_path / "mm.json"
    returned = await api.download_mind_map("nb_123", str(output))

    assert returned == str(output)
    saved = json.loads(output.read_text(encoding="utf-8"))
    assert saved["name"] == "Mind Map Title"
