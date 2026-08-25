"""Service-boundary guardrails for programme P10 (invariants I1, I2, I9).

Governed by :doc:`ADR-0035 <../../docs/adr/0035-semantic-backend-boundary>` and
``docs/plan/remediation-plan.md`` (the P10 remediation plan). Three of that
plan's target invariants are enforced here; each is a **ratchet with a seed
allowlist that shrinks only**, so a violation that exists today is recorded
once and a new one fails immediately:

**I1 — semantic service modules stay neutral.** A semantic service module
(``src/notebooklm/_*_service.py``, ``_read_services.py``,
``_mutation_services.py``, ``_chat/service.py`` — later ``_chat/workflow.py`` —
and ``_studio/*.py``) may import none of ``_projectors``,
``notebooklm.types``, ``_types.*``, ``_backend_compat``, ``rpc.*``,
``_row_adapters.*``, ``_web.*`` or ``httpx``, and its public methods must
return ``*Record`` / ``*Result`` types, neutral enums, built-in scalars or
collections thereof, or ``None``. Projection to public models is a *facade*
responsibility — the ADR's Decision lists "public model projectors" among the
dependencies a service may take, so I1 is authorised by the plan's decision
**D7** (an ADR-0035 addendum landed in R0.0), not by the unamended ADR.

**I2 — ``_web/**`` imports no domain package.** The web backend may not import
``_chat``, ``_source``, ``_studio``, ``_artifact``, ``_mind_map`` or any
semantic service module. Neutral helper modules (``_records*``,
``_research_neutral``, ``_deadline``, ``_request_types``, ``_markdown``) stay
permitted and are asserted as such below, so the rule cannot be widened into
one that forbids the neutral direction too.

**I9 — no ``Legacy*`` class below ``_app``** except the enumerated exemptions:
the legacy-mapping records consumed only by ``_backend_compat``/the projectors
and the auth storage-migration types. The two deletion targets that P10 owns
are recorded separately from the exemptions so they cannot be quietly
reclassified as permanent.

**I10 is deliberately not enforced here.** The plan's tenth invariant caps
``src/notebooklm/_records.py`` at 1,500 lines; that is already
:mod:`tests._guardrails.test_module_size_ratchet`'s job — ``_records.py``
measures exactly ``MODULE_SIZE_BUDGET`` lines and is not in
``ALLOWLISTED_CEILINGS``, so ``test_no_module_exceeds_the_size_budget`` fails
on the first line of growth. Duplicating it here would create a second
authority for one ceiling.

This module also carries, verbatim, the five assertions of the retired
``test_semantic_read_boundary.py``. I1 subsumes that guard's *intent* but not
all of its checks: it pinned the exact import sets of ``_read_services.py`` and
``_mutation_services.py`` (tighter than I1's forbidden-list rule, and
``_read_services.py`` is an I1 seed so I1 does not inspect it at all), and it
also constrained ``_projectors.py``, which is not a service module. Those
checks are ported below under "Read-core pins" so no assertion is lost.
"""

from __future__ import annotations

import ast
from pathlib import Path

import notebooklm
import notebooklm._projectors as projector_module
import notebooklm._read_services as service_module
import notebooklm.types as public_types

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
WEB_ROOT = SRC_ROOT / "_web"
APP_ROOT = SRC_ROOT / "_app"

# --- I1: semantic service modules -------------------------------------------

#: Top-level ``notebooklm`` targets a semantic service may never import. Every
#: entry is either wire (``rpc``, ``_row_adapters``, ``_web``, ``httpx``), a
#: public model surface (``types``, ``_types``), or the projection/legacy
#: compatibility layer (``_projectors``, ``_backend_compat``) that belongs to
#: the facade above the service.
I1_FORBIDDEN_FIRST_PARTY_ROOTS: frozenset[str] = frozenset(
    {
        "_backend_compat",
        "_projectors",
        "_row_adapters",
        "_types",
        "_web",
        "rpc",
        "types",
    }
)

#: Shrinking seed: the semantic service modules that violate I1 today. Entries
#: leave as their retiring slice lands (R5.2 for ``_studio``, R6.1-R6.4 for the
#: root services, R6.6 for ``_note_service``); nothing may be added. The plan's
#: §6 target is an empty set beside the permanent exemption below.
I1_SEED_ALLOWLIST: frozenset[str] = frozenset(
    {
        "_note_service.py",
        "_notebook_guide_service.py",
        "_notebook_mutation_service.py",
        "_read_services.py",
        "_research_service.py",
        "_settings_service.py",
        "_sharing_service.py",
        "_studio/catalog.py",
        "_studio/lifecycle.py",
        "_studio/mind_maps.py",
        "_studio/representations.py",
        "_suggestion_service.py",
    }
)

#: Permanent, *named* exemption — not a shrinking seed. ``_studio/downloads.py``
#: owns the byte-download clients (``httpx`` plus ``_curl_cffi_transport``);
#: moving that transport under ``_web`` is explicitly out of P10 scope and is
#: deferred to a separate download-transport slice (plan §0, §8).
I1_PERMANENT_EXEMPTIONS: frozenset[str] = frozenset({"_studio/downloads.py"})

#: Built-in scalars and collection constructors a neutral service may name in a
#: return annotation. Deliberately minimal: widening it is a reviewed change,
#: which is the point of the invariant.
I1_PERMITTED_RETURN_BUILTINS: frozenset[str] = frozenset(
    {
        "None",
        "bool",
        "bytes",
        "dict",
        "float",
        "frozenset",
        "int",
        "list",
        "set",
        "str",
        "tuple",
    }
)

#: Modules that define the neutral record/enum vocabulary services return.
NEUTRAL_RECORD_MODULE_NAMES: frozenset[str] = frozenset({"_records.py", "_research_neutral.py"})

# --- I2: the web backend takes no domain dependency --------------------------

#: Domain *packages* above the semantic port. ``_web`` consumes their neutral
#: records, never their modules.
I2_FORBIDDEN_DOMAIN_PACKAGES: frozenset[str] = frozenset(
    {"_artifact", "_chat", "_mind_map", "_source", "_studio"}
)

#: Shrinking seed: the ``_web`` files that import a domain package today, as
#: paths relative to ``src/notebooklm/_web``. ``codec/chat_stream.py`` and
#: ``codec/chat.py`` retired in R2.1 (the codec now owns the streamed-ask wire
#: and emits records); ``backend.py`` retires in R2.3/R3.1,
#: ``bindings/mind_maps.py`` in R4.2 and ``bindings/sources.py`` in R3.5.
I2_SEED_ALLOWLIST: frozenset[str] = frozenset(
    {
        "backend.py",
        "bindings/mind_maps.py",
        "bindings/sources.py",
    }
)

#: Neutral helpers ``_web`` legitimately imports. Asserted to be *permitted* so
#: the domain rule cannot be broadened into one that also severs the neutral
#: record/deadline/request-type direction the port is built on.
I2_PERMITTED_NEUTRAL_HELPERS: frozenset[str] = frozenset(
    {
        "_deadline",
        "_markdown",
        "_records",
        "_request_types",
        "_research_neutral",
    }
)

# --- I9: no legacy classes below the application layer -----------------------

#: Enumerated I9 exemptions as ``<path relative to src/notebooklm>::<class>``.
#: The ``_sharing_records`` / ``_chat_records`` entries are legacy-*mapping*
#: records reachable only from ``_backend_compat`` and the projectors; the
#: ``_cookie_persistence`` / ``_auth`` entries are auth storage migration.
I9_EXEMPT_LEGACY_CLASSES: frozenset[str] = frozenset(
    {
        "_auth/profile_migration.py::LegacyAccount",
        "_auth/profile_migration.py::LegacyAccountContext",
        "_auth/profile_migration.py::LegacyAccountMigrator",
        "_auth/profile_migration.py::LegacyPromotionScheduler",
        "_auth/profile_migration.py::NoLegacyRecord",
        "_chat_records.py::ChatLegacyMappingRecord",
        "_chat_records.py::ChatLegacySequenceRecord",
        "_cookie_persistence.py::_LegacySnapshotAdapter",
        "_sharing_records.py::LegacyShareArtifactInput",
        "_sharing_records.py::LegacyShareArtifactResult",
    }
)

#: P10 deletion targets, recorded separately from the exemptions so neither can
#: be reclassified as permanent by editing one set. ``LegacyNoteBackedService``
#: goes in R4.2 (its wire graph moves above the port with the mind-map
#: workflows); ``NotebookLegacyRpc`` goes in R6.2 with ``NotebooksAPI.get_raw``.
I9_DELETION_TARGETS: frozenset[str] = frozenset(
    {
        "_note_service.py::LegacyNoteBackedService",
        "_notebooks.py::NotebookLegacyRpc",
    }
)


# --- AST helpers -------------------------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path) -> list[tuple[int, str, bool]]:
    """Every import in ``path`` as ``(lineno, dotted, first_party)``.

    ``dotted`` is relative to the ``notebooklm`` package for a first-party
    target (``from ..types import X`` inside ``_studio`` → ``types``) and the
    absolute module name otherwise (``httpx``). Relative imports are resolved
    against the file's own package, so a stdlib ``from types import
    MappingProxyType`` (``types``, third-party) is never confused with the
    public ``from .types import Source`` (``types``, first-party).

    A ``from <pkg> import <name>`` whose ``<name>`` resolves to a module on
    disk is reported under that module too, so the ``from . import types`` form
    cannot smuggle in a target the dotted form would flag.

    The walk covers every import statement, so one hidden inside
    ``if TYPE_CHECKING:`` is still reported — a type-only import still couples
    the service to the layer it names.
    """
    package_parts = list(path.relative_to(SRC_ROOT).parts[:-1])
    found: list[tuple[int, str, bool]] = []
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "notebooklm" or alias.name.startswith("notebooklm."):
                    found.append((node.lineno, alias.name.partition(".")[2], True))
                else:
                    found.append((node.lineno, alias.name, False))
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        module = node.module or ""
        if node.level == 0:
            if not (module == "notebooklm" or module.startswith("notebooklm.")):
                found.append((node.lineno, module, False))
                continue
            base_parts = module.split(".")[1:]
        else:
            base_parts = package_parts[: len(package_parts) - (node.level - 1)]
            base_parts = [*base_parts, module] if module else base_parts
        if base_parts:
            found.append((node.lineno, ".".join(base_parts), True))
        for alias in node.names:
            target = SRC_ROOT.joinpath(*base_parts, alias.name)
            if target.with_suffix(".py").is_file() or (target / "__init__.py").is_file():
                found.append((node.lineno, ".".join([*base_parts, alias.name]), True))
    return found


def _relative(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def _semantic_service_modules() -> tuple[Path, ...]:
    """Every semantic service module I1 governs, per the plan's definition."""
    root_services = sorted(SRC_ROOT.glob("_*_service.py"))
    assert root_services, "no root-level _*_service.py modules found; I1 discovery is broken"
    chat = [SRC_ROOT / "_chat" / name for name in ("service.py", "workflow.py")]
    chat_services = [path for path in chat if path.exists()]
    assert chat_services, "neither _chat/service.py nor _chat/workflow.py exists"
    studio = sorted(p for p in (SRC_ROOT / "_studio").glob("*.py") if p.name != "__init__.py")
    assert studio, "no _studio service modules found; I1 discovery is broken"
    return tuple(
        sorted(
            {
                *root_services,
                SRC_ROOT / "_read_services.py",
                SRC_ROOT / "_mutation_services.py",
                *chat_services,
                *studio,
            }
        )
    )


def _service_module_roots() -> frozenset[str]:
    """Dotted first-party roots of the semantic service modules (for I2)."""
    return frozenset(
        _relative(path, SRC_ROOT).removesuffix(".py").replace("/", ".").partition(".")[0]
        for path in _semantic_service_modules()
    )


def _is_i1_forbidden(dotted: str, first_party: bool) -> bool:
    if not first_party:
        return dotted == "httpx" or dotted.startswith("httpx.")
    return dotted.partition(".")[0] in I1_FORBIDDEN_FIRST_PARTY_ROOTS


def _i1_import_violations() -> dict[str, list[tuple[int, str]]]:
    """Service module → the forbidden imports it makes, as ``(lineno, dotted)``."""
    violations: dict[str, list[tuple[int, str]]] = {}
    for path in _semantic_service_modules():
        bad = sorted(
            {
                (lineno, dotted)
                for lineno, dotted, first_party in _imports(path)
                if _is_i1_forbidden(dotted, first_party)
            }
        )
        if bad:
            violations[_relative(path, SRC_ROOT)] = bad
    return violations


def _i2_domain_violations() -> dict[str, list[tuple[int, str]]]:
    """``_web`` file → the domain imports it makes, as ``(lineno, dotted)``."""
    forbidden = I2_FORBIDDEN_DOMAIN_PACKAGES | _service_module_roots()
    violations: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(WEB_ROOT.rglob("*.py")):
        bad = sorted(
            {
                (lineno, dotted)
                for lineno, dotted, first_party in _imports(path)
                if first_party and dotted.partition(".")[0] in forbidden
            }
        )
        if bad:
            violations[_relative(path, WEB_ROOT)] = bad
    return violations


def _legacy_classes(root: Path) -> dict[str, tuple[str, int]]:
    """``<relpath>::<class>`` → ``(relpath, lineno)`` for every ``Legacy`` class.

    Matched on *containment*, not prefix: ``NotebookLegacyRpc`` is one of the
    plan's two named deletion targets and a prefix match would miss it.
    """
    found: dict[str, tuple[str, int]] = {}
    for path in sorted(root.rglob("*.py")):
        relative = _relative(path, SRC_ROOT)
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.ClassDef) and "Legacy" in node.name:
                found[f"{relative}::{node.name}"] = (relative, node.lineno)
    return found


def _annotation_atoms(node: ast.expr) -> frozenset[str]:
    """Every type name a return annotation names, ``None`` included."""
    if isinstance(node, ast.Name):
        return frozenset({node.id})
    if isinstance(node, ast.Attribute):
        return frozenset({node.attr})
    if isinstance(node, ast.Subscript):
        return _annotation_atoms(node.value) | _annotation_atoms(node.slice)
    if isinstance(node, ast.BinOp):
        return _annotation_atoms(node.left) | _annotation_atoms(node.right)
    if isinstance(node, ast.Tuple | ast.List):
        return frozenset().union(*(_annotation_atoms(item) for item in node.elts), frozenset())
    if isinstance(node, ast.Constant):
        if node.value is None:
            return frozenset({"None"})
        if node.value is Ellipsis:
            return frozenset()
        if isinstance(node.value, str):
            return _annotation_atoms(ast.parse(node.value, mode="eval").body)
    return frozenset({ast.unparse(node)})


def _neutral_enum_names() -> frozenset[str]:
    """Enum classes defined in the neutral record modules."""
    modules = sorted(SRC_ROOT.glob("_*_records.py"))
    modules += [SRC_ROOT / name for name in sorted(NEUTRAL_RECORD_MODULE_NAMES)]
    names: set[str] = set()
    for path in modules:
        for node in _tree(path).body:
            if isinstance(node, ast.ClassDef) and any(
                base.id.endswith("Enum") for base in node.bases if isinstance(base, ast.Name)
            ):
                names.add(node.name)
    return frozenset(names)


def _public_model_names() -> frozenset[str]:
    return frozenset(notebooklm.__all__) | frozenset(public_types.__all__)


def _public_return_annotations(path: Path) -> list[tuple[str, str, frozenset[str]]]:
    """``(qualified name, unparsed annotation, atoms)`` per public callable."""
    found: list[tuple[str, str, frozenset[str]]] = []

    def record(qualname: str, fn: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        assert fn.returns is not None, f"{qualname} has no return annotation"
        found.append((qualname, ast.unparse(fn.returns), _annotation_atoms(fn.returns)))

    for node in _tree(path).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and not node.name.startswith(
            "_"
        ):
            record(node.name, node)
        elif isinstance(node, ast.ClassDef):
            for member in node.body:
                if isinstance(
                    member, ast.FunctionDef | ast.AsyncFunctionDef
                ) and not member.name.startswith("_"):
                    record(f"{node.name}.{member.name}", member)
    return found


# --- I1 ---------------------------------------------------------------------


def test_i1_seed_and_exemption_lists_are_disjoint_and_name_real_modules() -> None:
    assert not (I1_SEED_ALLOWLIST & I1_PERMANENT_EXEMPTIONS), (
        "a module is both a shrinking seed and a permanent exemption"
    )
    governed = {_relative(path, SRC_ROOT) for path in _semantic_service_modules()}
    ungoverned = sorted((I1_SEED_ALLOWLIST | I1_PERMANENT_EXEMPTIONS) - governed)
    assert not ungoverned, (
        f"allowlisted modules that are not semantic service modules: {ungoverned}"
    )


def test_no_new_semantic_service_module_imports_wire_public_or_projection_modules() -> None:
    """I1, growth half: nothing outside the seed may take a forbidden import."""
    violations = _i1_import_violations()
    unexpected = {
        module: found
        for module, found in violations.items()
        if module not in I1_SEED_ALLOWLIST and module not in I1_PERMANENT_EXEMPTIONS
    }
    assert not unexpected, (
        "semantic service module(s) took a forbidden import. A service may not "
        "name _projectors, notebooklm.types, _types.*, _backend_compat, rpc.*, "
        f"_row_adapters.*, _web.* or httpx (P10 invariant I1): {unexpected}"
    )


def test_the_i1_allowlist_carries_no_module_that_already_conforms() -> None:
    """I1, ratchet half: a seed that no longer violates must be removed."""
    violations = _i1_import_violations()
    stale = sorted(I1_SEED_ALLOWLIST - violations.keys())
    assert not stale, (
        "I1 seed entries that no longer violate the invariant — the allowlist "
        f"shrinks only, so delete them from I1_SEED_ALLOWLIST: {stale}"
    )
    assert sorted(I1_PERMANENT_EXEMPTIONS) == sorted(I1_PERMANENT_EXEMPTIONS & violations.keys()), (
        "the named permanent exemption no longer violates I1; retire it "
        "together with the download-transport slice it is reserved for"
    )


def test_conforming_semantic_services_return_only_neutral_types() -> None:
    """I1, return half: no public method leaks a public model out of a service."""
    permitted = I1_PERMITTED_RETURN_BUILTINS | _neutral_enum_names()
    public_models = _public_model_names()

    def is_neutral(atom: str) -> bool:
        if atom in permitted:
            return True
        # A private neutral record/result. The public-model exclusion matters:
        # ``AskResult`` and ``MindMapResult`` are exported models that the
        # bare suffix rule would otherwise wave through.
        return atom.endswith(("Record", "Result")) and atom not in public_models

    offenders: dict[str, list[tuple[str, str]]] = {}
    for path in _semantic_service_modules():
        module = _relative(path, SRC_ROOT)
        if module in I1_SEED_ALLOWLIST or module in I1_PERMANENT_EXEMPTIONS:
            continue
        bad = [
            (qualname, annotation)
            for qualname, annotation, atoms in _public_return_annotations(path)
            if not all(is_neutral(atom) for atom in atoms)
        ]
        if bad:
            offenders[module] = bad
    assert not offenders, (
        "semantic service method(s) return something other than a *Record / "
        "*Result, a neutral enum, a built-in scalar or collection thereof, or "
        f"None (P10 invariant I1): {offenders}"
    )


def test_the_neutral_return_vocabulary_is_discovered_not_empty() -> None:
    """Guard the discovery the return check depends on: it must find real enums."""
    enums = _neutral_enum_names()
    assert {"LabelKind"} <= enums, (
        f"neutral enum discovery lost its record modules; found {sorted(enums)}"
    )
    assert {"AskResult", "MindMapResult"} <= _public_model_names(), (
        "public-model discovery is broken; the *Result suffix rule would then "
        "admit public models into a service return type"
    )


# --- I2 ---------------------------------------------------------------------


def test_i2_seed_names_real_web_modules() -> None:
    web_files = {_relative(path, WEB_ROOT) for path in WEB_ROOT.rglob("*.py")}
    missing = sorted(I2_SEED_ALLOWLIST - web_files)
    assert not missing, f"I2 seed names files that do not exist: {missing}"


def test_no_new_web_module_imports_a_domain_package() -> None:
    """I2, growth half: ``_web`` consumes neutral records, never a domain."""
    violations = _i2_domain_violations()
    unexpected = {
        module: found for module, found in violations.items() if module not in I2_SEED_ALLOWLIST
    }
    assert not unexpected, (
        "_web module(s) imported a domain package or semantic service. The web "
        "backend owns wire encode/decode and consumes neutral records only "
        f"(P10 invariant I2): {unexpected}"
    )


def test_the_i2_allowlist_carries_no_module_that_already_conforms() -> None:
    """I2, ratchet half: a seed that no longer violates must be removed."""
    stale = sorted(I2_SEED_ALLOWLIST - _i2_domain_violations().keys())
    assert not stale, (
        "I2 seed entries that no longer import a domain package — the "
        f"allowlist shrinks only, so delete them from I2_SEED_ALLOWLIST: {stale}"
    )


def test_i2_leaves_the_neutral_helper_direction_open() -> None:
    """The domain rule must not be widened into a neutral-record ban."""
    forbidden = I2_FORBIDDEN_DOMAIN_PACKAGES | _service_module_roots()
    assert not (I2_PERMITTED_NEUTRAL_HELPERS & forbidden), (
        "a neutral helper was classified as a domain package: "
        f"{sorted(I2_PERMITTED_NEUTRAL_HELPERS & forbidden)}"
    )
    imported = {
        dotted.partition(".")[0]
        for path in WEB_ROOT.rglob("*.py")
        for _, dotted, first_party in _imports(path)
        if first_party
    }
    vanished = sorted(I2_PERMITTED_NEUTRAL_HELPERS - imported)
    assert not vanished, (
        "a permitted neutral helper is no longer imported by _web; confirm it "
        f"was not renamed out from under this assertion: {vanished}"
    )


# --- I9 ---------------------------------------------------------------------


def test_no_unenumerated_legacy_class_exists_below_the_application_layer() -> None:
    """I9: the ``Legacy`` inventory below ``_app`` is exactly the enumerated set."""
    found = _legacy_classes(SRC_ROOT)
    below_app = {key: where for key, where in found.items() if not where[0].startswith("_app/")}
    assert set(below_app) == I9_EXEMPT_LEGACY_CLASSES | I9_DELETION_TARGETS, (
        "the Legacy class inventory below _app drifted (P10 invariant I9). "
        f"Unenumerated: {sorted(set(below_app) - I9_EXEMPT_LEGACY_CLASSES - I9_DELETION_TARGETS)}; "
        f"gone: {sorted((I9_EXEMPT_LEGACY_CLASSES | I9_DELETION_TARGETS) - set(below_app))}"
    )


def test_the_application_layer_defines_no_legacy_class_at_all() -> None:
    assert APP_ROOT.is_dir(), "the _app package moved; I9's scope split is stale"
    assert not _legacy_classes(APP_ROOT), (
        "_app is the transport-neutral business-logic layer and carries no "
        f"legacy compatibility types: {sorted(_legacy_classes(APP_ROOT))}"
    )


def test_the_i9_deletion_targets_stay_separate_from_the_exemptions() -> None:
    assert not (I9_EXEMPT_LEGACY_CLASSES & I9_DELETION_TARGETS), (
        "a P10 deletion target was reclassified as a permanent I9 exemption"
    )


# --- Read-core pins (ported from the retired test_semantic_read_boundary.py) --

_PROJECTORS = SRC_ROOT / "_projectors.py"
_SERVICES = SRC_ROOT / "_read_services.py"
_MUTATION_SERVICES = SRC_ROOT / "_mutation_services.py"

_FORBIDDEN_MODULE_PARTS = frozenset(
    {
        "_row_adapters",
        "_web",
        "cli",
        "httpx",
        "mcp",
        "rpc",
        "server",
    }
)
_FORBIDDEN_IDENTIFIERS = frozenset(
    {
        "NotebookLMClient",
        "RPCMethod",
        "RpcCaller",
        "SourceRow",
        "ProjectRow",
    }
)


def _imported_modules(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            modules.add(node.module)
    return modules


def _identifiers(path: Path) -> set[str]:
    return {node.id for node in ast.walk(_tree(path)) if isinstance(node, ast.Name)}


def test_read_services_depend_only_on_semantic_port_records_deadline_and_projectors() -> None:
    assert _imported_modules(_SERVICES) <= {
        "__future__",
        "builtins",
        "typing",
        "_backend",
        "_deadline",
        "_projectors",
        "_records",
        "types",
    }
    assert not (_identifiers(_SERVICES) & _FORBIDDEN_IDENTIFIERS)


def test_read_core_has_no_transport_wire_or_adapter_dependencies() -> None:
    for path in (_MUTATION_SERVICES, _PROJECTORS, _SERVICES):
        assert not {
            module
            for module in _imported_modules(path)
            if any(part in _FORBIDDEN_MODULE_PARTS for part in module.split("."))
        }
        assert not (_identifiers(path) & _FORBIDDEN_IDENTIFIERS)


def test_url_mutation_service_depends_only_on_semantic_port_deadline_and_records() -> None:
    assert _imported_modules(_MUTATION_SERVICES) <= {
        "__future__",
        "_backend",
        "_deadline",
        "_records",
    }
    assert not any(isinstance(node, ast.Subscript) for node in ast.walk(_tree(_MUTATION_SERVICES)))


def test_projectors_use_normal_public_constructors_without_wire_factories() -> None:
    tree = _tree(_PROJECTORS)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]

    assert {node.func.id for node in calls if isinstance(node.func, ast.Name)} >= {
        "Notebook",
        "Source",
    }
    assert not {
        node.func.attr
        for node in calls
        if isinstance(node.func, ast.Attribute)
        and node.func.attr in {"from_api_response", "from_row"}
    }


def test_read_core_remains_private_and_does_not_expand_public_package_exports() -> None:
    assert projector_module.__name__ == "notebooklm._projectors"
    assert service_module.__name__ == "notebooklm._read_services"
    assert not (set(projector_module.__all__) | set(service_module.__all__)) & set(
        notebooklm.__all__
    )
