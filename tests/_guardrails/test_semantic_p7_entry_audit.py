"""Fail-closed audit and characterization for Phase 7 (P7) entry criteria.

Governed by ADR-0035 and docs/plan/2026-08-13-semantic-backend-refactor.md.
P7 runs last: no runtime collapse is authorized until P1-P6 have isolated
semantic feature callers from RpcCaller (or recorded explicit legacy exceptions).
The ErrorInjectionMiddleware, mutable-test-seam prerequisites, and active
semantic migrations are complete. The one intentionally retained physical
RpcCaller consumer is explicitly authorized compatibility code, not a semantic
feature-service blocker.

This audit checks all P7 entry criteria, enumerates current blockers, and fails
closed if entry criteria or internal consumer inventories drift unexpectedly.
It demands a fully passing entry gate before the P7 runtime collapse begins.
"""

from __future__ import annotations

import ast
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from notebooklm._operations import Operation
from notebooklm._web.registry import WEB_SERVICE_OWNED_OPERATIONS, WEB_SUPPORTED_OPERATIONS

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
TESTS_ROOT = REPO_ROOT / "tests"
GUARDRAILS_ROOT = TESTS_ROOT / "_guardrails"

pytestmark = pytest.mark.repo_lint

# Maximum allowed legacy_exception catalog rows per ADR-0035 / Plan line 1385.
MAX_ALLOWED_LEGACY_EXCEPTIONS = 5

# P4's exact active semantic surface is a P7 input.  New migrations must update
# this baseline rather than silently changing which operations the future
# runtime collapse has to preserve.
KNOWN_ACTIVE_SEMANTIC_OPERATIONS: frozenset[Operation] = frozenset(
    {
        Operation.NOTEBOOK_LIST,
        Operation.NOTEBOOK_GET,
        Operation.NOTEBOOK_ALLOCATE,
        Operation.NOTEBOOK_PATCH,
        Operation.NOTEBOOK_DELETE,
        Operation.NOTEBOOK_REMOVE_RECENT,
        Operation.NOTEBOOK_SUMMARIZE,
        Operation.NOTEBOOK_DESCRIBE,
        Operation.SOURCE_LIST,
        Operation.SOURCE_GET,
        Operation.SOURCE_ADD_URL,
        Operation.SOURCE_ADD_URL_BATCH,
        Operation.SOURCE_ADD_TEXT,
        Operation.SOURCE_ADD_DRIVE,
        Operation.SOURCE_ADD_FILE,
        Operation.SOURCE_DELETE,
        Operation.SOURCE_PATCH_TITLE,
        Operation.SOURCE_REFRESH,
        Operation.SOURCE_CHECK_FRESHNESS,
        Operation.SOURCE_GET_GUIDE,
        Operation.SOURCE_GET_FULLTEXT,
        Operation.SETTINGS_GET,
        Operation.SETTINGS_GET_LIMITS,
        Operation.SETTINGS_SET_LANGUAGE,
        Operation.NOTEBOOK_SUGGEST_PROMPTS,
        Operation.SOURCE_WAIT,
        Operation.ARTIFACT_LIST,
        Operation.ARTIFACT_GET,
        Operation.ARTIFACT_GENERATE_AUDIO,
        Operation.ARTIFACT_GENERATE_QUIZ,
        Operation.ARTIFACT_GENERATE_FLASHCARDS,
        Operation.ARTIFACT_GENERATE_VIDEO,
        Operation.ARTIFACT_GENERATE_REPORT,
        Operation.ARTIFACT_GENERATE_INFOGRAPHIC,
        Operation.ARTIFACT_GENERATE_SLIDE_DECK,
        Operation.ARTIFACT_GENERATE_DATA_TABLE,
        Operation.ARTIFACT_GENERATE_MIND_MAP,
        Operation.ARTIFACT_EXPORT,
        Operation.ARTIFACT_REVISE_SLIDE,
        Operation.ARTIFACT_RETRY,
        Operation.ARTIFACT_DELETE,
        Operation.ARTIFACT_PATCH_TITLE,
        Operation.ARTIFACT_CATALOG,
        Operation.ARTIFACT_DOWNLOAD,
        Operation.ARTIFACT_WAIT,
        Operation.ARTIFACT_SUGGEST_REPORTS,
        Operation.NOTE_LIST,
        Operation.NOTE_GET,
        Operation.NOTE_CREATE,
        Operation.NOTE_UPDATE,
        Operation.NOTE_DELETE,
        Operation.CHAT_ASK,
        Operation.CHAT_GET_CONVERSATION,
        Operation.CHAT_GET_HISTORY,
        Operation.CHAT_DELETE_HISTORY,
        Operation.CHAT_CONFIGURE,
        Operation.CHAT_SAVE_NOTE,
        Operation.MIND_MAP_LIST,
        Operation.MIND_MAP_GET,
        Operation.MIND_MAP_GENERATE_NOTE,
        Operation.MIND_MAP_GENERATE_INTERACTIVE,
        Operation.MIND_MAP_UPDATE,
        Operation.MIND_MAP_DELETE,
        Operation.LABEL_LIST,
        Operation.LABEL_GET,
        Operation.LABEL_GENERATE,
        Operation.LABEL_DELETE,
        Operation.LABEL_MUTATE,
        Operation.LABEL_ALLOCATE,
        Operation.COLLECTION_LIST,
        Operation.COLLECTION_GET,
        Operation.COLLECTION_DELETE,
        Operation.SHARING_GET,
        Operation.SHARING_PATCH_VIEW_LEVEL,
        Operation.LEGACY_SHARE_ARTIFACT,
        Operation.SHARING_MUTATE,
        Operation.RESEARCH_START,
        Operation.RESEARCH_POLL,
        Operation.RESEARCH_CANCEL,
        Operation.RESEARCH_IMPORT,
    }
)

# Exact legacy exceptions to the zero-semantic-RpcCaller entry rule.
# ``LegacyNoteBackedService`` is explicitly retained by the P6.3 plan for
# deferred saved-chat/artifact compatibility. ``ShareManager`` is fully
# semantic and no longer owns the runtime-wide RpcCaller capability.
AUTHORIZED_LEGACY_RPC_CALLERS: frozenset[tuple[str, str, str]] = frozenset(
    {
        ("_note_service.py", "LegacyNoteBackedService.__init__", "rpc"),
    }
)

# No semantic facade/service may consume the old runtime-wide capability at P7
# entry. Keep this named empty baseline so a deliberate future exception cannot
# be hidden inside the authorized legacy-private set.
KNOWN_SEMANTIC_RPC_CALLER_BLOCKERS: frozenset[tuple[str, str, str]] = frozenset()

# The physical non-web inventory remains exact and fail-closed.
KNOWN_RPC_CALLER_CONSUMERS: frozenset[tuple[str, str, str]] = (
    AUTHORIZED_LEGACY_RPC_CALLERS | KNOWN_SEMANTIC_RPC_CALLER_BLOCKERS
)

# The one explicit request-envelope construction seam. Tests consume this
# factory; no other function in the module is exempt from the AST audit.
REQUEST_FIXTURE_SEAM = "_fixtures/chain.py"
REQUEST_FIXTURE_FACTORY = "make_request"

# P7 retired ClientComposed and migrated its behavioral oracles to the atomic
# backend-runtime tests. No mutable-holder exemption remains.
REQUIRED_COMPOSED_CHARACTERIZATION: dict[str, frozenset[str]] = {}


@dataclass(frozen=True, slots=True)
class LegacyException:
    operation: str
    approver: str
    issue: str


@dataclass(frozen=True, slots=True)
class P7EntryReport:
    ready: bool
    remaining_rpc_consumers: list[tuple[str, str, str]]
    authorized_legacy_rpc_consumers: list[tuple[str, str, str]]
    unsupported_semantic_operations: list[str]
    legacy_exceptions: list[LegacyException]
    error_injection_blocked: bool
    chain_composed_test_files: list[str]
    chain_host_test_files: list[str]
    request_context_test_files: list[str]
    active_semantic_operations: list[str]
    blockers: list[str]


def collect_rpc_caller_consumers(src_dir: Path = SRC_ROOT) -> set[tuple[str, str, str]]:
    """Scan src/notebooklm/ for classes and functions accepting RpcCaller annotations."""
    consumers: set[tuple[str, str, str]] = set()
    for path in sorted(src_dir.rglob("*.py")):
        rel_posix = path.relative_to(src_dir).as_posix()
        # Web adapters are the authorized transport boundary. P7 tracks semantic
        # services/facades that still consume RpcCaller, not provider bindings.
        if rel_posix.startswith("_web/"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

        class ConsumerVisitor(ast.NodeVisitor):
            def __init__(self, module: str) -> None:
                self.module = module
                self.owners: list[str] = []

            def visit_ClassDef(self, node: ast.ClassDef) -> None:
                self.owners.append(node.name)
                self.generic_visit(node)
                self.owners.pop()

            def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
                for arg in node.args.args + node.args.kwonlyargs:
                    if arg.annotation and "RpcCaller" in ast.unparse(arg.annotation):
                        owner = ".".join((*self.owners, node.name))
                        consumers.add((self.module, owner, arg.arg))
                self.owners.append(node.name)
                self.generic_visit(node)
                self.owners.pop()

            def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
                self._visit_function(node)

            def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
                self._visit_function(node)

        ConsumerVisitor(rel_posix).visit(tree)
    return consumers


MIGRATED_SOURCE_MODULES = frozenset(
    {
        "_source/add.py",
        "_source/batch.py",
        "_source/content.py",
        "_source/listing.py",
        "_source/polling.py",
        "_source/upload.py",
        "_mutation_services.py",
        "_notebook_metadata.py",
        "_read_services.py",
        "_source_service.py",
        "_sources.py",
    }
)

# P6's fully semantic feature facades and their immediate semantic services.
# Partial legacy/raw-RPC owners (notably NotebooksAPI and LegacyNoteBackedService)
# remain in the separately classified physical inventory below.
MIGRATED_FEATURE_RPC_NEUTRAL_MODULES = frozenset(
    {
        "_artifact/listing.py",
        "_artifacts.py",
        "_chat/api.py",
        "_chat/service.py",
        "_collections.py",
        "_label_service.py",
        "_labels.py",
        "_mind_maps_api.py",
        "_notes.py",
        "_research.py",
        "_research_service.py",
        "_settings.py",
        "_settings_service.py",
        "_sharing.py",
        "_sharing_service.py",
        "_source_service.py",
        "_sources.py",
    }
)

# Exact post-P6 physical imports outside the protocol/binding homes skipped by
# the P0 measurement. Every survivor has a named compatibility/protocol role;
# a feature facade is never admitted here as a convenient exception.
CLASSIFIED_NON_WEB_RPC_METHOD_IMPORTS: dict[str, str] = {
    "_artifact/formatters.py": "legacy artifact wire decoder",
    "_backend_compat.py": "legacy public exception diagnostic projector",
    "_note_service.py": "plan-authorized LegacyNoteBackedService",
    "_notebooks.py": "documented public raw-RPC compatibility owner",
    "_research_task_parser.py": "legacy research wire decoder",
    "_row_adapters/artifacts.py": "artifact positional row decoder",
    "_row_adapters/chat.py": "chat positional row decoder",
    "_row_adapters/notes.py": "note positional row decoder",
    "_row_adapters/research.py": "research positional row decoder",
    "_row_adapters/sources.py": "source positional row decoder",
    "_runtime/contracts.py": "web RPC protocol type declaration",
    "_types/notebooks.py": "legacy notebook wire decoder",
    "_types/sharing.py": "legacy sharing wire decoder",
    "_web_request_auth.py": "web request binding support",
    "client.py": "documented public raw rpc_call escape hatch",
}


def collect_rpc_method_import_modules(src_dir: Path = SRC_ROOT) -> set[str]:
    """Collect non-binding modules that still import the native RPC enum."""
    found: set[str] = set()
    for path in sorted(src_dir.rglob("*.py")):
        relative = path.relative_to(src_dir)
        if (
            relative.is_relative_to("rpc")
            or relative.is_relative_to("_web")
            or path.name.startswith("_idempotency")
        ):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if any(
            isinstance(node, (ast.Import, ast.ImportFrom))
            and any(alias.name == "RPCMethod" for alias in node.names)
            for node in ast.walk(tree)
        ):
            found.add(relative.as_posix())
    return found


def collect_migrated_feature_rpc_method_leaks(
    src_dir: Path = SRC_ROOT,
) -> set[tuple[str, int, str]]:
    """Find native enum imports/references in the fully migrated P6 slice."""
    leaks: set[tuple[str, int, str]] = set()
    for relative in sorted(MIGRATED_FEATURE_RPC_NEUTRAL_MODULES):
        path = src_dir / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)) and any(
                alias.name == "RPCMethod" for alias in node.names
            ):
                leaks.add((relative, node.lineno, "import RPCMethod"))
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id == "RPCMethod"
            ):
                leaks.add((relative, node.lineno, f"RPCMethod.{node.attr}"))
    return leaks


def collect_migrated_source_transport_leaks(
    src_dir: Path = SRC_ROOT,
) -> set[tuple[str, int, str]]:
    """Find direct RPC execution/vocabulary reintroduced into migrated source layers."""

    leaks: set[tuple[str, int, str]] = set()
    forbidden_imports = {"RpcCaller", "RPCMethod", "SourceWireCaller"}
    for relative in sorted(MIGRATED_SOURCE_MODULES):
        path = src_dir / relative
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name in forbidden_imports:
                        leaks.add((relative, node.lineno, f"import {alias.name}"))
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "rpc_call"
            ):
                leaks.add((relative, node.lineno, "rpc_call execution"))
            if (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == "rpc_call"
            ):
                leaks.add((relative, node.lineno, "RpcCaller-compatible protocol"))
    return leaks


def collect_legacy_exceptions(
    operation_specs: Sequence[Any] | None = None,
) -> list[LegacyException]:
    """Collect legacy_exception declarations from operation specs."""
    if operation_specs is None:
        try:
            from scripts._operation_catalog_specs import OPERATION_SPECS

            operation_specs = OPERATION_SPECS
        except ImportError:
            operation_specs = []

    exceptions: list[LegacyException] = []
    for spec in operation_specs:
        legacy = getattr(spec, "legacy_exception", None)
        if legacy is not None:
            approver = getattr(legacy, "approver", "") or ""
            issue = getattr(legacy, "issue", "") or ""
            op_key = getattr(spec, "operation", None)
            op_name = op_key.value if op_key is not None else str(spec)
            exceptions.append(LegacyException(operation=op_name, approver=approver, issue=issue))
    return exceptions


def collect_unapproved_legacy_private_operations(
    operation_specs: Sequence[Any] | None = None,
) -> list[str]:
    """Return legacy-private rows missing reviewed exception metadata."""
    if operation_specs is None:
        try:
            from scripts._operation_catalog_specs import OPERATION_SPECS

            operation_specs = OPERATION_SPECS
        except ImportError:
            operation_specs = []

    missing: list[str] = []
    for spec in operation_specs:
        disposition = getattr(spec, "disposition", None)
        disposition_value = getattr(disposition, "value", disposition)
        if disposition_value != "legacy_private":
            continue
        if getattr(spec, "legacy_exception", None) is not None:
            continue
        operation = getattr(spec, "operation", None)
        missing.append(getattr(operation, "value", str(operation)))
    return sorted(missing)


def collect_unsupported_semantic_operations(
    operation_specs: Sequence[Any] | None = None,
) -> list[str]:
    """Return semantic catalog rows that still lack an active web binding."""
    if operation_specs is None:
        from scripts._operation_catalog_specs import OPERATION_SPECS

        operation_specs = OPERATION_SPECS

    unsupported: list[str] = []
    for spec in operation_specs:
        operation = getattr(spec, "operation", None)
        disposition = getattr(getattr(spec, "disposition", None), "value", None)
        # P9.2: a service-owned workflow is sequenced from active leaf bindings by
        # its semantic service; it is not invokable but it is not unmigrated.
        if (
            disposition == "semantic"
            and operation not in WEB_SUPPORTED_OPERATIONS
            and operation not in WEB_SERVICE_OWNED_OPERATIONS
        ):
            unsupported.append(operation.value)
    return sorted(unsupported)


def check_error_injection_middleware_dependency(
    middleware_path: Path = SRC_ROOT / "_middleware" / "error_injection.py",
) -> bool:
    """Check if ErrorInjectionMiddleware still imports from _middleware.core."""
    if not middleware_path.exists():
        return False
    tree = ast.parse(middleware_path.read_text(encoding="utf-8"), filename=str(middleware_path))
    core_imports = {"NextCall", "RpcRequest", "RpcResponse", "core"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import) and any(
            alias.name == "notebooklm._runtime.rpc_call" for alias in node.names
        ):
            return True
        if isinstance(node, ast.ImportFrom):
            if node.module in {
                "core",
                ".core",
                "notebooklm._runtime.rpc_call",
                "._middleware.core",
            }:
                return True
            if any(alias.name in core_imports for alias in node.names):
                return True
    return False


@dataclass(frozen=True, slots=True)
class MutableRuntimeTestUses:
    composed: set[str]
    chain_host: set[str]
    request_context: set[str]


def _dotted_name(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Name):
        return (node.id,)
    if isinstance(node, ast.Attribute):
        return (*_dotted_name(node.value), node.attr)
    if isinstance(node, ast.Subscript):
        return _dotted_name(node.value)
    return ()


def _assigned_names(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Name):
        return {node.id}
    if isinstance(node, (ast.Tuple, ast.List)):
        return {name for item in node.elts for name in _assigned_names(item)}
    return set()


class _MutableRuntimeUseVisitor(ast.NodeVisitor):
    """Find construction/mutation without classifying read-only observations."""

    def __init__(self, relative_path: str) -> None:
        self.relative_path = relative_path
        self.import_aliases: dict[str, str] = {}
        self.request_names: set[str] = set()
        self.context_names: set[str] = set()
        self.host_names: set[str] = set()
        self.function_names: list[str] = []
        self.composed = False
        self.chain_host = False
        self.request_context = False

    @property
    def _in_request_factory(self) -> bool:
        return self.relative_path == REQUEST_FIXTURE_SEAM and self.function_names == [
            REQUEST_FIXTURE_FACTORY
        ]

    @property
    def _in_required_composed_characterization(self) -> bool:
        required_names = REQUIRED_COMPOSED_CHARACTERIZATION.get(self.relative_path, frozenset())
        return any(name in required_names for name in self.function_names)

    def _mark_composed(self) -> None:
        if not self._in_required_composed_characterization:
            self.composed = True

    def _mark_request_context(self) -> None:
        if not self._in_request_factory:
            self.request_context = True

    def _resolved_tail(self, node: ast.AST) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value.rsplit(".", 1)[-1]
        dotted = _dotted_name(node)
        if not dotted:
            return ""
        return self.import_aliases.get(dotted[0], dotted[-1])

    def _is_request_value(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Call):
            return self._resolved_tail(node.func) in {
                "RpcRequest",
                "make_request",
                "materialize_rpc_request",
            }
        return isinstance(node, ast.Name) and node.id in self.request_names

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.name in {"ClientComposed", "MiddlewareChainHost", "RpcRequest"}:
                self.import_aliases[alias.asname or alias.name] = alias.name

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.function_names.append(node.name)
        self._record_request_parameters(node)
        self.generic_visit(node)
        self.function_names.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self.function_names.append(node.name)
        self._record_request_parameters(node)
        self.generic_visit(node)
        self.function_names.pop()

    def _record_request_parameters(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        for arg in (*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs):
            if arg.annotation is not None and self._resolved_tail(arg.annotation) == "RpcRequest":
                self.request_names.add(arg.arg)

    def visit_Assign(self, node: ast.Assign) -> None:
        names = {name for target in node.targets for name in _assigned_names(target)}
        self._record_aliases(names, node.value)
        self._record_mutation_targets(node.targets)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        names = _assigned_names(node.target)
        if node.value is not None:
            self._record_aliases(names, node.value)
        self._record_mutation_targets([node.target])
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._record_mutation_targets([node.target])
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        self._record_mutation_targets(node.targets)
        self.generic_visit(node)

    def _record_aliases(self, names: set[str], value: ast.AST) -> None:
        dotted = _dotted_name(value)
        if "chain_host" in dotted or (isinstance(value, ast.Name) and value.id in self.host_names):
            self.host_names.update(names)
        if self._is_request_value(value):
            self.request_names.update(names)
        if (
            len(dotted) >= 2
            and dotted[-1] == "context"
            and (
                dotted[0] in self.request_names
                or dotted[0] in {"current", "req", "request", "retry_request"}
            )
        ) or (isinstance(value, ast.Name) and value.id in self.context_names):
            self.context_names.update(names)

    def _record_mutation_targets(self, targets: Sequence[ast.AST]) -> None:
        for target in targets:
            dotted = _dotted_name(target)
            if not dotted:
                continue
            if "_composed" in dotted:
                self._mark_composed()
            if "chain_host" in dotted or dotted[0] in self.host_names:
                self.chain_host = True
            if dotted[0] in self.context_names or (
                "context" in dotted
                and (
                    dotted[0] in self.request_names
                    or dotted[0] in {"current", "req", "request", "retry_request"}
                )
            ):
                self._mark_request_context()

    def visit_Call(self, node: ast.Call) -> None:
        resolved = self._resolved_tail(node.func)
        if resolved == "ClientComposed":
            self._mark_composed()
        elif resolved == "MiddlewareChainHost":
            self.chain_host = True
        elif resolved == "RpcRequest":
            # Direct construction is forbidden regardless of whether context
            # is supplied positionally, by keyword, or allowed to default.
            self._mark_request_context()

        dotted = _dotted_name(node.func)
        if dotted and dotted[-1] in {
            "_bind_transport",
            "bind_chain_host",
            "bind_chain_metadata",
            "bind_executor",
            "bind_runtime_collaborators",
            "bind_transport",
            "reset_after_open",
            "set_bound_loop",
        }:
            if "_composed" in dotted:
                self._mark_composed()
            if "chain_host" in dotted or dotted[0] in self.host_names:
                self.chain_host = True
        if dotted and dotted[-1] in {
            "__setitem__",
            "clear",
            "pop",
            "popitem",
            "setdefault",
            "update",
        }:
            if dotted[0] in self.context_names or (
                "context" in dotted
                and (
                    dotted[0] in self.request_names
                    or dotted[0] in {"current", "req", "request", "retry_request"}
                )
            ):
                self._mark_request_context()
        if resolved == "replace" and node.args and self._is_request_value(node.args[0]):
            if any(kw.arg == "context" for kw in node.keywords):
                self._mark_request_context()
        self.generic_visit(node)


def collect_mutable_runtime_test_uses(tests_dir: Path = TESTS_ROOT) -> MutableRuntimeTestUses:
    """Scan all non-guardrail tests for the three forbidden mutable-runtime seams."""
    composed_files: set[str] = set()
    chain_host_files: set[str] = set()
    request_context_files: set[str] = set()

    for path in sorted(tests_dir.rglob("*.py")):
        if GUARDRAILS_ROOT in path.parents or path.parent == GUARDRAILS_ROOT:
            continue
        rel_posix = path.relative_to(tests_dir).as_posix()
        visitor = _MutableRuntimeUseVisitor(rel_posix)
        visitor.visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
        if visitor.composed:
            composed_files.add(rel_posix)
        if visitor.chain_host:
            chain_host_files.add(rel_posix)
        if visitor.request_context:
            request_context_files.add(rel_posix)

    return MutableRuntimeTestUses(
        composed=composed_files,
        chain_host=chain_host_files,
        request_context=request_context_files,
    )


def evaluate_p7_entry_readiness(
    src_dir: Path = SRC_ROOT,
    tests_dir: Path = TESTS_ROOT,
    operation_specs: Sequence[Any] | None = None,
) -> P7EntryReport:
    """Evaluate all P7 entry criteria and return a structured audit report."""
    physical_rpc_consumers = collect_rpc_caller_consumers(src_dir)
    authorized_legacy_rpc_consumers = sorted(physical_rpc_consumers & AUTHORIZED_LEGACY_RPC_CALLERS)
    semantic_rpc_consumers = sorted(physical_rpc_consumers - AUTHORIZED_LEGACY_RPC_CALLERS)
    unsupported_semantic_operations = collect_unsupported_semantic_operations(operation_specs)
    legacy_exceptions = collect_legacy_exceptions(operation_specs)
    unapproved_legacy_private = collect_unapproved_legacy_private_operations(operation_specs)
    ei_blocked = check_error_injection_middleware_dependency(
        src_dir / "_middleware" / "error_injection.py"
    )
    mutable_uses = collect_mutable_runtime_test_uses(tests_dir)

    blockers: list[str] = []

    active_operation_drift = set(WEB_SUPPORTED_OPERATIONS) ^ set(KNOWN_ACTIVE_SEMANTIC_OPERATIONS)
    if active_operation_drift:
        blockers.append(
            "active semantic operation baseline drifted: "
            + ", ".join(sorted(operation.value for operation in active_operation_drift))
        )

    if semantic_rpc_consumers:
        blockers.append(
            f"{len(semantic_rpc_consumers)} semantic-service call sites still consume "
            "RpcCaller directly"
        )

    if unsupported_semantic_operations:
        blockers.append(
            f"{len(unsupported_semantic_operations)} catalog operations still have semantic "
            "disposition without an active web binding: "
            + ", ".join(unsupported_semantic_operations)
        )

    if len(legacy_exceptions) > MAX_ALLOWED_LEGACY_EXCEPTIONS:
        blockers.append(
            f"legacy_exceptions count ({len(legacy_exceptions)}) exceeds maximum allowed "
            f"ceiling of {MAX_ALLOWED_LEGACY_EXCEPTIONS}"
        )

    for exc in legacy_exceptions:
        if not exc.approver or not exc.issue:
            blockers.append(
                f"legacy_exception for operation {exc.operation!r} must specify both an approver and an open removal issue"
            )

    if unapproved_legacy_private:
        blockers.append(
            "legacy-private operations missing an approver and evidenced open removal issue: "
            + ", ".join(unapproved_legacy_private)
        )

    if ei_blocked:
        blockers.append(
            "ErrorInjectionMiddleware still imports from _middleware.core (must be migrated/rehomed before P7)"
        )

    if mutable_uses.composed:
        blockers.append(
            f"{len(mutable_uses.composed)} test files outside _guardrails/ still construct or mutate ClientComposed"
        )

    if mutable_uses.chain_host:
        blockers.append(
            f"{len(mutable_uses.chain_host)} test files outside _guardrails/ still construct or mutate MiddlewareChainHost"
        )

    if mutable_uses.request_context:
        blockers.append(
            f"{len(mutable_uses.request_context)} test files outside _guardrails/ still construct or mutate RpcRequest.context"
        )

    return P7EntryReport(
        ready=len(blockers) == 0,
        remaining_rpc_consumers=semantic_rpc_consumers,
        authorized_legacy_rpc_consumers=authorized_legacy_rpc_consumers,
        unsupported_semantic_operations=unsupported_semantic_operations,
        legacy_exceptions=legacy_exceptions,
        error_injection_blocked=ei_blocked,
        chain_composed_test_files=sorted(mutable_uses.composed),
        chain_host_test_files=sorted(mutable_uses.chain_host),
        request_context_test_files=sorted(mutable_uses.request_context),
        active_semantic_operations=sorted(
            operation.value for operation in WEB_SUPPORTED_OPERATIONS
        ),
        blockers=blockers,
    )


# --- Test Suite -------------------------------------------------------------


def test_p7_entry_is_ready_after_all_semantic_operations_migrate() -> None:
    """All P7 entry criteria pass with only the authorized note compatibility caller."""
    report = evaluate_p7_entry_readiness()

    assert report.ready
    assert report.remaining_rpc_consumers == []
    assert report.authorized_legacy_rpc_consumers == sorted(AUTHORIZED_LEGACY_RPC_CALLERS)
    assert report.unsupported_semantic_operations == []
    assert report.legacy_exceptions == []
    assert report.blockers == []


def test_rpccaller_consumer_inventory_is_exact_and_fails_closed() -> None:
    """The set of RpcCaller consumers in src/notebooklm/ matches known baseline."""
    actual_consumers = collect_rpc_caller_consumers()
    unclassified_new = actual_consumers - KNOWN_RPC_CALLER_CONSUMERS
    removed = KNOWN_RPC_CALLER_CONSUMERS - actual_consumers

    assert not unclassified_new, (
        "New, unclassified RpcCaller consumers found in src/notebooklm/:\n  "
        + "\n  ".join(f"{p}:{fn}({arg})" for p, fn, arg in sorted(unclassified_new))
    )
    assert not removed, (
        "Reviewed RpcCaller consumers disappeared; update the exact authorized inventory:\n  "
        + "\n  ".join(f"{p}:{fn}({arg})" for p, fn, arg in sorted(removed))
    )


def test_authorized_legacy_rpc_callers_are_exact_and_semantic_baseline_is_zero() -> None:
    """Only the plan-backed note compatibility implementation is exempt."""
    assert {
        ("_note_service.py", "LegacyNoteBackedService.__init__", "rpc"),
    } == AUTHORIZED_LEGACY_RPC_CALLERS
    assert not KNOWN_SEMANTIC_RPC_CALLER_BLOCKERS


def test_migrated_source_layers_cannot_reintroduce_transport_execution() -> None:
    leaks = collect_migrated_source_transport_leaks()
    assert not leaks, (
        "Migrated source layers crossed the web transport boundary:\n  "
        + "\n  ".join(f"{path}:{line}: {reason}" for path, line, reason in sorted(leaks))
    )


def test_migrated_feature_layers_cannot_reintroduce_native_rpc_vocabulary() -> None:
    leaks = collect_migrated_feature_rpc_method_leaks()
    assert not leaks, (
        "Migrated feature layers reintroduced native RPC vocabulary:\n  "
        + "\n  ".join(f"{path}:{line}: {reason}" for path, line, reason in sorted(leaks))
    )


def test_remaining_non_web_rpc_method_imports_are_exact_and_classified() -> None:
    assert collect_rpc_method_import_modules() == set(CLASSIFIED_NON_WEB_RPC_METHOD_IMPORTS)
    assert all(reason.strip() for reason in CLASSIFIED_NON_WEB_RPC_METHOD_IMPORTS.values())


def test_active_semantic_operation_inventory_is_exact_for_p7() -> None:
    """P7's runtime-collapse input is the exact P4-supported operation set."""
    assert len(KNOWN_ACTIVE_SEMANTIC_OPERATIONS) == 80
    assert WEB_SUPPORTED_OPERATIONS == KNOWN_ACTIVE_SEMANTIC_OPERATIONS


def test_legacy_exception_policy_and_ceiling() -> None:
    """Legacy exception rows must be <= 5 and carry valid approver + issue."""
    exceptions = collect_legacy_exceptions()
    assert collect_unapproved_legacy_private_operations() == []
    assert len(exceptions) <= MAX_ALLOWED_LEGACY_EXCEPTIONS, (
        f"Too many legacy exceptions: {len(exceptions)} > {MAX_ALLOWED_LEGACY_EXCEPTIONS}"
    )
    for exc in exceptions:
        assert exc.approver, f"Legacy exception {exc.operation} missing approver"
        assert exc.issue, f"Legacy exception {exc.operation} missing issue"


def test_error_injection_middleware_isolated_for_p7() -> None:
    """The retired test-only middleware is no longer a P7 core-type blocker."""
    assert not (SRC_ROOT / "_middleware" / "error_injection.py").exists()
    assert check_error_injection_middleware_dependency() is False


def test_mutable_runtime_test_seams_are_fully_migrated() -> None:
    """No test uses retired runtime mutation outside the named P7 behavior oracle."""
    uses = collect_mutable_runtime_test_uses()

    assert not uses.composed, (
        "Tests constructing/mutating ClientComposed outside _guardrails/:\n  "
        + "\n  ".join(sorted(uses.composed))
    )
    assert not uses.chain_host, (
        "Tests constructing/mutating MiddlewareChainHost outside _guardrails/:\n  "
        + "\n  ".join(sorted(uses.chain_host))
    )
    assert not uses.request_context, (
        "Tests constructing/mutating RpcRequest.context outside _guardrails/:\n  "
        + "\n  ".join(sorted(uses.request_context))
    )


def test_required_composed_characterization_remains_until_p7() -> None:
    """The narrow detector exemption cannot outlive the holder behavior oracle."""
    for relative_path, required_names in REQUIRED_COMPOSED_CHARACTERIZATION.items():
        tree = ast.parse(
            (TESTS_ROOT / relative_path).read_text(encoding="utf-8"),
            filename=relative_path,
        )
        functions = {
            node.name: node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        assert required_names <= functions.keys()
        for name in required_names:
            assert any(
                isinstance(node, ast.Call) and _dotted_name(node.func)[-1:] == ("ClientComposed",)
                for node in ast.walk(functions[name])
            ), f"{relative_path}:{name} no longer characterizes ClientComposed"


# --- Detector Self-Tests (Fail-Closed Mutation Tests) ------------------------


def test_evaluator_blocks_a_new_unapproved_semantic_rpc_consumer(tmp_path: Path) -> None:
    """Authorization is exact: every newly introduced non-web consumer blocks P7."""
    consumer = tmp_path / "semantic_service.py"
    consumer.write_text(
        "from notebooklm._runtime.contracts import RpcCaller\n"
        "class SemanticService:\n"
        "    def __init__(self, rpc: RpcCaller) -> None:\n"
        "        self.rpc = rpc\n",
        encoding="utf-8",
    )

    report = evaluate_p7_entry_readiness(src_dir=tmp_path)

    assert report.ready is False
    assert report.remaining_rpc_consumers == [
        ("semantic_service.py", "SemanticService.__init__", "rpc")
    ]
    assert any("semantic-service call sites still consume RpcCaller" in b for b in report.blockers)


def test_detector_fails_closed_when_legacy_exceptions_exceed_ceiling() -> None:
    """A catalog with > 5 legacy exceptions triggers a blocker."""
    fake_specs = [
        type(
            "Spec",
            (),
            {
                "operation": type("Op", (), {"value": f"op.{i}"}),
                "legacy_exception": type("Legacy", (), {"approver": "owner", "issue": "#123"}),
            },
        )()
        for i in range(6)
    ]
    report = evaluate_p7_entry_readiness(operation_specs=fake_specs)
    assert any("exceeds maximum allowed ceiling of 5" in b for b in report.blockers)


def test_detector_fails_closed_when_legacy_exception_missing_approver_or_issue() -> None:
    """A legacy exception without approver or issue triggers a blocker."""
    fake_specs = [
        type(
            "Spec",
            (),
            {
                "operation": type("Op", (), {"value": "op.test"}),
                "legacy_exception": type("Legacy", (), {"approver": "", "issue": "#123"}),
            },
        )(),
        type(
            "Spec",
            (),
            {
                "operation": type("Op", (), {"value": "op.test2"}),
                "legacy_exception": type("Legacy", (), {"approver": "owner", "issue": ""}),
            },
        )(),
    ]
    report = evaluate_p7_entry_readiness(operation_specs=fake_specs)
    assert any(
        "must specify both an approver and an open removal issue" in b for b in report.blockers
    )


def test_error_injection_detector_catches_reintroduced_relative_core_import(
    tmp_path: Path,
) -> None:
    middleware_path = tmp_path / "error_injection.py"
    middleware_path.write_text(
        "from .core import NextCall, RpcRequest, RpcResponse\n",
        encoding="utf-8",
    )

    assert check_error_injection_middleware_dependency(middleware_path) is True


def test_error_injection_detector_catches_reintroduced_absolute_core_import(
    tmp_path: Path,
) -> None:
    middleware_path = tmp_path / "error_injection.py"
    middleware_path.write_text(
        "import notebooklm._runtime.rpc_call as core\n",
        encoding="utf-8",
    )

    assert check_error_injection_middleware_dependency(middleware_path) is True


@pytest.mark.parametrize(
    ("source", "field"),
    [
        (
            "from notebooklm._runtime.composition import ClientComposed as Composed\n"
            "value = Composed()\n",
            "composed",
        ),
        (
            "host = client._composed.chain_host\nhost._rate_limit_max_retries = 0\n",
            "chain_host",
        ),
        (
            "from notebooklm._runtime.rpc_call import RpcRequest as Request\n"
            "value = Request(url='x', headers={}, body=b'', context={})\n",
            "request_context",
        ),
        (
            "from notebooklm._runtime.rpc_call import RpcRequest as Request\n"
            "value = Request('x', {}, b'', {})\n",
            "request_context",
        ),
        (
            "from notebooklm._runtime.rpc_call import RpcRequest\n"
            "def mutate(request: RpcRequest):\n"
            "    request.context.update({'rpc_method': 'x'})\n",
            "request_context",
        ),
        (
            "from notebooklm._runtime.rpc_call import RpcRequest\n"
            "def mutate(request: RpcRequest):\n"
            "    context = request.context\n"
            "    alias = context\n"
            "    alias['rpc_method'] = 'x'\n",
            "request_context",
        ),
    ],
)
def test_mutable_runtime_detector_fails_closed_on_forbidden_test_seams(
    tmp_path: Path,
    source: str,
    field: str,
) -> None:
    test_file = tmp_path / "test_forbidden_seam.py"
    test_file.write_text(source, encoding="utf-8")

    uses = collect_mutable_runtime_test_uses(tmp_path)

    assert getattr(uses, field) == {"test_forbidden_seam.py"}


def test_request_factory_exemption_is_function_scoped_and_fails_closed(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "_fixtures"
    fixture_dir.mkdir()
    fixture = fixture_dir / "chain.py"
    fixture.write_text(
        "from notebooklm._runtime.rpc_call import RpcRequest\n"
        "def make_request():\n"
        "    return RpcRequest('x', {}, b'', {})\n"
        "def bypass_factory():\n"
        "    return RpcRequest('x', {}, b'', {})\n",
        encoding="utf-8",
    )

    uses = collect_mutable_runtime_test_uses(tmp_path)

    assert uses.request_context == {"_fixtures/chain.py"}


def test_request_factory_is_the_only_allowed_request_constructor(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "_fixtures"
    fixture_dir.mkdir()
    fixture = fixture_dir / "chain.py"
    fixture.write_text(
        "from notebooklm._runtime.rpc_call import RpcRequest\n"
        "def make_request():\n"
        "    return RpcRequest('x', {}, b'', {})\n",
        encoding="utf-8",
    )

    uses = collect_mutable_runtime_test_uses(tmp_path)

    assert not uses.request_context


def test_request_fixture_context_alias_outside_factory_fails_closed(tmp_path: Path) -> None:
    fixture_dir = tmp_path / "_fixtures"
    fixture_dir.mkdir()
    fixture = fixture_dir / "chain.py"
    fixture.write_text(
        "from notebooklm._runtime.rpc_call import RpcRequest\n"
        "def make_request():\n"
        "    return RpcRequest('x', {}, b'', {})\n"
        "def mutate(request: RpcRequest):\n"
        "    context = request.context\n"
        "    context.update({'rpc_method': 'x'})\n",
        encoding="utf-8",
    )

    uses = collect_mutable_runtime_test_uses(tmp_path)

    assert uses.request_context == {"_fixtures/chain.py"}


def test_composed_characterization_exemption_is_function_scoped(tmp_path: Path) -> None:
    unit_dir = tmp_path / "unit"
    unit_dir.mkdir()
    characterization = unit_dir / "test_semantic_p7_runtime_characterization.py"
    characterization.write_text(
        "from notebooklm._client_composed import ClientComposed\n"
        "def test_client_composed_max_concurrent_rpcs_validation():\n"
        "    ClientComposed(max_concurrent_rpcs=0)\n"
        "def unreviewed_holder_seam():\n"
        "    ClientComposed()\n",
        encoding="utf-8",
    )

    uses = collect_mutable_runtime_test_uses(tmp_path)

    assert uses.composed == {"unit/test_semantic_p7_runtime_characterization.py"}
