"""Fail-closed inventory audit for Phase 8 (P8): the web cookie-provider boundary.

Governed by ADR-0035 and docs/plan/2026-08-13-semantic-backend-refactor.md (P8).
P8 extracts credential acquisition and persistence out of one open web backend
session and behind a ``WebCookieProvider``. Its acceptance criteria are
*negative* about the backend and *conservative* about everything else:

- the backend does not read profile files or launch interactive authentication;
- an injected provider is caller-owned, a convenience factory closes only what
  it created;
- profile file paths, locking, CAS, atomic writes, permissions, account routing,
  and secret redaction are unchanged unless separately reviewed;
- interactive login, browser-cookie capture, doctor, and profile management stay
  outside the backend;
- existing profile storage / refresh / recovery / master-token work is *adapted*
  behind the provider, never duplicated.

Every one of those is an inventory claim about who owns what today, so this
module pins the post-P8 inventories and fails closed when they drift. The
former ``test_p8_provider_is_not_defined_yet`` tripwire fired when the port was
introduced; its replacement below requires the exact provider/generation
definitions and re-derived backend/auth ownership sets.

Runtime behaviour that P8 must equality-preserve is characterized separately in
``tests/unit/test_semantic_p8_provider_characterization.py``.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, fields
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC_ROOT = REPO_ROOT / "src" / "notebooklm"
WEB_ROOT = SRC_ROOT / "_web"
CHAT_STREAM_REQUEST_PATH = SRC_ROOT / "_chat" / "stream_request.py"
WEB_REQUEST_AUTH_PATH = SRC_ROOT / "_web_request_auth.py"
SOURCE_UPLOAD_PATH = SRC_ROOT / "_source" / "upload.py"
DRIVE_IMPORT_PATH = SRC_ROOT / "_source" / "drive_import.py"
PROVIDER_PATH = SRC_ROOT / "_web_cookie_provider.py"
RUNTIME_PROVIDER_PATH = SRC_ROOT / "_runtime" / "web_cookie_provider.py"
BACKEND_SESSION_PATH = SRC_ROOT / "_runtime" / "web_backend_session.py"
STORAGE_ADAPTER_PATH = SRC_ROOT / "_auth" / "web_provider_storage.py"
REFRESH_ADAPTER_PATH = SRC_ROOT / "_auth" / "web_provider_refresh.py"

pytestmark = pytest.mark.repo_lint

# --- P8 target symbols -------------------------------------------------------

#: The exact provider types P8 introduced; duplicate definitions are forbidden.
P8_PROVIDER_SYMBOLS: frozenset[str] = frozenset(
    {
        "RuntimeWebCookieProvider",
        "WebBackendSession",
        "WebCookieGeneration",
        "WebCookieProvider",
        "WebCookieSession",
        "WebCookieSessionState",
    }
)
EXPECTED_P8_PROVIDER_DEFINITIONS: frozenset[str] = frozenset(
    {
        "_runtime/web_backend_session.py::WebBackendSession",
        "_runtime/web_cookie_provider.py::RuntimeWebCookieProvider",
        "_web_cookie_provider.py::WebCookieGeneration",
        "_web_cookie_provider.py::WebCookieProvider",
        "_web_cookie_provider.py::WebCookieSession",
        "_web_cookie_provider.py::WebCookieSessionState",
    }
)

# --- Backend package inventory ----------------------------------------------

#: Exact set of first-party ``notebooklm.*`` modules the web backend package
#: imports today, as dotted names relative to ``notebooklm``. Every name below
#: is a wire/record/policy dependency or one of the two provider ports; none is
#: a credential-acquisition dependency.
KNOWN_WEB_PACKAGE_FIRST_PARTY_IMPORTS: frozenset[str] = frozenset(
    {
        "_artifact.formatters",
        "_artifact.payloads",
        "_auth_refresh_retry",
        "_backend",
        "_binding",
        "_chat",
        "_chat.stream_decode",
        "_chat.stream_request",
        "_client_metrics",
        "_deadline",
        "_env",
        "_idempotency",
        "_logging",
        "_runtime.pipeline",
        "_mind_map",
        "_note_service",
        "_notebook_payloads",
        "_operations",
        "_projectors",
        "_records",
        "_research_neutral",
        "_reqid_counter",
        "_request_types",
        "_row_adapters.artifacts",
        "_row_adapters.chat",
        "_row_adapters.documents",
        "_row_adapters.labels",
        "_row_adapters.notebooks",
        "_row_adapters.notes",
        "_row_adapters.research",
        "_row_adapters.sources",
        "_runtime.config",
        "_runtime.contracts",
        "_runtime.transport",
        "_source.add",
        "_source.batch",
        "_source.markdown",
        "_source.upload",
        "_transport_drain",
        "_transport_errors",
        "_types.documents",
        "_types.sources",
        "_url_utils",
        "_web.backend",
        "_web.bindings",
        "_web.bindings.labels",
        "_web.bindings.mind_maps",
        "_web.bindings.notes",
        "_web.bindings.research",
        "_web.bindings.settings",
        "_web.bindings.sharing",
        "_web.bindings.sources",
        "_web.chat",
        "_web.chat_transport",
        "_web.codec",
        "_web.codec.artifacts",
        "_web.codec.chat",
        "_web.codec.chat_saved_note",
        "_web.codec.chat_stream",
        "_web.codec.collections",
        "_web.codec.documents",
        "_web.codec.labels",
        "_web.codec.mind_maps",
        "_web.codec.notebooks",
        "_web.codec.notes",
        "_web.codec.research",
        "_web.codec.settings",
        "_web.codec.sharing",
        "_web.codec.sources",
        "_web.codec.studio_documents",
        "_web.codec.suggestions",
        "_web.deadline_rpc",
        "_web.deadlines",
        "_web.error_policy",
        "_web.errors",
        "_web.failure_projection",
        "_web.labels",
        "_web.policy",
        "_web.registry",
        "_web.runtime",
        "_web.sharing",
        "_web.settings_suggestions",
        "_web.source_variants",
        "_web.studio_data",
        "_web.studio_documents",
        "_web.studio_facade",
        "_web.studio_media",
        "_web.transport",
        "_web_cookie_provider",
        "_web_request_auth",
        "exceptions",
        "rpc",
        "rpc._safe_index",
        "rpc.decoder",
        "rpc.types",
        "types",
    }
)

#: Import prefixes that would mean the backend acquires or persists credentials
#: itself. Even pure account-route formatting is materialized outside ``_web``;
#: every ``_auth`` owner and both concrete runtime adapters must stay behind
#: the injected provider/session ports.
FORBIDDEN_WEB_IMPORT_PREFIXES: tuple[str, ...] = (
    "_app",
    "_atomic_io",
    "_auth",
    "_cookie_persistence",
    "_kernel",
    "_runtime.auth",
    "_runtime.init",
    "_runtime.lifecycle",
    "_runtime.web_backend_session",
    "_runtime.web_cookie_provider",
    "auth",
    "cli",
    "io",
    "paths",
)

#: Identifiers that would mean the backend names credential material directly.
#: Checked at AST identifier granularity, not by substring, so prose like
#: "the only timeout authority" does not false-fire.
CREDENTIAL_IDENTIFIERS: frozenset[str] = frozenset(
    {
        "account_email",
        "account_route",
        "authuser",
        "cookie_jar",
        "cookie_snapshot",
        "cookies",
        "csrf_token",
        "master_token",
        "session_id",
        "storage_path",
        "storage_state",
    }
)

#: Credential materialization lives outside ``_web``. The backend consumes the
#: opaque builder and provider ports, so no module in the package needs to name
#: a credential field.
EXPECTED_WEB_CREDENTIAL_IDENTIFIERS: dict[str, frozenset[str]] = {}

#: Existing auth/persistence owners must not be retained as backend attributes
#: or types. A provider composes these accepted owners; merely renaming an
#: import while keeping the capability inside ``_web`` must still fail.
FORBIDDEN_WEB_OWNER_ATTRIBUTES: frozenset[str] = frozenset(
    {"_auth", "_auth_coord", "_auth_refresh", "_cookie_persistence", "_lifecycle", "auth"}
)
FORBIDDEN_WEB_OWNER_TYPES: frozenset[str] = frozenset(
    {"AuthRefreshCoordinator", "AuthTokens", "ClientLifecycle", "CookiePersistence"}
)

#: Ordinary RPC credential-to-wire materialization is one narrow transitive
#: adapter outside ``_web``. It may format an account route but may not acquire,
#: refresh, persist, or interactively re-mint credentials.
KNOWN_WEB_REQUEST_AUTH_IMPORTS: frozenset[str] = frozenset(
    {"_auth.account", "_env", "_web_cookie_provider", "rpc"}
)
KNOWN_WEB_REQUEST_AUTH_CREDENTIAL_IDENTIFIERS: frozenset[str] = frozenset(
    {"account_email", "authuser", "csrf_token", "session_id"}
)

#: Upload and Drive have direct HTTP legs outside the RPC backend. Their exact
#: credential surface is limited to immutable provider values, pure account
#: formatting, and value-copying the immutable ``CookieJar`` compatibility
#: fallback. They may not import any credential acquisition owner.
DIRECT_LEG_CREDENTIAL_IMPORT_PREFIXES: tuple[str, ...] = (
    "_app",
    "_auth",
    "_cookie_persistence",
    "_kernel",
    "_runtime.auth",
    "_runtime.lifecycle",
    "_web_cookie_provider",
    "auth",
    "cli",
    "paths",
)
KNOWN_DIRECT_LEG_CREDENTIAL_IMPORTS: dict[str, frozenset[str]] = {
    "_source/drive_import.py": frozenset({"_auth.account", "_web_cookie_provider"}),
    "_source/upload.py": frozenset({"_auth.account", "_auth.cookie_types", "_web_cookie_provider"}),
}
KNOWN_DIRECT_LEG_CREDENTIAL_IDENTIFIERS: dict[str, frozenset[str]] = {
    "_source/drive_import.py": frozenset({"account_email", "authuser", "cookies"}),
    "_source/upload.py": frozenset(
        {"account_email", "authuser", "cookies", "csrf_token", "session_id"}
    ),
}

#: Streamed Chat must materialize four already-acquired route/token values into
#: its request. The builder deliberately sits outside ``_web`` until P8 adds a
#: provider-owned private session, so this exact transitive boundary is audited
#: independently rather than escaping the package-only scan above.
KNOWN_CHAT_STREAM_REQUEST_IMPORTS: frozenset[str] = frozenset(
    {
        "_auth.account",
        "_env",
        "rpc.encoder",
        "rpc.types",
    }
)
KNOWN_CHAT_STREAM_CREDENTIAL_IDENTIFIERS: frozenset[str] = frozenset(
    {"account_email", "authuser", "csrf_token", "session_id"}
)

# --- Ownership inventories P8 must adapt rather than duplicate ---------------

#: Modules that reach the persisted profile document (``storage_state.json``)
#: through ``ProfileStore``, the sealed credential-commit capability, or the
#: stored-auth loaders. P8 adapts these behind the provider; the backend package
#: must never join this list.
KNOWN_PROFILE_DOCUMENT_OWNERS: frozenset[str] = frozenset(
    {
        "_auth/account_email.py",
        "_auth/account_repair.py",
        "_auth/browser_capture.py",
        "_auth/credential_io.py",
        "_auth/master_token.py",
        "_auth/master_token_bootstrap.py",
        "_auth/profile_migration.py",
        "_auth/profile_store.py",
        "_auth/psidts_recovery.py",
        "_auth/refresh.py",
        "_auth/storage.py",
        "_auth/tokens.py",
        "_cookie_persistence.py",
        "_runtime/init.py",
    }
)

#: Concrete profile-document operation calls. Unlike a source-substring scan,
#: this deliberately ignores annotations and frozen adapter values that merely
#: carry a ``ProfileStore`` capability to its existing owner.
PROFILE_DOCUMENT_CALLS: frozenset[str] = frozenset(
    {
        "ProfileStore",
        "_commit_json_unchecked",
        "_commit_profile_json",
        "_read_account_document",
        "_read_cookie_document",
        "_update_account_if_document_unchanged",
        "clear_account",
        "merge_cookie_observation",
        "merge_legacy_cookie_observation",
        "read_account",
        "read_document",
        "read_master_token",
        "read_session",
        "replace_from_login",
        "replace_from_remint",
        "replace_minted_session",
        "update_account",
        "write_master_token",
    }
)

#: Whole-transaction facade calls that share an operation name with a concrete
#: ``ProfileStore`` method. They do not receive a store/document capability and
#: therefore remain adapters rather than document owners.
PROFILE_DOCUMENT_ADAPTER_CALLS: dict[str, frozenset[str]] = {
    "_app/master_token.py": frozenset({"read_master_token", "write_master_token"}),
}

#: Modules that can drive a browser (interactive login, browser-cookie capture,
#: headless re-mint, doctor). P8 keeps every one of them OUTSIDE the backend.
KNOWN_INTERACTIVE_AUTH_OWNERS: frozenset[str] = frozenset(
    {
        "_app/profile.py",
        "_auth/_browser_cookie_filter.py",
        "_auth/account.py",
        "_auth/account_types.py",
        "_auth/browser_capture.py",
        "_auth/browser_launch_errors.py",
        "_auth/cookies.py",
        "_auth/headless_reauth.py",
        "_auth/session.py",
        "auth.py",
        "cli/_cookie_import.py",
        "cli/doctor_cmd.py",
        "cli/playwright_login_io.py",
        "cli/services/auth_refresh.py",
        "cli/services/login/io_seam.py",
        "cli/services/login/master_token.py",
        "cli/services/playwright_login.py",
        "cli/services/playwright_redaction.py",
        "cli/session_cmd.py",
    }
)

#: The four credential lock siblings, all derived from one helper
#: (``_auth.paths._lock_sibling``). They must stay four DISTINCT files: the
#: bootstrap lock is held across the storage lock's acquire, and ``flock``
#: conflicts between two open file descriptions inside one process. P8 keeps
#: this derivation unchanged.
EXPECTED_LOCK_SIBLING_KINDS: frozenset[str] = frozenset(
    {"lock", "rotate.lock", "refresh.lock", "lock.bootstrap"}
)

#: ``AuthTokens`` fields suppressed from the dataclass-generated ``repr``
#: because they are credential-equivalent. The immutable generation P8 returns
#: from the provider inherits this obligation.
EXPECTED_AUTH_TOKENS_REPR_SUPPRESSED: frozenset[str] = frozenset(
    {
        "cookies",
        "csrf_token",
        "session_id",
        "cookie_jar",
        "cookie_snapshot",
        "_profile_session_generation",
    }
)
EXPECTED_GENERATION_REPR_SUPPRESSED: frozenset[str] = frozenset(
    {"cookies", "csrf_token", "session_id"}
)
EXPECTED_SESSION_STATE_REPR_SUPPRESSED: frozenset[str] = frozenset({"cookies"})

#: Whole audited master-token TRANSACTIONS the CLI/app layer invokes through the
#: ``notebooklm.auth`` facade. P8 reuses these; it does not re-derive minting.
EXPECTED_MASTER_TOKEN_TRANSACTIONS: frozenset[str] = frozenset(
    {
        "assert_account_writable",
        "bootstrap_missing_storage_from_master_token",
        "master_token_bootstrap",
        "master_token_remint",
    }
)


@dataclass(frozen=True, slots=True)
class P8EntryReport:
    """Structured view of the P8 boundary as it stands today."""

    provider_defined: bool
    backend_first_party_imports: list[str]
    backend_credential_imports: list[str]
    backend_credential_identifiers: list[str]
    profile_document_owners: list[str]
    interactive_auth_owners: list[str]
    notes: list[str]


# --- Detectors ---------------------------------------------------------------


def _resolve_import(node: ast.ImportFrom | ast.Import, *, package_parts: list[str]) -> list[str]:
    """Return dotted module names relative to ``notebooklm`` for one import node."""
    if isinstance(node, ast.Import):
        return [
            alias.name.removeprefix("notebooklm.")
            for alias in node.names
            if alias.name == "notebooklm" or alias.name.startswith("notebooklm.")
        ]
    if node.level == 0:
        module = node.module or ""
        if module == "notebooklm" or module.startswith("notebooklm."):
            return [module.removeprefix("notebooklm.")]
        return []
    # ``level`` counts up from the importing module's own package.
    base = package_parts[: len(package_parts) - (node.level - 1)]
    tail = (node.module or "").split(".") if node.module else []
    return [".".join([*base, *tail])] if (base or tail) else []


def _python_files(path: Path) -> list[Path]:
    """Return one file or every Python file below a package root."""
    return [path] if path.is_file() else sorted(path.rglob("*.py"))


def collect_first_party_imports(package_root: Path, src_root: Path = SRC_ROOT) -> set[str]:
    """Collect first-party ``notebooklm.*`` imports made by one package."""
    imports: set[str] = set()
    for path in _python_files(package_root):
        package_parts = list(path.relative_to(src_root).parent.parts)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                imports.update(
                    name for name in _resolve_import(node, package_parts=package_parts) if name
                )
    return imports


def credential_imports(imports: set[str]) -> set[str]:
    """Select imports that would give a module credential acquisition powers."""
    return {
        name
        for name in imports
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in FORBIDDEN_WEB_IMPORT_PREFIXES
        )
    }


def direct_leg_credential_imports(imports: set[str]) -> set[str]:
    """Select imports that participate in direct-leg credential handling."""
    return {
        name
        for name in imports
        if any(
            name == prefix or name.startswith(f"{prefix}.")
            for prefix in DIRECT_LEG_CREDENTIAL_IMPORT_PREFIXES
        )
    }


def collect_named_identifiers(package_root: Path) -> set[str]:
    """Collect attribute and bare-name identifiers used anywhere in a package."""
    names: set[str] = set()
    for path in _python_files(package_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, (ast.arg, ast.keyword)) and node.arg is not None:
                names.add(node.arg)
    return names


def collect_credential_identifiers_by_module(package_root: Path) -> dict[str, frozenset[str]]:
    """Return credential-shaped identifiers grouped by package-relative module."""
    grouped: dict[str, frozenset[str]] = {}
    for path in _python_files(package_root):
        found = collect_named_identifiers(path) & CREDENTIAL_IDENTIFIERS
        if found:
            grouped[path.relative_to(package_root).as_posix()] = frozenset(found)
    return grouped


def collect_forbidden_owner_names_by_module(
    package_root: Path,
) -> dict[str, frozenset[str]]:
    """Return credential-owner attributes/types retained under a package."""
    grouped: dict[str, frozenset[str]] = {}
    for path in _python_files(package_root):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_WEB_OWNER_ATTRIBUTES:
                found.add(node.attr)
            elif isinstance(node, ast.Name) and node.id in FORBIDDEN_WEB_OWNER_TYPES:
                found.add(node.id)
        if found:
            grouped[path.relative_to(package_root).as_posix()] = frozenset(found)
    return grouped


def unexpected_credential_identifiers(package_root: Path) -> set[str]:
    """Return module-qualified identifiers outside the one materializer."""
    grouped = collect_credential_identifiers_by_module(package_root)
    unexpected: set[str] = set()
    for module, names in grouped.items():
        allowed = EXPECTED_WEB_CREDENTIAL_IDENTIFIERS.get(module, frozenset())
        unexpected.update(f"{module}::{name}" for name in names - allowed)
    return unexpected


def collect_modules_matching(
    needles: frozenset[str],
    src_root: Path = SRC_ROOT,
) -> set[str]:
    """Return repo-relative module paths whose source mentions any needle."""
    found: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        content = path.read_text(encoding="utf-8")
        if any(needle in content for needle in needles):
            found.add(path.relative_to(src_root).as_posix())
    return found


def collect_profile_document_owners(src_root: Path = SRC_ROOT) -> set[str]:
    """Return modules that call a concrete profile-document operation.

    Type annotations, dataclass fields, imports, and capability forwarding are
    intentionally not ownership. This keeps the provider adapters visible as
    adapters while the existing store/commit implementations remain the only
    document owners.
    """
    owners: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        module = path.relative_to(src_root).as_posix()
        calls = set(collect_call_names(path))
        calls -= PROFILE_DOCUMENT_ADAPTER_CALLS.get(module, frozenset())
        if PROFILE_DOCUMENT_CALLS.intersection(calls):
            owners.add(module)
    return owners


def collect_symbol_definitions(symbols: frozenset[str], src_root: Path = SRC_ROOT) -> set[str]:
    """Return ``module::symbol`` for every class/function definition named in ``symbols``."""
    defined: set[str] = set()
    for path in sorted(src_root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name in symbols
            ):
                defined.add(f"{path.relative_to(src_root).as_posix()}::{node.name}")
    return defined


def collect_call_names(path: Path) -> list[str]:
    """Return the final identifier of every call in one module."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if isinstance(node.func, ast.Name):
            names.append(node.func.id)
        elif isinstance(node.func, ast.Attribute):
            names.append(node.func.attr)
    return names


def evaluate_p8_boundary(
    src_root: Path = SRC_ROOT,
    web_root: Path = WEB_ROOT,
) -> P8EntryReport:
    """Inventory the P8 provider boundary as it stands today."""
    backend_imports = collect_first_party_imports(web_root, src_root=src_root)
    forbidden = credential_imports(backend_imports)
    identifiers = unexpected_credential_identifiers(web_root)
    provider = collect_symbol_definitions(P8_PROVIDER_SYMBOLS, src_root=src_root)

    notes: list[str] = []
    if not provider:
        notes.append(
            "WebCookieProvider is not defined yet: P8 has not started, and the "
            "backend still borrows the client-owned RpcExecutor/Kernel session"
        )
    if forbidden:
        notes.append(
            "web backend package imports credential-acquisition modules: "
            + ", ".join(sorted(forbidden))
        )
    if identifiers:
        notes.append(
            "web backend package names unexpected credential identifiers: "
            + ", ".join(sorted(identifiers))
        )

    return P8EntryReport(
        provider_defined=bool(provider),
        backend_first_party_imports=sorted(backend_imports),
        backend_credential_imports=sorted(forbidden),
        backend_credential_identifiers=sorted(identifiers),
        profile_document_owners=sorted(collect_profile_document_owners(src_root=src_root)),
        interactive_auth_owners=sorted(
            collect_modules_matching(frozenset({"playwright"}), src_root=src_root)
        ),
        notes=notes,
    )


# --- Test suite --------------------------------------------------------------


def test_p8_provider_definitions_are_exact_and_fail_closed() -> None:
    """P8 has exactly one provider/session port family and runtime adapters."""
    report = evaluate_p8_boundary()
    actual = collect_symbol_definitions(P8_PROVIDER_SYMBOLS)
    assert actual == EXPECTED_P8_PROVIDER_DEFINITIONS
    assert report.provider_defined
    assert not any("not defined yet" in note for note in report.notes)


def test_web_backend_first_party_imports_are_exact_and_fail_closed() -> None:
    """The backend package's first-party dependency set is baselined."""
    actual = collect_first_party_imports(WEB_ROOT)
    added = actual - KNOWN_WEB_PACKAGE_FIRST_PARTY_IMPORTS
    removed = KNOWN_WEB_PACKAGE_FIRST_PARTY_IMPORTS - actual

    assert not added, (
        "New first-party imports in src/notebooklm/_web/ — classify them before P8:\n  "
        + "\n  ".join(sorted(added))
    )
    assert not removed, (
        "Imports disappeared from src/notebooklm/_web/; update the baseline:\n  "
        + "\n  ".join(sorted(removed))
    )


def test_web_backend_does_not_reach_credential_acquisition() -> None:
    """P8 criterion: the backend reads no profile file and starts no login."""
    report = evaluate_p8_boundary()
    assert not report.backend_credential_imports, (
        "src/notebooklm/_web/ must receive an injected provider, never import "
        "credential acquisition/persistence itself:\n  "
        + "\n  ".join(report.backend_credential_imports)
    )


def test_web_backend_credential_materializer_is_exact() -> None:
    """Only the RPC runtime names already-acquired token/route values."""
    report = evaluate_p8_boundary()
    assert not report.backend_credential_identifiers, (
        "src/notebooklm/_web/ names credential identifiers outside the exact "
        "generation materializer:\n  " + "\n  ".join(report.backend_credential_identifiers)
    )
    assert collect_credential_identifiers_by_module(WEB_ROOT) == (
        EXPECTED_WEB_CREDENTIAL_IDENTIFIERS
    )


def test_web_backend_retains_no_auth_persistence_owner_attributes_or_types() -> None:
    """Credential owners stay composed behind the provider port."""
    retained = collect_forbidden_owner_names_by_module(WEB_ROOT)
    assert retained == {}, (
        "src/notebooklm/_web/ retained credential-owner attributes/types instead of "
        f"the provider port: {retained}"
    )


def test_web_rpc_request_materializer_is_exact_and_cannot_acquire_credentials() -> None:
    """The opaque-generation RPC encoder is the one reviewed transitive seam."""
    imports = collect_first_party_imports(WEB_REQUEST_AUTH_PATH)
    identifiers = collect_named_identifiers(WEB_REQUEST_AUTH_PATH) & CREDENTIAL_IDENTIFIERS

    assert imports == KNOWN_WEB_REQUEST_AUTH_IMPORTS
    assert identifiers == KNOWN_WEB_REQUEST_AUTH_CREDENTIAL_IDENTIFIERS
    assert credential_imports(imports) == {"_auth.account"}


def test_streamed_chat_credential_materializer_is_exact_and_cannot_acquire_credentials() -> None:
    """The backend's one credential-aware transitive encoder is explicit and closed."""
    imports = collect_first_party_imports(CHAT_STREAM_REQUEST_PATH)
    identifiers = collect_named_identifiers(CHAT_STREAM_REQUEST_PATH) & CREDENTIAL_IDENTIFIERS

    assert imports == KNOWN_CHAT_STREAM_REQUEST_IMPORTS
    assert identifiers == KNOWN_CHAT_STREAM_CREDENTIAL_IDENTIFIERS
    assert credential_imports(imports) == {"_auth.account"}


@pytest.mark.parametrize(
    "path",
    [SOURCE_UPLOAD_PATH, DRIVE_IMPORT_PATH],
    ids=["source-upload", "drive-import"],
)
def test_direct_http_leg_credential_materializers_are_exact(path: Path) -> None:
    """Upload/Drive receive immutable generations and no acquisition owner."""
    module = path.relative_to(SRC_ROOT).as_posix()
    imports = collect_first_party_imports(path)
    identifiers = collect_named_identifiers(path) & CREDENTIAL_IDENTIFIERS

    assert direct_leg_credential_imports(imports) == KNOWN_DIRECT_LEG_CREDENTIAL_IMPORTS[module]
    assert identifiers == KNOWN_DIRECT_LEG_CREDENTIAL_IDENTIFIERS[module]

    duplicated_owner_calls = {
        "ProfileStore",
        "StorageLockManager",
        "_commit_profile_json",
        "_load_stored_auth",
        "attempt_headless_reauth",
        "mint_cookies",
        "refresh_auth_session",
        "remint_from_stored_token",
        "try_headless_reauth",
        "try_master_token_reauth",
        "try_refresh_cmd_reauth",
        "try_storage_cookie_reload",
    }
    assert not duplicated_owner_calls.intersection(collect_call_names(path))


def test_profile_document_owner_inventory_is_exact_and_excludes_the_backend() -> None:
    """Profile-document owners are baselined; P8 adapts them, never duplicates."""
    actual = collect_profile_document_owners()
    added = actual - KNOWN_PROFILE_DOCUMENT_OWNERS
    removed = KNOWN_PROFILE_DOCUMENT_OWNERS - actual

    assert not added, (
        "New profile-document readers/writers — P8 must adapt the existing owners, "
        "not add new ones:\n  " + "\n  ".join(sorted(added))
    )
    assert not removed, (
        "Profile-document owners disappeared; update the baseline:\n  "
        + "\n  ".join(sorted(removed))
    )
    assert not {name for name in actual if name.startswith("_web/")}, (
        "The web backend package must not reach the profile document"
    )


def test_interactive_auth_stays_outside_the_backend() -> None:
    """Interactive login, browser capture, headless re-mint, and doctor stay out."""
    actual = collect_modules_matching(frozenset({"playwright"}))
    added = actual - KNOWN_INTERACTIVE_AUTH_OWNERS
    removed = KNOWN_INTERACTIVE_AUTH_OWNERS - actual

    assert not added, (
        "New browser-driving modules — P8 keeps interactive auth outside the "
        "backend:\n  " + "\n  ".join(sorted(added))
    )
    assert not removed, (
        "Browser-driving modules disappeared; update the baseline:\n  "
        + "\n  ".join(sorted(removed))
    )
    assert not {name for name in actual if name.startswith("_web/")}, (
        "The web backend package must not launch interactive authentication"
    )


def test_credential_lock_siblings_share_one_derivation_and_stay_distinct() -> None:
    """The four credential locks derive from one helper and remain four files."""
    from notebooklm._auth import paths as auth_paths

    base = Path("/tmp/profile/storage_state.json")
    derived = {
        auth_paths._storage_state_lock_path(base),
        auth_paths._rotation_lock_path(base),
        auth_paths._refresh_lock_path(base),
        auth_paths._bootstrap_lock_path(base),
    }
    assert len(derived) == 4, f"credential locks collapsed onto the same file: {derived}"

    kinds = {
        path.name.removeprefix(f".{base.name}.")
        for path in derived
        # the bootstrap lock canonicalizes its base, so match on suffix only
    }
    assert kinds == EXPECTED_LOCK_SIBLING_KINDS, f"lock kinds drifted: {sorted(kinds)}"

    # One derivation, so a new lock kind cannot invent its own spelling.
    source = (SRC_ROOT / "_auth" / "paths.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    sibling_callers = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "_lock_sibling" in sibling_callers


def test_bare_atomic_write_still_refuses_the_profile_document() -> None:
    """CAS/lock discipline: the unlocked atomic write refuses storage_state.json."""
    from notebooklm._atomic_io import atomic_write_json

    with pytest.raises(ValueError, match="storage_state.json"):
        atomic_write_json(Path("/tmp/does-not-matter/Storage_State.JSON"), {})


def test_credential_commit_capability_stays_sealed_behind_credential_io() -> None:
    """The unchecked atomic primitive keeps exactly one importer."""
    importers = {
        path.relative_to(SRC_ROOT).as_posix()
        for path in SRC_ROOT.rglob("*.py")
        if path.name != "_atomic_io.py"
        and "_atomic_write_json_unchecked" in path.read_text(encoding="utf-8")
    }
    assert importers == {"_auth/credential_io.py"}, (
        f"credential commit capability leaked to: {sorted(importers)}"
    )


def test_auth_tokens_credential_fields_are_repr_suppressed() -> None:
    """Secret redaction: credential fields never reach the generated repr.

    The immutable cookie/account-route generation P8 returns from the provider
    inherits this obligation; pinning the field set here makes a new credential
    field that forgets ``repr=False`` fail closed.
    """
    from notebooklm.auth import AuthTokens

    suppressed = {field.name for field in fields(AuthTokens) if not field.repr}
    assert suppressed == EXPECTED_AUTH_TOKENS_REPR_SUPPRESSED, (
        f"AuthTokens repr-suppressed field set drifted: {sorted(suppressed)}"
    )
    assert "__repr__" in vars(AuthTokens), "AuthTokens must keep its redacting __repr__"


def test_provider_generation_and_detached_session_values_redact_credentials() -> None:
    """Every provider/session value suppresses credential-equivalent fields."""
    from notebooklm._web_cookie_provider import WebCookieGeneration, WebCookieSessionState

    generation_suppressed = {field.name for field in fields(WebCookieGeneration) if not field.repr}
    session_suppressed = {field.name for field in fields(WebCookieSessionState) if not field.repr}
    assert generation_suppressed == EXPECTED_GENERATION_REPR_SUPPRESSED
    assert session_suppressed == EXPECTED_SESSION_STATE_REPR_SUPPRESSED


def test_provider_storage_adapter_delegates_one_whole_load_transaction() -> None:
    """The provider names the stored-auth owner without copying its mechanics."""
    calls = collect_call_names(STORAGE_ADAPTER_PATH)
    assert calls.count("_load_stored_auth") == 1
    duplicated_storage_mechanics = {
        "ProfileStore",
        "StorageLockManager",
        "_atomic_write_json_unchecked",
        "_commit_json_unchecked",
        "_commit_profile_json",
        "_lock_sibling",
        "_load_storage_state",
        "atomic_write_json",
        "merge_cookie_observation",
        "save_cookies_to_storage",
    }
    assert not duplicated_storage_mechanics.intersection(calls)


def test_provider_refresh_adapter_delegates_one_whole_refresh_transaction() -> None:
    """The provider keeps policy joining but never copies a recovery rung."""
    calls = collect_call_names(REFRESH_ADAPTER_PATH)
    assert calls.count("refresh_auth_session") == 1
    assert calls.count("await_refresh") == 1
    duplicated_refresh_mechanics = {
        "attempt_headless_reauth",
        "coalesced_cold_recovery",
        "mint_cookies",
        "remint_from_stored_token",
        "try_headless_reauth",
        "try_master_token_reauth",
        "try_refresh_cmd_reauth",
        "try_storage_cookie_reload",
    }
    assert not duplicated_refresh_mechanics.intersection(calls)


def test_runtime_provider_composes_transactions_without_reimplementing_them() -> None:
    """The concrete provider orchestrates owners but implements no auth rung/store."""
    calls = collect_call_names(RUNTIME_PROVIDER_PATH)
    duplicated_owner_calls = {
        "ProfileStore",
        "StorageLockManager",
        "_atomic_write_json_unchecked",
        "_commit_profile_json",
        "_load_stored_auth",
        "_load_storage_state",
        "attempt_headless_reauth",
        "atomic_write_json",
        "mint_cookies",
        "refresh_auth_session",
        "remint_from_stored_token",
        "try_headless_reauth",
        "try_master_token_reauth",
        "try_refresh_cmd_reauth",
        "try_storage_cookie_reload",
    }
    assert not duplicated_owner_calls.intersection(calls)


def test_backend_private_session_has_no_acquisition_or_persistence_capability() -> None:
    """The mutable backend session only clones, detaches, opens, and closes."""
    imports = collect_first_party_imports(BACKEND_SESSION_PATH)
    assert not {
        name
        for name in imports
        if name
        in {
            "_auth.profile_store",
            "_auth.session",
            "_auth.tokens",
            "_cookie_persistence",
            "auth",
            "paths",
        }
    }


def test_master_token_transactions_are_reused_not_reimplemented() -> None:
    """P8 adapts the audited master-token transactions behind the provider."""
    from notebooklm import auth as auth_facade

    missing = {
        name for name in EXPECTED_MASTER_TOKEN_TRANSACTIONS if not hasattr(auth_facade, name)
    }
    assert not missing, f"master-token transactions disappeared from the facade: {sorted(missing)}"

    # ``MintService`` is the one wire implementation; ``mint_cookies`` is its
    # v0.x composition adapter. Two definitions, one implementation — a second
    # of either would mean P8 re-derived minting instead of adapting it.
    minting_owners = collect_symbol_definitions(frozenset({"MintService", "mint_cookies"}))
    assert minting_owners == {
        "_auth/master_token.py::mint_cookies",
        "_auth/mint_service.py::MintService",
    }, f"cookie minting gained a second implementation: {sorted(minting_owners)}"


# --- Detector self-tests (fail-closed mutation tests) ------------------------


def test_detector_flags_a_backend_that_imports_credentials(tmp_path: Path) -> None:
    """A backend package importing ``.._auth.storage`` is reported as a blocker."""
    src = tmp_path / "notebooklm"
    web = src / "_web"
    web.mkdir(parents=True)
    (web / "backend.py").write_text(
        "from .._auth.storage import read_account_metadata\n", encoding="utf-8"
    )
    report = evaluate_p8_boundary(src_root=src, web_root=web)
    assert report.backend_credential_imports == ["_auth.storage"]
    assert any("credential-acquisition modules" in note for note in report.notes)


def test_detector_flags_a_backend_that_names_credentials(tmp_path: Path) -> None:
    """A backend naming ``csrf_token``/``cookie_jar`` is reported as a blocker."""
    src = tmp_path / "notebooklm"
    web = src / "_web"
    web.mkdir(parents=True)
    (web / "backend.py").write_text(
        "def build(session):\n    return session.cookie_jar, session.csrf_token\n",
        encoding="utf-8",
    )
    report = evaluate_p8_boundary(src_root=src, web_root=web)
    assert report.backend_credential_identifiers == [
        "backend.py::cookie_jar",
        "backend.py::csrf_token",
    ]
    assert any("unexpected credential identifiers" in note for note in report.notes)


def test_detector_distinguishes_store_adapter_types_from_document_owners(
    tmp_path: Path,
) -> None:
    """Carrying a store type is not ownership; invoking it is."""
    src = tmp_path / "notebooklm"
    auth_root = src / "_auth"
    auth_root.mkdir(parents=True)
    (auth_root / "adapter.py").write_text(
        "from .profile_store import ProfileStore\n"
        "def carry(store: ProfileStore) -> ProfileStore:\n"
        "    return store\n",
        encoding="utf-8",
    )
    (auth_root / "owner.py").write_text(
        "from .profile_store import ProfileStore\n"
        "def read(path):\n"
        "    return ProfileStore(path).read_document()\n",
        encoding="utf-8",
    )

    assert collect_profile_document_owners(src_root=src) == {"_auth/owner.py"}


def test_detector_flags_backend_credential_owner_attributes_and_types(tmp_path: Path) -> None:
    """A backend cannot hide an auth owner behind a private attribute."""
    web = tmp_path / "_web"
    web.mkdir()
    (web / "backend.py").write_text(
        "class Backend:\n"
        "    def __init__(self, lifecycle: ClientLifecycle):\n"
        "        self._lifecycle = lifecycle\n"
        "        self._auth_coord = None\n",
        encoding="utf-8",
    )

    assert collect_forbidden_owner_names_by_module(web) == {
        "backend.py": frozenset({"ClientLifecycle", "_auth_coord", "_lifecycle"})
    }


def test_detector_flags_acquisition_moved_into_a_direct_http_leg(tmp_path: Path) -> None:
    """An upload adapter cannot hide profile/refresh work outside ``_web``."""
    src = tmp_path / "notebooklm"
    upload = src / "_source" / "upload.py"
    upload.parent.mkdir(parents=True)
    upload.write_text(
        "from .._auth.account import format_authuser_value\n"
        "from .._auth.storage import load_auth_from_storage\n"
        "from .._web_cookie_provider import WebCookieGeneration\n"
        "def build(generation):\n"
        "    return generation.cookies, generation.authuser, load_auth_from_storage()\n",
        encoding="utf-8",
    )

    imports = collect_first_party_imports(upload, src_root=src)
    credential_surface = direct_leg_credential_imports(imports)
    assert credential_surface == {
        "_auth.account",
        "_auth.storage",
        "_web_cookie_provider",
    }
    assert "_auth.storage" not in KNOWN_DIRECT_LEG_CREDENTIAL_IMPORTS["_source/upload.py"]


def test_detector_flags_credential_acquisition_moved_behind_chat_stream_import(
    tmp_path: Path,
) -> None:
    """Moving an auth reader one module outside ``_web`` cannot evade P8."""
    src = tmp_path / "notebooklm"
    stream_request = src / "_chat" / "stream_request.py"
    stream_request.parent.mkdir(parents=True)
    stream_request.write_text(
        "from .._auth.account import format_authuser_value\n"
        "from .._auth.storage import load_auth_from_storage\n"
        "def build(snapshot):\n"
        "    return snapshot.csrf_token, snapshot.session_id, "
        "snapshot.authuser, snapshot.account_email\n",
        encoding="utf-8",
    )

    imports = collect_first_party_imports(stream_request, src_root=src)
    identifiers = collect_named_identifiers(stream_request) & CREDENTIAL_IDENTIFIERS

    assert credential_imports(imports) == {"_auth.account", "_auth.storage"}
    assert "_auth.storage" not in KNOWN_CHAT_STREAM_REQUEST_IMPORTS
    assert identifiers == KNOWN_CHAT_STREAM_CREDENTIAL_IDENTIFIERS


def test_detector_reports_provider_once_defined(tmp_path: Path) -> None:
    """Defining ``WebCookieProvider`` flips the report's readiness note."""
    src = tmp_path / "notebooklm"
    web = src / "_web"
    web.mkdir(parents=True)
    (web / "provider.py").write_text("class WebCookieProvider:\n    pass\n", encoding="utf-8")
    report = evaluate_p8_boundary(src_root=src, web_root=web)
    assert report.provider_defined is True
    assert not any("not defined yet" in note for note in report.notes)
