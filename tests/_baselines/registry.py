"""The regenerable-baseline registry (ADR-0022).

One place that knows, per *regenerable baseline*, how to:

* **derive** it from live code (``Baseline.derive``);
* where its committed JSON file lives (``Baseline.path``);
* how to **serialize** it deterministically (``Baseline.dump`` /
  ``Baseline.sort_keys``).
* for shrink-only ratchets, what counts as reviewed **growth**
  (``Baseline.growth_check``).

A baseline is a value the code already derives — e.g. ``notebooklm.types.__all__``
or the collected public surface of the ungated public modules. The freeze tests
in ``tests/_guardrails/test_public_surface_manifest.py`` load the committed file
and assert it equals ``derive()``; ``scripts/regen_baselines.py`` (via the
``--update-baselines`` pytest flag) rewrites the file from ``derive()``.

**Dev-only-regen invariant.** Regeneration only ever happens when a developer
passes ``--update-baselines`` to pytest. CI never passes the flag, so CI only
ever *diffs* derive() against the committed file. See ADR-0022.

The derive callables reuse the production-facing surface (``notebooklm`` imports)
and the audit's own ``load_policy`` — they never copy values. Adding one public
symbol then becomes a one-command regen instead of hand-editing snapshot literals.
"""

from __future__ import annotations

import importlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

# ``tests/`` directory (this file is tests/_baselines/registry.py).
_TESTS_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_ROOT = _TESTS_ROOT.parent
_FIXTURES_DIR = _TESTS_ROOT / "fixtures"
_BASELINES_DIR = _FIXTURES_DIR / "baselines"
_BROWSER_ROOT = _PROJECT_ROOT / "src" / "notebooklm" / "_browser"
_AUTH_ROOT = _PROJECT_ROOT / "src" / "notebooklm" / "_auth"
_AUTH_FACADE = _PROJECT_ROOT / "src" / "notebooklm" / "auth.py"

# Audit source-of-truth for the allowlist ``extra_public_names`` (mirrors
# ``scripts/audit_public_api_compat.py``). The collected public surface for a
# module is ``__all__`` plus any *resolvable* allowlist extras not already in it.
_ALLOWLIST_PATH = _PROJECT_ROOT / "scripts" / "api-compat-allowlist.json"

GrowthCheck = Callable[[object, object], list[str]]


# ---------------------------------------------------------------------------
# Shared derivation primitives (also imported by the freeze tests so the gate
# and the regen path derive identically — no copy).
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def allowlist_extra_public_names() -> dict[str, list[str]]:
    """Allowlist ``extra_public_names`` via the audit's OWN ``load_policy`` — the
    same schema validation + case-insensitive sort/dedupe the audit applies, so
    this can't drift from the audit's contract, parsed once. Lazy import to keep
    the audit module (and its collector machinery) off the registry-import path.
    """
    import scripts.audit_public_api_compat as audit

    _allowances, extras = audit.load_policy(_ALLOWLIST_PATH)
    return extras


def collect_public_surface(module_name: str) -> list[str]:
    """The audit-collected export surface for ``module_name``: ``__all__`` plus
    any *resolvable* ``extra_public_names`` not already in ``__all__`` — mirroring
    ``scripts/audit_public_api_compat.py::collect_module``. Order is ``__all__``
    (its own order) first, then the normalized extras. A non-resolving name *in*
    ``__all__`` is kept (unlike the audit, which re-raises) — that bad state is
    caught independently by ``test_public_top_level_module_declares_all``.
    """
    module = importlib.import_module(module_name)
    names = list(getattr(module, "__all__", []))
    for name in allowlist_extra_public_names().get(module_name, []):
        if name not in names and hasattr(module, name):
            names.append(name)
    return names


# Ungated public modules whose collected surface is frozen by the
# ``ungated_surface`` baseline. These are every audit-discovered public module
# EXCEPT the four exact-``__all__``-pinned-elsewhere modules
# (``notebooklm.auth`` / ``client`` / ``rpc`` / ``types``). The exact set is a
# property pinned by ``test_ungated_public_surface_covers_exactly_the_unpinned_modules``;
# this list is the regen seed and is asserted complete against discovery there.
UNGATED_PUBLIC_MODULES: tuple[str, ...] = (
    "notebooklm",
    "notebooklm.artifacts",
    "notebooklm.config",
    "notebooklm.exceptions",
    "notebooklm.io",
    "notebooklm.log",
    "notebooklm.migration",
    "notebooklm.paths",
    "notebooklm.raw",
    "notebooklm.research",
    "notebooklm.urls",
    "notebooklm.utils",
)


# ---------------------------------------------------------------------------
# Derive callables (one per baseline). Each REUSES existing production surface;
# none copies a literal.
# ---------------------------------------------------------------------------


def _derive_types_all() -> list[str]:
    """``notebooklm.types.__all__`` as an ordered list (export order is meaningful)."""
    import notebooklm.types as public_types

    return list(public_types.__all__)


def _derive_ungated_surface() -> dict[str, list[str]]:
    """The collected public surface of each ungated public module (ordered lists)."""
    return {module: collect_public_surface(module) for module in UNGATED_PUBLIC_MODULES}


def _derive_cli_contract() -> dict[str, object]:
    """The deterministic public CLI inventory (``build_cli_contract``)."""
    from tests._baselines.cli_contract import build_cli_contract

    return build_cli_contract()


@lru_cache(maxsize=1)
def _derive_auth_patch_sites() -> dict[str, object]:
    """Stable full-joint projection from the auth patch-site audit."""
    from scripts.audit_auth_patch_sites import build_projection, collect_sites

    return build_projection(collect_sites(_TESTS_ROOT))


@lru_cache(maxsize=1)
def _derive_browser_patch_sites() -> dict[str, object]:
    """Stable projection from patch sites into the browser package."""
    from scripts.audit_auth_patch_sites import build_projection, collect_sites

    return build_projection(
        collect_sites(
            _TESTS_ROOT,
            _BROWSER_ROOT,
            package_dotted="notebooklm._browser",
        )
    )


@lru_cache(maxsize=1)
def _derive_auth_facade_patch_sites() -> dict[str, object]:
    """Public-auth facade substitution projection (relocation sentinel)."""
    from scripts.audit_auth_patch_sites import build_projection, collect_sites

    return build_projection(
        collect_sites(
            _TESTS_ROOT,
            _AUTH_FACADE,
            package_dotted="notebooklm.auth",
        )
    )


def _derive_auth_family_patch_scorecard() -> dict[str, object]:
    from scripts.audit_auth_patch_sites import build_family_scorecard

    return build_family_scorecard(
        [
            _derive_auth_patch_sites(),
            _derive_browser_patch_sites(),
            _derive_auth_facade_patch_sites(),
        ]
    )


def _patch_projection_growth(previous: object, current: object) -> list[str]:
    from scripts.audit_auth_patch_sites import projection_growth

    return projection_growth(previous, current)


def _auth_family_growth(previous: object, current: object) -> list[str]:
    from scripts.audit_auth_patch_sites import family_scorecard_growth

    return family_scorecard_growth(previous, current)


@lru_cache(maxsize=1)
def _derive_auth_shared_mutations() -> dict[str, object]:
    from scripts.audit_auth_shared_mutations import build_projection, collect_mutations

    return build_projection(
        collect_mutations(
            _TESTS_ROOT,
            {
                "notebooklm._auth": _AUTH_ROOT,
                "notebooklm._browser": _BROWSER_ROOT,
            },
        )
    )


def _auth_shared_mutation_growth(previous: object, current: object) -> list[str]:
    from scripts.audit_auth_shared_mutations import projection_growth

    return projection_growth(previous, current)


def _derive_auth_import_graph() -> dict[str, object]:
    """Static direct-module import graph for ``notebooklm._auth``."""
    from scripts.audit_auth_import_graph import build_projection

    return build_projection()


def _derive_browser_import_graph() -> dict[str, object]:
    """Package-aware import projection for ``notebooklm._browser``."""
    from scripts.audit_auth_import_graph import build_projection

    return build_projection(
        _BROWSER_ROOT,
        package_prefix="notebooklm._browser",
        include_external=True,
    )


def _derive_module_size() -> dict[str, object]:
    """Current module-size budget, allowlist ceilings, and shrink locks."""
    from tests._baselines.module_size import derive_module_size

    return derive_module_size()


def _module_size_growth(previous: object, current: object) -> list[str]:
    from tests._baselines.module_size import module_size_growth

    return module_size_growth(previous, current)


def _derive_storage_transaction_policy() -> dict[str, list[str]]:
    """Direct callers of each profile-transaction lock-failure policy."""
    from tests._baselines.storage_transaction_policy import derive_storage_transaction_policy

    return derive_storage_transaction_policy()


def _storage_transaction_policy_growth(previous: object, current: object) -> list[str]:
    from tests._baselines.storage_transaction_policy import storage_transaction_policy_growth

    return storage_transaction_policy_growth(previous, current)


def _derive_guardrail_inline_literals() -> dict[str, dict[str, int]]:
    """Large inline container literals still grandfathered in guardrail tests."""
    from tests._baselines.guardrail_literals import inventory_large_inline_literals

    return inventory_large_inline_literals()


def _guardrail_inline_literal_growth(previous: object, current: object) -> list[str]:
    from tests._baselines.guardrail_literals import guardrail_literal_growth

    return guardrail_literal_growth(previous, current)


# ---------------------------------------------------------------------------
# Baseline registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    """One regenerable baseline: derive it, locate its committed JSON, compare.

    ``derive`` returns the live value; ``path`` is the committed JSON file;
    ``sort_keys`` controls JSON key ordering on dump (lists always preserve
    order — only dict *keys* are affected). ``growth_check`` optionally protects
    shrink-only state from accidental expansion. ``load()`` reads the committed
    value; ``write()`` rewrites it from ``derive()`` (dev-only, behind
    ``--update-baselines``).
    """

    name: str
    path: Path
    derive: Callable[[], object]
    sort_keys: bool = False
    growth_check: GrowthCheck | None = field(default=None, compare=False)
    # Extra metadata kept out of equality/hash; documents intent.
    description: str = field(default="", compare=False)

    def dump(self, value: object) -> str:
        """Serialize ``value`` to the committed-on-disk JSON string (trailing newline)."""
        return json.dumps(value, indent=2, sort_keys=self.sort_keys) + "\n"

    def load(self) -> object:
        """The committed baseline value (parsed JSON)."""
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, *, allow_growth: bool = False) -> None:
        """Rewrite the committed file from ``derive()``. Dev-only (regen seam).

        Enforces the dev-only-regen invariant at the seam itself (not only at the
        ``--update-baselines`` call site): a CI environment must never rewrite a
        baseline. CI only ever diffs (ADR-0022).
        """
        if os.environ.get("CI", "").strip():
            raise RuntimeError(
                "refusing to regenerate baselines in CI: baselines are dev-only "
                "regenerated and CI only diffs (ADR-0022)."
            )
        derived = self.derive()
        if self.growth_check is not None and self.path.is_file() and not allow_growth:
            growth = self.growth_check(self.load(), derived)
            if growth:
                details = "\n  ".join(growth)
                raise RuntimeError(
                    f"refusing to grow the shrink-only {self.name} baseline:\n  {details}\n"
                    "Review the growth, then rerun `python scripts/regen_baselines.py "
                    "--allow-growth` to acknowledge it explicitly."
                )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.dump(derived), encoding="utf-8")


BASELINES: list[Baseline] = [
    Baseline(
        name="module_size",
        path=_BASELINES_DIR / "module_size.json",
        derive=_derive_module_size,
        sort_keys=True,
        growth_check=_module_size_growth,
        description="Module-size budget plus live over-budget and shrink-locked ceilings.",
    ),
    Baseline(
        name="storage_transaction_policy",
        path=_BASELINES_DIR / "storage_transaction_policy.json",
        derive=_derive_storage_transaction_policy,
        sort_keys=True,
        growth_check=_storage_transaction_policy_growth,
        description="Direct owners of the profile transaction's lock-failure policies.",
    ),
    Baseline(
        name="guardrail_inline_literals",
        path=_BASELINES_DIR / "guardrail_inline_literals.json",
        derive=_derive_guardrail_inline_literals,
        sort_keys=True,
        growth_check=_guardrail_inline_literal_growth,
        description="Grandfathered large module-level literals in guardrail tests.",
    ),
    Baseline(
        name="auth_patch_sites",
        path=_BASELINES_DIR / "auth_patch_sites.json",
        derive=_derive_auth_patch_sites,
        sort_keys=True,
        growth_check=_patch_projection_growth,
        description="Auth test patch sites with package/path/lexical-owner identity.",
    ),
    Baseline(
        name="browser_patch_sites",
        path=_BASELINES_DIR / "browser_patch_sites.json",
        derive=_derive_browser_patch_sites,
        sort_keys=True,
        growth_check=_patch_projection_growth,
        description="Browser test patch sites with package/path/lexical-owner identity.",
    ),
    Baseline(
        name="auth_facade_patch_sites",
        path=_BASELINES_DIR / "auth_facade_patch_sites.json",
        derive=_derive_auth_facade_patch_sites,
        sort_keys=True,
        growth_check=_patch_projection_growth,
        description="Public auth facade substitutions; a no-growth relocation sentinel.",
    ),
    Baseline(
        name="auth_family_patch_scorecard",
        path=_BASELINES_DIR / "auth_family_patch_scorecard.json",
        derive=_derive_auth_family_patch_scorecard,
        sort_keys=True,
        growth_check=_auth_family_growth,
        description="Combined auth/browser/facade scorecard retaining package identity.",
    ),
    Baseline(
        name="auth_shared_mutations",
        path=_BASELINES_DIR / "auth_shared_mutations.json",
        derive=_derive_auth_shared_mutations,
        sort_keys=True,
        growth_check=_auth_shared_mutation_growth,
        description="Auth/browser shared-owner mutations with lexical ownership.",
    ),
    Baseline(
        name="auth_import_graph",
        path=_BASELINES_DIR / "auth_import_graph.json",
        derive=_derive_auth_import_graph,
        sort_keys=True,
        description="Static direct-module import graph for notebooklm._auth.",
    ),
    Baseline(
        name="browser_import_graph",
        path=_BASELINES_DIR / "browser_import_graph.json",
        derive=_derive_browser_import_graph,
        sort_keys=True,
        description="Static package-aware import graph for notebooklm._browser.",
    ),
    Baseline(
        name="types_all",
        path=_BASELINES_DIR / "types_all.json",
        derive=_derive_types_all,
        sort_keys=False,
        description="notebooklm.types.__all__ (ordered export surface).",
    ),
    Baseline(
        name="ungated_surface",
        path=_BASELINES_DIR / "ungated_surface.json",
        derive=_derive_ungated_surface,
        sort_keys=False,
        description="Collected public surface of every ungated public module.",
    ),
    Baseline(
        name="cli_contract",
        # Pre-existing path kept in place (the CLI contract test already uses it).
        path=_FIXTURES_DIR / "cli_contract_baseline.json",
        derive=_derive_cli_contract,
        sort_keys=True,
        description="Public CLI command tree, options, help, and aliases.",
    ),
]


def baseline_by_name(name: str) -> Baseline:
    """Look up a registered baseline by ``name`` (raises ``KeyError`` if absent)."""
    for baseline in BASELINES:
        if baseline.name == name:
            return baseline
    raise KeyError(f"no registered baseline named {name!r}")


__all__ = [
    "BASELINES",
    "Baseline",
    "GrowthCheck",
    "UNGATED_PUBLIC_MODULES",
    "allowlist_extra_public_names",
    "baseline_by_name",
    "collect_public_surface",
]
