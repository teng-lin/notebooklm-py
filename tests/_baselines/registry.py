"""The regenerable-baseline registry (ADR-0022).

One place that knows, per *regenerable baseline*, how to:

* **derive** it from live code (``Baseline.derive``);
* where its committed JSON file lives (``Baseline.path``);
* how to **serialize** it deterministically (``Baseline.dump`` /
  ``Baseline.sort_keys``).

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

# Audit source-of-truth for the allowlist ``extra_public_names`` (mirrors
# ``scripts/audit_public_api_compat.py``). The collected public surface for a
# module is ``__all__`` plus any *resolvable* allowlist extras not already in it.
_ALLOWLIST_PATH = _PROJECT_ROOT / "scripts" / "api-compat-allowlist.json"


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


# ---------------------------------------------------------------------------
# Baseline registry
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Baseline:
    """One regenerable baseline: derive it, locate its committed JSON, compare.

    ``derive`` returns the live value; ``path`` is the committed JSON file;
    ``sort_keys`` controls JSON key ordering on dump (lists always preserve
    order — only dict *keys* are affected). ``load()`` reads the committed value;
    ``write()`` rewrites it from ``derive()`` (dev-only, behind
    ``--update-baselines``).
    """

    name: str
    path: Path
    derive: Callable[[], object]
    sort_keys: bool = False
    # Extra metadata kept out of equality/hash; documents intent.
    description: str = field(default="", compare=False)

    def dump(self, value: object) -> str:
        """Serialize ``value`` to the committed-on-disk JSON string (trailing newline)."""
        return json.dumps(value, indent=2, sort_keys=self.sort_keys) + "\n"

    def load(self) -> object:
        """The committed baseline value (parsed JSON)."""
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self) -> None:
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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(self.dump(self.derive()), encoding="utf-8")


BASELINES: list[Baseline] = [
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
    "UNGATED_PUBLIC_MODULES",
    "allowlist_extra_public_names",
    "baseline_by_name",
    "collect_public_surface",
]
