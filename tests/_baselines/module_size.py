"""Derivation and growth policy for the module-size baseline."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOT = PROJECT_ROOT / "src" / "notebooklm"

# This is authored policy, not frozen state. The measured ceilings live in the
# committed baseline and are derived from the source tree.
MODULE_SIZE_BUDGET = 1500
# Authored exemptions preserve the review intent that JSON cannot carry. Values
# explain why the path may remain over budget; measured ceilings stay derived.
OVER_BUDGET_EXEMPTIONS: dict[str, str] = {
    "_android/proto/google/internal/labs/tailwind/orchestration/v1/"
    "orchestration_service_pb2_grpc.py": (
        "deterministic protoc output for the complete generated Android service; splitting it "
        "would require hand-editing generated code"
    ),
    "exceptions.py": (
        "canonical public exception home; moving classes would fork their documented provenance"
    ),
    "_android/sources.py": (
        "the complete native source surface for one backend; the deadline-race fix and its "
        "rationale comments pushed it just over budget, and splitting the API mid-fix would "
        "cost more coherence than the overage"
    ),
}
SHRINK_LOCKED_MODULES: tuple[str, ...] = (
    "_browser/browser_capture.py",
    "_auth/psidts_recovery.py",
    "_auth/refresh.py",
    "_auth/storage.py",
)


def measure_modules(source_root: Path = SOURCE_ROOT) -> dict[str, int]:
    """Return every package module's splitlines-compatible line count."""
    return {
        path.relative_to(source_root).as_posix(): len(path.read_text(encoding="utf-8").splitlines())
        for path in sorted(source_root.rglob("*.py"))
    }


def derive_module_size() -> dict[str, object]:
    """Derive the global budget plus every live ratchet ceiling."""
    measured = measure_modules()
    invalid_reasons = sorted(
        path
        for path, reason in OVER_BUDGET_EXEMPTIONS.items()
        if not isinstance(reason, str) or not reason.strip()
    )
    if invalid_reasons:
        raise RuntimeError(
            f"over-budget exemptions require a durable authored reason: {invalid_reasons}"
        )
    authored_paths = set(OVER_BUDGET_EXEMPTIONS) | set(SHRINK_LOCKED_MODULES)
    missing = sorted(authored_paths - set(measured))
    if missing:
        raise RuntimeError(f"authored module-size policy paths are stale: {missing}")

    stale_exemptions = sorted(
        path for path in OVER_BUDGET_EXEMPTIONS if measured[path] <= MODULE_SIZE_BUDGET
    )
    if stale_exemptions:
        raise RuntimeError(
            "over-budget exemptions are now at or below the budget; remove their authored "
            f"entries before regenerating: {stale_exemptions}"
        )

    unapproved = {
        path: lines
        for path, lines in measured.items()
        if lines > MODULE_SIZE_BUDGET and path not in authored_paths
    }
    if unapproved:
        raise RuntimeError(
            "modules exceed the budget without an authored exemption or ADR-0033 shrink lock: "
            f"{unapproved}"
        )

    return {
        "budget": MODULE_SIZE_BUDGET,
        "allowlisted_ceilings": {
            path: measured[path]
            for path in sorted(authored_paths)
            if measured[path] > MODULE_SIZE_BUDGET
        },
        "shrink_locked_ceilings": {
            path: measured[path]
            for path in SHRINK_LOCKED_MODULES
            if measured[path] <= MODULE_SIZE_BUDGET
        },
    }


def _integer_mapping(value: object, key: str) -> dict[str, int]:
    if not isinstance(value, dict) or not isinstance(value.get(key), dict):
        raise ValueError(f"module-size baseline must contain a {key!r} mapping")
    mapping = value[key]
    assert isinstance(mapping, dict)
    if not all(isinstance(path, str) and isinstance(lines, int) for path, lines in mapping.items()):
        raise ValueError(f"module-size baseline {key!r} must map paths to integers")
    return mapping


def module_size_growth(previous: object, current: object) -> list[str]:
    """Describe ceiling/budget increases that weaken the shrink-only ratchet."""
    if not isinstance(previous, dict) or not isinstance(current, dict):
        raise ValueError("module-size baseline must be a JSON object")
    previous_budget = previous.get("budget")
    current_budget = current.get("budget")
    if not isinstance(previous_budget, int) or not isinstance(current_budget, int):
        raise ValueError("module-size baseline budget must be an integer")

    growth: list[str] = []
    if current_budget > previous_budget:
        growth.append(f"budget: {previous_budget} -> {current_budget}")

    for section in ("allowlisted_ceilings", "shrink_locked_ceilings"):
        before = _integer_mapping(previous, section)
        after = _integer_mapping(current, section)
        other_section = (
            "shrink_locked_ceilings"
            if section == "allowlisted_ceilings"
            else "allowlisted_ceilings"
        )
        other_before = _integer_mapping(previous, other_section)
        for path, lines in sorted(after.items()):
            old_lines = before.get(path)
            if old_lines is None:
                transition_lines = other_before.get(path)
                if transition_lines is None:
                    growth.append(f"{section}.{path}: new ceiling {lines}")
                elif lines > transition_lines:
                    growth.append(
                        f"{path}: {other_section} {transition_lines} -> {section} {lines}"
                    )
            elif lines > old_lines:
                growth.append(f"{section}.{path}: {old_lines} -> {lines}")

    before_shrink_locked = _integer_mapping(previous, "shrink_locked_ceilings")
    after_shrink_locked = _integer_mapping(current, "shrink_locked_ceilings")
    after_allowlisted = _integer_mapping(current, "allowlisted_ceilings")
    for path in sorted(before_shrink_locked):
        if path not in after_shrink_locked and path not in after_allowlisted:
            growth.append(f"shrink_locked_ceilings.{path}: protection removed")
    return growth


__all__ = [
    "MODULE_SIZE_BUDGET",
    "OVER_BUDGET_EXEMPTIONS",
    "SHRINK_LOCKED_MODULES",
    "derive_module_size",
    "measure_modules",
    "module_size_growth",
]
