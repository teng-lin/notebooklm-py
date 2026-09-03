"""Keep every Android call-site retry literal aligned with web policy semantics."""

from __future__ import annotations

import ast
import importlib.util
from dataclasses import dataclass
from pathlib import Path

import pytest

from notebooklm._android.retry_policy import ANDROID_RETRY_MANIFEST, replay_safe_for
from notebooklm._web.policy import IDEMPOTENCY_REGISTRY, IdempotencyPolicy
from notebooklm.rpc import RPCMethod

pytestmark = pytest.mark.repo_lint

_ANDROID_ROOT = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_android"
_FORCE_NO_RETRY = {
    IdempotencyPolicy.PROBE_THEN_CREATE,
    IdempotencyPolicy.NON_IDEMPOTENT_NO_RETRY,
}


@dataclass(frozen=True)
class _WebRetryClass:
    method: RPCMethod
    mutation_or_inference: bool = False


# One row per concrete Android method in ANDROID_RETRY_MANIFEST. The web method
# identifies the same public operation; mutation_or_inference applies the plan's
# stricter Android rule that writes and inference kickoffs are never replayed,
# even where the web transport can safely retry an idempotent set operation.
_WEB_CLASS_BY_ANDROID_NAME: dict[str, _WebRetryClass] = {
    "ActOnSources": _WebRetryClass(RPCMethod.GENERATE_MIND_MAP, True),
    "AddSources": _WebRetryClass(RPCMethod.ADD_SOURCE, True),
    "AddSourcesAsync": _WebRetryClass(RPCMethod.ADD_SOURCES_ASYNC, True),
    "AddTentativeSources": _WebRetryClass(RPCMethod.ADD_SOURCE, True),
    "AppendSource": _WebRetryClass(RPCMethod.APPEND_SOURCE, True),
    "CancelDiscoverSourcesJob": _WebRetryClass(RPCMethod.CANCEL_RESEARCH, True),
    # CancelGeneration is the one replay-neutral control operation already
    # proven safe by both backends; it retains the existing replay class.
    "CancelGeneration": _WebRetryClass(RPCMethod.CANCEL_GENERATION),
    "CheckSourceFreshness": _WebRetryClass(RPCMethod.CHECK_SOURCE_FRESHNESS),
    "CopyArtifactsAsync": _WebRetryClass(RPCMethod.COPY_ARTIFACTS, True),
    "CopyProject": _WebRetryClass(RPCMethod.COPY_NOTEBOOK, True),
    "CopySourcesAsync": _WebRetryClass(RPCMethod.COPY_SOURCES, True),
    "CreateArtifact": _WebRetryClass(RPCMethod.CREATE_ARTIFACT, True),
    "CreateLabel": _WebRetryClass(RPCMethod.CREATE_LABEL, True),
    "CreateNote": _WebRetryClass(RPCMethod.CREATE_NOTE, True),
    "CreateProject": _WebRetryClass(RPCMethod.CREATE_NOTEBOOK, True),
    "DeleteArtifact": _WebRetryClass(RPCMethod.DELETE_ARTIFACT, True),
    "DeleteChatTurns": _WebRetryClass(RPCMethod.DELETE_CONVERSATION, True),
    "DeleteLabels": _WebRetryClass(RPCMethod.DELETE_LABEL, True),
    "DeleteNotes": _WebRetryClass(RPCMethod.DELETE_NOTE, True),
    "DeleteProjects": _WebRetryClass(RPCMethod.DELETE_NOTEBOOK, True),
    "DeleteSources": _WebRetryClass(RPCMethod.DELETE_SOURCE, True),
    "DeriveArtifact": _WebRetryClass(RPCMethod.REVISE_SLIDE, True),
    "DiscoverSources": _WebRetryClass(RPCMethod.DISCOVER_SOURCES, True),
    "DiscoverSourcesAsync": _WebRetryClass(RPCMethod.START_DEEP_RESEARCH, True),
    "DiscoverSourcesManifold": _WebRetryClass(RPCMethod.START_FAST_RESEARCH, True),
    "ExportToDrive": _WebRetryClass(RPCMethod.EXPORT_ARTIFACT, True),
    "FinishDiscoverSourcesRun": _WebRetryClass(RPCMethod.IMPORT_RESEARCH, True),
    "GenerateArtifact": _WebRetryClass(RPCMethod.RETRY_ARTIFACT, True),
    "GenerateDocumentGuides": _WebRetryClass(RPCMethod.GET_SOURCE_GUIDE),
    "GenerateFreeFormStreamed": _WebRetryClass(RPCMethod.SUMMARIZE, True),
    "GenerateNotebookGuide": _WebRetryClass(RPCMethod.SUMMARIZE, True),
    "GeneratePromptSuggestions": _WebRetryClass(RPCMethod.SUGGEST_PROMPTS),
    "GenerateReportSuggestions": _WebRetryClass(RPCMethod.GET_SUGGESTED_REPORTS),
    "GetArtifact": _WebRetryClass(RPCMethod.LIST_ARTIFACTS),
    "GetArtifactCustomizationChoices": _WebRetryClass(RPCMethod.GET_CUSTOMIZATION_CHOICES),
    "GetChatSessionStatus": _WebRetryClass(RPCMethod.GET_CHAT_SESSION_STATUS),
    "GetLabels": _WebRetryClass(RPCMethod.LIST_LABELS),
    "GetNotes": _WebRetryClass(RPCMethod.GET_NOTES_AND_MIND_MAPS),
    "GetOrCreateAccount": _WebRetryClass(RPCMethod.GET_USER_SETTINGS, True),
    "GetProject": _WebRetryClass(RPCMethod.GET_NOTEBOOK),
    "ListArtifacts": _WebRetryClass(RPCMethod.LIST_ARTIFACTS),
    "ListChatSessions": _WebRetryClass(RPCMethod.GET_LAST_CONVERSATION_ID),
    "ListChatTurns": _WebRetryClass(RPCMethod.GET_CONVERSATION_TURNS),
    "ListDiscoverSourcesJob": _WebRetryClass(RPCMethod.POLL_RESEARCH),
    "ListExpertIntelligenceContent": _WebRetryClass(RPCMethod.LIST_EXPERT_INTELLIGENCE_CONTENT),
    "ListRecentlyViewedProjects": _WebRetryClass(RPCMethod.LIST_NOTEBOOKS),
    "LoadSource": _WebRetryClass(RPCMethod.GET_SOURCE),
    "MutateAccount": _WebRetryClass(RPCMethod.SET_USER_SETTINGS, True),
    "MutateLabel": _WebRetryClass(RPCMethod.UPDATE_LABEL, True),
    "MutateNote": _WebRetryClass(RPCMethod.UPDATE_NOTE, True),
    "MutateProject": _WebRetryClass(RPCMethod.RENAME_NOTEBOOK, True),
    "MutateSource": _WebRetryClass(RPCMethod.UPDATE_SOURCE, True),
    "NextStepSuggestions": _WebRetryClass(RPCMethod.SUGGEST_NEXT_STEPS),
    "RefreshSource": _WebRetryClass(RPCMethod.REFRESH_SOURCE, True),
    "RemoveRecentlyViewedProject": _WebRetryClass(RPCMethod.REMOVE_RECENTLY_VIEWED, True),
    "RetrieveRelevantChunks": _WebRetryClass(RPCMethod.RETRIEVE_RELEVANT_CHUNKS),
    "ShareProject": _WebRetryClass(RPCMethod.SHARE_NOTEBOOK, True),
    "UpdateArtifact": _WebRetryClass(RPCMethod.RENAME_ARTIFACT, True),
    "GetProjectDetails": _WebRetryClass(RPCMethod.GET_SHARE_STATUS),
}


def _expected_retry_class(row: _WebRetryClass) -> bool:
    policy = IDEMPOTENCY_REGISTRY.get_entry(row.method).policy
    return not row.mutation_or_inference and policy not in _FORCE_NO_RETRY


def _string_value(node: ast.expr, names: dict[str, str]) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        return names.get(node.id)
    if isinstance(node, ast.JoinedStr):
        parts: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                resolved = _string_value(value.value, names)
                if resolved is None:
                    return None
                parts.append(resolved)
            else:
                return None
        return "".join(parts)
    return None


def _module_strings(tree: ast.Module, imported: dict[str, str] | None = None) -> dict[str, str]:
    names = dict(imported or {})
    pending = list(tree.body)
    for _ in range(len(pending) + 1):
        changed = False
        for node in pending:
            target: ast.expr | None = None
            value: ast.expr | None = None
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign):
                target, value = node.target, node.value
            if not isinstance(target, ast.Name) or value is None:
                continue
            resolved = _string_value(value, names)
            if resolved is not None and names.get(target.id) != resolved:
                names[target.id] = resolved
                changed = True
        if not changed:
            break
    return names


def _module_identity(path: Path) -> tuple[str, str]:
    relative = path.relative_to(_ANDROID_ROOT).with_suffix("")
    parts = ["notebooklm", "_android", *relative.parts]
    if parts[-1] == "__init__":
        parts.pop()
        module = ".".join(parts)
        return module, module
    module = ".".join(parts)
    return module, module.rpartition(".")[0]


def _android_module_strings(
    trees: dict[Path, ast.Module],
) -> dict[Path, dict[str, str]]:
    """Resolve local and imported string constants across Android modules."""

    identities = {path: _module_identity(path) for path in trees}
    path_by_module = {module: path for path, (module, _) in identities.items()}
    resolved = {path: _module_strings(tree) for path, tree in trees.items()}

    for _ in range(len(trees) + 1):
        changed = False
        for path, tree in trees.items():
            imported: dict[str, str] = {}
            package = identities[path][1]
            for node in tree.body:
                if not isinstance(node, ast.ImportFrom) or node.module is None:
                    continue
                import_name = f"{'.' * node.level}{node.module}" if node.level else node.module
                target_module = (
                    importlib.util.resolve_name(import_name, package) if node.level else import_name
                )
                target_path = path_by_module.get(target_module)
                if target_path is None:
                    continue
                for alias in node.names:
                    value = resolved[target_path].get(alias.name)
                    if value is not None:
                        imported[alias.asname or alias.name] = value
            names = _module_strings(tree, imported)
            if names != resolved[path]:
                resolved[path] = names
                changed = True
        if not changed:
            return resolved
    raise AssertionError("Android string-constant imports did not converge")


def _android_callsite_classes(
    source_overrides: dict[Path, str] | None = None,
) -> dict[str, set[bool]]:
    source_overrides = source_overrides or {}
    paths = [path for path in sorted(_ANDROID_ROOT.rglob("*.py")) if "proto" not in path.parts]
    trees = {
        path: ast.parse(
            source_overrides.get(path, path.read_text(encoding="utf-8")),
            filename=str(path),
        )
        for path in paths
    }
    names_by_path = _android_module_strings(trees)
    found: dict[str, set[bool]] = {}
    for path, tree in trees.items():
        names = names_by_path[path]
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in {"unary", "stream"}
                and node.args
            ):
                continue
            method = _string_value(node.args[0], names)
            assert method is not None, (
                f"{path.relative_to(_ANDROID_ROOT)}:{node.lineno} has an unresolved "
                f"{node.func.attr} method expression; every Android transport call must be "
                "classified"
            )
            # Ignore unrelated HTTP client.stream("GET", ...). Android gRPC
            # methods are absolute /service/method names.
            if not method.startswith("/"):
                continue
            replay_keywords = [kw.value for kw in node.keywords if kw.arg == "replay_safe"]
            assert len(replay_keywords) == 1, (
                f"{path.relative_to(_ANDROID_ROOT)}:{node.lineno} must pass one literal "
                f"replay_safe= value for {method}"
            )
            literal = replay_keywords[0]
            assert isinstance(literal, ast.Constant) and isinstance(literal.value, bool), (
                f"{path.relative_to(_ANDROID_ROOT)}:{node.lineno} replay_safe must be a bool "
                "literal, not a computed value"
            )
            found.setdefault(method, set()).add(literal.value)
    return found


def _assert_retry_manifest(callsite_classes: dict[str, set[bool]]) -> None:
    assert callsite_classes.keys() == ANDROID_RETRY_MANIFEST.keys(), (
        "Android retry manifest and unary/stream call sites differ: "
        f"only at call sites={sorted(callsite_classes.keys() - ANDROID_RETRY_MANIFEST.keys())}; "
        f"only in manifest={sorted(ANDROID_RETRY_MANIFEST.keys() - callsite_classes.keys())}"
    )
    for method, values in callsite_classes.items():
        assert values == {ANDROID_RETRY_MANIFEST[method]}, (
            f"{method} declares replay_safe={sorted(values)} but its manifest class is "
            f"{ANDROID_RETRY_MANIFEST[method]}"
        )


def test_android_retry_manifest_matches_every_callsite_literal() -> None:
    _assert_retry_manifest(_android_callsite_classes())


def test_android_retry_manifest_is_derived_from_web_registry_policy() -> None:
    by_name = {method.rsplit("/", 1)[-1]: value for method, value in ANDROID_RETRY_MANIFEST.items()}
    assert by_name.keys() == _WEB_CLASS_BY_ANDROID_NAME.keys()
    assert by_name == {
        name: _expected_retry_class(row) for name, row in _WEB_CLASS_BY_ANDROID_NAME.items()
    }


def test_android_retry_manifest_negative_self_test_rejects_a_flipped_literal() -> None:
    observed = _android_callsite_classes()
    method = next(iter(observed))
    observed[method] = {not ANDROID_RETRY_MANIFEST[method]}
    with pytest.raises(AssertionError, match="declares replay_safe"):
        _assert_retry_manifest(observed)


def test_android_retry_manifest_negative_self_test_covers_imported_constant_site() -> None:
    path = _ANDROID_ROOT / "artifacts.py"
    source = path.read_text(encoding="utf-8")
    method_offset = source.index("LIST_ARTIFACTS_METHOD")
    literal_offset = source.index("replay_safe=True", method_offset)
    changed = (
        source[:literal_offset]
        + "replay_safe=False"
        + source[literal_offset + len("replay_safe=True") :]
    )

    with pytest.raises(AssertionError, match="ListArtifacts.*declares replay_safe"):
        _assert_retry_manifest(_android_callsite_classes({path: changed}))


def test_android_session_consults_the_retry_manifest() -> None:
    tree = ast.parse((_ANDROID_ROOT / "session.py").read_text(encoding="utf-8"))
    replay_resolver = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "_resolve_replay_safe"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "replay_safe_for"
        for node in ast.walk(replay_resolver)
    )


def test_android_retry_manifest_is_a_ceiling_on_caller_replay() -> None:
    get_project = next(
        method for method in ANDROID_RETRY_MANIFEST if method.endswith("/GetProject")
    )
    create_note = next(
        method for method in ANDROID_RETRY_MANIFEST if method.endswith("/CreateNote")
    )

    assert replay_safe_for(get_project, True) is True
    assert replay_safe_for(get_project, False) is False
    assert replay_safe_for(create_note, True) is False
    assert replay_safe_for("/third.party.Service/Method", True) is False
    assert replay_safe_for("/third.party.Service/Method", False) is False
