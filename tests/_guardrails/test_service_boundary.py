"""Service-boundary guardrails for programme P10 (invariants I1, I2, I9).

Governed by :doc:`ADR-0035 <../../docs/adr/0035-semantic-backend-boundary>` and
``docs/plan/2026-08-25-p10-semantic-remediation.md`` (the P10 remediation
plan). Three of that plan's target invariants are enforced here, and all
three are now **hard rules**: their migrations are finished, so there is no
seed allowlist left to park a new violation in and a breach fails on the commit
that introduces it. I1's only escape is the single *named permanent* exemption
below, and widening it is an ADR-level decision (ADR-0035 addendum **D7**)
rather than a test edit; I9's only escape is its enumerated set; I2 has none at
all.

**I1 — semantic service modules stay neutral.** A semantic service module
(``src/notebooklm/_*_service.py``, ``_read_services.py``,
``_chat/service.py`` — later ``_chat/workflow.py`` — and ``_studio/*.py``) may
import none of ``_projectors``,
``notebooklm.types``, ``_types.*``, ``_backend_compat``, ``rpc.*``,
``_row_adapters.*``, ``_web.*`` or ``httpx``, and its public methods must
return ``*Record`` / ``*Result`` types, neutral enums, built-in scalars or
collections thereof, or ``None``. Projection to public models is a *facade*
responsibility — the ADR's Decision lists "public model projectors" among the
dependencies a service may take, so I1 is authorised by the plan's decision
**D7** (an ADR-0035 addendum landed in R0.0), not by the unamended ADR.
Since R6.5 this is a hard rule with exactly one named exemption,
``_studio/downloads.py``.

**I2 — ``_web/**`` imports no domain package.** The web backend may not import
``_chat``, ``_source``, ``_studio``, ``_artifact`` or any
semantic service module. Neutral helper modules (``_records*``,
``_research_neutral``, ``_deadline``, ``_request_types``, ``_markdown``) stay
permitted and are asserted as such below, so the rule cannot be widened into
one that forbids the neutral direction too. **Met.** The seed drained to empty
and R6.5's successor slice deleted it, so this is a hard rule with no exemption
of any kind; see the I2 section below for what the last entry was.

**I9 — no ``Legacy*`` class below ``_app``** except the enumerated exemptions:
the legacy-mapping records consumed only by ``_backend_compat``/the projectors
and the auth storage-migration types. **Met.** Both deletion targets P10 owned
are gone — ``LegacyNoteBackedService`` in R4.2, and ``NotebookLegacyRpc`` in
R6.2 when ``NotebooksAPI.get_raw`` moved onto the ``NOTEBOOK_GET`` row — so
R6.5 deleted the separate deletion-target set rather than leave an empty one
standing as somewhere to reclassify a new violation into. The two stay named in
:data:`I9_DELETED_TARGETS`, which fails if either class comes back.

**I10 is deliberately not enforced here.** The plan's tenth invariant caps
``src/notebooklm/_records.py`` at 1,500 lines; that is already
:mod:`tests._guardrails.test_module_size_ratchet`'s job — ``_records.py``
measures exactly ``MODULE_SIZE_BUDGET`` lines and is not in
``ALLOWLISTED_CEILINGS``, so ``test_no_module_exceeds_the_size_budget`` fails
on the first line of growth. Duplicating it here would create a second
authority for one ceiling.

This module also carries the five assertions of the retired
``test_semantic_read_boundary.py``. I1 subsumes that guard's *intent* but not
all of its checks: it pinned the exact import sets of ``_read_services.py``
and ``_mutation_services.py`` (an allowlist, tighter than I1's forbidden-list
rule), and it also constrained ``_projectors.py``, which is not a service
module at all. Those checks are ported below under "Read-core pins" so no
assertion is lost; the ``_mutation_services.py`` half went away with the module
itself in R3.3, and the ``_read_services.py`` set was *narrowed* in R6.1 when
that module left the I1 seed.
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

#: The one **permanent, named** exemption to I1 — deliberately not a seed, and
#: not a place to park the next violation. ``_studio/downloads.py`` owns the
#: byte-download clients (``httpx`` plus ``_curl_cffi_transport``); relocating
#: that transport under ``_web`` is explicitly out of P10 scope and is deferred
#: to a separate download-transport slice (plan §0, §8), which is also when this
#: entry retires.
#:
#: There is no other escape. P10's shrink-only seed drained to empty — R6.1-R6.6
#: took the root services and R5.2 the four ``_studio/*`` modules (``catalog``,
#: ``lifecycle``, ``mind_maps``, ``representations``) — and R6.5 deleted it, so
#: I1 is a hard rule. Adding an entry here is an ADR-level decision, not a test
#: edit: ADR-0035 addendum D7 is what authorises I1, and it names "the
#: byte-download clients" as the exemption it carries, so a second exemption
#: needs a second addendum.
I1_PERMANENT_EXEMPTIONS: frozenset[str] = frozenset({"_studio/downloads.py"})

#: Governed by I1's *import* half only. ``_source_add_reports.py`` holds no
#: service methods: it is the neutral failure-report vocabulary P10 R3.4 split
#: out of ``_source_service.py``, and its constructors return the port's own
#: ``BackendError`` — which services *raise* rather than return, so the return
#: half's neutral vocabulary deliberately excludes it. Keeping the module under
#: the import half is the point of listing it at all; widening the return
#: vocabulary to admit ``BackendError`` would weaken the check for every real
#: service.
I1_RETURN_ARM_EXEMPTIONS: frozenset[str] = frozenset({"_source_add_reports.py"})

#: Built-in scalars and collection constructors a neutral service may name in a
#: return annotation. Deliberately minimal: widening it is a reviewed change,
#: which is the point of the invariant.
#:
#: ``object`` was added in R6.6 for the two raw-row compatibility listings
#: ``NoteService.list_mind_map_rows`` / ``list_note_rows``, whose elements are
#: undecoded wire rows the frozen public ``NotesAPI.list_mind_maps ->
#: list[Any]`` contract republishes verbatim. It is the opaque top type, not
#: the unchecked ``Any``: a service may say "this element has no known type",
#: which the already-permitted bare ``list`` / ``dict`` spellings say too, but
#: it still may not annotate a return ``Any`` and wave anything through.
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
        "object",
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
    {"_artifact", "_chat", "_source", "_studio"}
)

# I2 has no allowlist. The shrink-only seed it used to carry drained entry by
# entry — ``codec/chat_stream.py`` and ``codec/chat.py`` in R2.1 (the codec took
# the streamed-ask wire and emits records), ``backend.py`` once R2.3 drained its
# ``_chat`` edge and R3.1 put its ``_source.upload`` edge behind
# ``_source_upload_port``, ``bindings/mind_maps.py`` in R4.2 (the note-backed
# generation and the catalog merge moved above the port) — and
# ``bindings/sources.py`` went last.
#
# That final edge did not hoist. It was ``honor_requested_title``, the
# ``SOURCE_ADD_FILE`` row's post-add rename, and that row stays custom
# permanently under decision D4, so there was nothing above the port to move it
# to. Nor was it neutral: it takes and returns a public ``Source`` and catches
# the raw ``RPCError``/``NetworkError`` families, all three of which a neutral
# module is forbidden to name, so parking it in one would have recorded the
# coupling as compliance rather than removing it. It moved *down* instead, into
# ``_web/bindings/sources.py`` beside the one row that calls it, where that
# vocabulary is already licensed; ``SourceService._honor_requested_title`` owns
# the record-based contract above the port. ``_source/add.py`` held nothing
# else and was deleted with it.
#
# So there is no seed to add to. A ``_web`` module that takes a domain import
# fails on the commit that introduces it, and the fix is to move the dependency,
# not to widen this file.

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

#: Deleted by their owning slice, kept named so a reintroduction under an old
#: name is a visible edit here rather than a silent revival.
#:
#: These were P10's two I9 deletion targets, and both are gone:
#: ``LegacyNoteBackedService`` in R4.2 (its wire graph moved above the port with
#: the mind-map workflows, and ``_mind_map.py``, the module R4.1 had moved it
#: to, went with it) and ``NotebookLegacyRpc`` in R6.2 (``NotebooksAPI.get_raw``
#: reads through the ``NOTEBOOK_GET`` row, so the facade needs no raw-call
#: collaborator at all). R6.5 therefore deleted the separate deletion-target
#: set: an empty "targets" list is indistinguishable from a parking space, and
#: I9 now stands as a hard rule over the enumerated exemptions alone.
I9_DELETED_TARGETS: frozenset[str] = frozenset(
    {
        "_mind_map.py::LegacyNoteBackedService",
        "_notebooks.py::NotebookLegacyRpc",
    }
)


# --- AST helpers -------------------------------------------------------------


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _imports(path: Path, src_root: Path = SRC_ROOT) -> list[tuple[int, str, bool]]:
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
    package_parts = list(path.relative_to(src_root).parts[:-1])
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
            target = src_root.joinpath(*base_parts, alias.name)
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
                # P10 R3.4 split the source-add family's neutral report
                # vocabulary out of ``_source_service.py`` to keep it inside the
                # module-size budget. It is service code by every other measure,
                # so I1 governs it too — otherwise the split would have moved
                # the workflows' vocabulary out from under the guard that keeps
                # it transport-neutral.
                SRC_ROOT / "_source_add_reports.py",
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


def _is_i2_forbidden(dotted: str, first_party: bool) -> bool:
    """Whether one import names a domain package or a semantic service module."""
    forbidden = I2_FORBIDDEN_DOMAIN_PACKAGES | _service_module_roots()
    return first_party and dotted.partition(".")[0] in forbidden


def _i2_domain_violations(
    web_root: Path = WEB_ROOT, src_root: Path = SRC_ROOT
) -> dict[str, list[tuple[int, str]]]:
    """``_web`` file → the domain imports it makes, as ``(lineno, dotted)``.

    The roots are parameters only so the non-vacuity test can run this exact
    walk over a synthetic tree that *does* violate: I2 has no violation left in
    live source and no seed whose entries would prove the detector still fires.
    """
    violations: dict[str, list[tuple[int, str]]] = {}
    for path in sorted(web_root.rglob("*.py")):
        bad = sorted(
            {
                (lineno, dotted)
                for lineno, dotted, first_party in _imports(path, src_root)
                if _is_i2_forbidden(dotted, first_party)
            }
        )
        if bad:
            violations[_relative(path, web_root)] = bad
    return violations


def _legacy_classes(root: Path) -> dict[str, tuple[str, int]]:
    """``<relpath>::<class>`` → ``(relpath, lineno)`` for every ``Legacy`` class.

    Matched on *containment*, not prefix: R6.2's deletion target was named
    ``NotebookLegacyRpc``, and a prefix match would have missed it.
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


def test_the_i1_permanent_exemption_names_a_governed_service_module() -> None:
    governed = {_relative(path, SRC_ROOT) for path in _semantic_service_modules()}
    ungoverned = sorted(I1_PERMANENT_EXEMPTIONS - governed)
    assert not ungoverned, f"exempted modules that are not semantic service modules: {ungoverned}"


def test_no_semantic_service_module_imports_wire_public_or_projection_modules() -> None:
    """I1, import half — a hard rule: only the named exemption may violate it."""
    violations = _i1_import_violations()
    unexpected = {
        module: found
        for module, found in violations.items()
        if module not in I1_PERMANENT_EXEMPTIONS
    }
    assert not unexpected, (
        "semantic service module(s) took a forbidden import. A service may not "
        "name _projectors, notebooklm.types, _types.*, _backend_compat, rpc.*, "
        "_row_adapters.*, _web.* or httpx (P10 invariant I1). There is no seed "
        "allowlist to add to: hoist the dependency above the port, or take an "
        f"ADR-0035 addendum for a second permanent exemption: {unexpected}"
    )


def test_the_i1_permanent_exemption_is_exactly_what_still_needs_it() -> None:
    """An exemption must name a module that would otherwise fail the hard rule."""
    violations = _i1_import_violations()
    assert sorted(I1_PERMANENT_EXEMPTIONS) == sorted(I1_PERMANENT_EXEMPTIONS & violations.keys()), (
        "the named permanent exemption no longer violates I1; retire it "
        "together with the download-transport slice it is reserved for"
    )


def test_i1_governs_every_service_family_and_its_detector_fires() -> None:
    """Non-vacuity: the scan reaches real modules and the predicate really bites.

    A boundary rule that scans nothing passes for free, and this repo has
    shipped exactly that before — a ``_chat/`` import ban whose scan never
    covered ``_chat/``. A hard rule earns less scrutiny than a ratchet (there is
    no shrinking seed whose entries prove it fires), so pin three things: the
    governed set holds a module from every family I1 names, the forbidden
    predicate answers correctly for each banned root *and* for the neutral names
    a service must keep reaching, and the end-to-end walk still finds a real
    forbidden import in live source.
    """
    governed = {_relative(path, SRC_ROOT) for path in _semantic_service_modules()}
    assert {
        "_note_service.py",
        "_read_services.py",
        "_source_add_reports.py",
        "_studio/catalog.py",
    } <= governed
    assert any(module.startswith("_chat/") for module in governed), (
        "no _chat service module is governed; I1's chat arm would be vacuous"
    )

    for root in I1_FORBIDDEN_FIRST_PARTY_ROOTS:
        assert _is_i1_forbidden(root, True), root
        assert _is_i1_forbidden(f"{root}.submodule", True), root
    assert _is_i1_forbidden("httpx", False)
    assert _is_i1_forbidden("httpx._client", False)
    for neutral in ("_backend", "_deadline", "_operations", "_records"):
        assert not _is_i1_forbidden(neutral, True), neutral
    # ``types`` is forbidden only as the first-party public module; the stdlib
    # module of the same name has to stay reachable.
    assert not _is_i1_forbidden("types", False)

    assert _i1_import_violations(), (
        "the I1 walk found no forbidden import anywhere in src, not even in the "
        "named exemption — the detector has stopped firing on live source"
    )


def test_conforming_semantic_services_return_only_neutral_types() -> None:
    """I1, return half — a hard rule too: no service leaks a public model out."""
    permitted = I1_PERMITTED_RETURN_BUILTINS | _neutral_enum_names()
    public_models = _public_model_names()

    def is_neutral(atom: str) -> bool:
        if atom in permitted:
            return True
        # A private neutral record/result/operation input. ``*Input`` joined the
        # vocabulary in P10 R5.1a: a service that resolves a port input above the
        # port (``StudioGenerationInputs``) returns one, and those records live in
        # the same neutral modules as ``*Record``. The public-model exclusion
        # matters: ``AskResult`` and ``MindMapResult`` are exported models that
        # the bare suffix rule would otherwise wave through.
        return atom.endswith(("Input", "Record", "Result")) and atom not in public_models

    offenders: dict[str, list[tuple[str, str]]] = {}
    for path in _semantic_service_modules():
        module = _relative(path, SRC_ROOT)
        if module in I1_PERMANENT_EXEMPTIONS or module in I1_RETURN_ARM_EXEMPTIONS:
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


def test_the_return_arm_exemption_is_exactly_what_still_needs_it() -> None:
    """A return-arm exemption must name a module that would otherwise fail it."""
    permitted = I1_PERMITTED_RETURN_BUILTINS | _neutral_enum_names()
    public_models = _public_model_names()
    governed = {_relative(path, SRC_ROOT) for path in _semantic_service_modules()}
    assert governed >= I1_RETURN_ARM_EXEMPTIONS, (
        "a return-arm exemption names a module I1 does not govern: "
        f"{sorted(I1_RETURN_ARM_EXEMPTIONS - governed)}"
    )
    for module in sorted(I1_RETURN_ARM_EXEMPTIONS):
        annotations = _public_return_annotations(SRC_ROOT / module)
        assert any(
            atom not in permitted
            and not (atom.endswith(("Record", "Result")) and atom not in public_models)
            for _qualname, _annotation, atoms in annotations
            for atom in atoms
        ), f"{module} no longer needs its return-arm exemption; drop it"


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


def test_no_web_module_imports_a_domain_package() -> None:
    """I2 — a hard rule: ``_web`` consumes neutral records, never a domain."""
    violations = _i2_domain_violations()
    assert not violations, (
        "_web module(s) imported a domain package or semantic service. The web "
        "backend owns wire encode/decode and consumes neutral records only "
        "(P10 invariant I2). There is no seed allowlist to add to: move the "
        "dependency — above the port if the workflow belongs there, or down "
        "into _web beside the row that reaches it if it already speaks the "
        f"wire's vocabulary: {violations}"
    )


def test_i2_governs_the_whole_web_tree_and_its_detector_fires(tmp_path: Path) -> None:
    """Non-vacuity: the scan reaches every ``_web`` family and the walk bites.

    A boundary rule that scans nothing passes for free, and this repo has
    shipped exactly that before — a ``_chat/`` import ban whose scan never
    covered ``_chat/``. I2 is now the most exposed of the three: it has no
    violation left anywhere in live source and no shrinking seed whose entries
    would prove it still fires, so *nothing* real exercises it. Pin the scan
    scope, the forbidden predicate, and an end-to-end walk over a synthetic
    ``_web`` tree that does violate.
    """
    scanned = {_relative(path, WEB_ROOT) for path in WEB_ROOT.rglob("*.py")}
    assert {"backend.py", "registry.py", "transport.py"} <= scanned
    for family in ("bindings/", "codec/"):
        assert any(name.startswith(family) for name in scanned), (
            f"no _web/{family} module is scanned; I2 would be vacuous there"
        )

    forbidden = I2_FORBIDDEN_DOMAIN_PACKAGES | _service_module_roots()
    assert forbidden >= I2_FORBIDDEN_DOMAIN_PACKAGES
    assert "_source_service" in forbidden, (
        "semantic service discovery lost the source service; I2's service arm "
        "would stop naming the modules the plan wrote it for"
    )
    for root in sorted(forbidden):
        assert _is_i2_forbidden(root, True), root
        assert _is_i2_forbidden(f"{root}.submodule", True), root
    for neutral in sorted(I2_PERMITTED_NEUTRAL_HELPERS):
        assert not _is_i2_forbidden(neutral, True), neutral
    # A third-party distribution that happened to share a name is not first
    # party, so the predicate must not fire on it.
    assert not _is_i2_forbidden("_chat", False)

    # End-to-end: the same walk, over a tree laid out exactly like src/notebooklm.
    web_root = tmp_path / "_web"
    (web_root / "bindings").mkdir(parents=True)
    (web_root / "clean.py").write_text(
        "from .._records import SourceRecord\nfrom .._deadline import RuntimeDeadline\n",
        encoding="utf-8",
    )
    (web_root / "bindings" / "offender.py").write_text(
        "from ..._source.upload import SourceUploadPipeline\n"
        "from ..._source_service import SourceService\n",
        encoding="utf-8",
    )
    assert _i2_domain_violations(web_root, tmp_path) == {
        "bindings/offender.py": [(1, "_source.upload"), (2, "_source_service")],
    }, "the I2 walk no longer reports a domain import it is handed"


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
    assert set(below_app) == I9_EXEMPT_LEGACY_CLASSES, (
        "the Legacy class inventory below _app drifted (P10 invariant I9). There "
        "is no deletion-target set left to park a new one in. Unenumerated: "
        f"{sorted(set(below_app) - I9_EXEMPT_LEGACY_CLASSES)}; "
        f"gone: {sorted(I9_EXEMPT_LEGACY_CLASSES - set(below_app))}"
    )


def test_the_application_layer_defines_no_legacy_class_at_all() -> None:
    assert APP_ROOT.is_dir(), "the _app package moved; I9's scope split is stale"
    assert not _legacy_classes(APP_ROOT), (
        "_app is the transport-neutral business-logic layer and carries no "
        f"legacy compatibility types: {sorted(_legacy_classes(APP_ROOT))}"
    )


def test_no_legacy_class_p10_deleted_returns_under_its_old_name() -> None:
    assert not (I9_EXEMPT_LEGACY_CLASSES & I9_DELETED_TARGETS), (
        "a deleted P10 target came back as a permanent I9 exemption"
    )
    live_class_names = {key.partition("::")[2] for key in _legacy_classes(SRC_ROOT)}
    for target in I9_DELETED_TARGETS:
        _module, _, class_name = target.partition("::")
        assert class_name not in live_class_names, (
            f"{target} was deleted in P10; {class_name} is back"
        )


# --- Read-core pins (ported from the retired test_semantic_read_boundary.py) --

_PROJECTORS = SRC_ROOT / "_projectors.py"
_SERVICES = SRC_ROOT / "_read_services.py"

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


def test_read_services_depend_only_on_semantic_port_records_and_deadline() -> None:
    """The read services import strictly less than the retired pin allowed.

    R6.1 moved projection up to ``NotebooksAPI`` / ``SourcesAPI``, so
    ``_projectors`` and the public ``types`` module leave this set, and with
    them the ``typing.TYPE_CHECKING`` block that guarded the public model
    names — a tightening of the ported pin, not a loosening. ``builtins``
    stays: ``get_source_ids`` still spells its return ``builtins.list[str]``
    to name the built-in rather than the sibling ``list`` method.
    """
    assert _imported_modules(_SERVICES) <= {
        "__future__",
        "builtins",
        "_backend",
        "_deadline",
        "_records",
    }
    assert not (_identifiers(_SERVICES) & _FORBIDDEN_IDENTIFIERS)


def test_read_core_has_no_transport_wire_or_adapter_dependencies() -> None:
    for path in (_PROJECTORS, _SERVICES):
        assert not {
            module
            for module in _imported_modules(path)
            if any(part in _FORBIDDEN_MODULE_PARTS for part in module.split("."))
        }
        assert not (_identifiers(path) & _FORBIDDEN_IDENTIFIERS)


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
