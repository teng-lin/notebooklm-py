#!/usr/bin/env python3
"""Validate and aggregate normalized live-CI phase outcomes."""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass

STATES = {"success", "failure", "not_applicable"}


@dataclass(frozen=True)
class LanePolicy:
    mode: str
    applicable: frozenset[str]
    dependencies: dict[str, tuple[str, ...]]


_BASE_COPY_DEPENDENCIES = {
    "sweep": ("auth",),
    "provision": ("sweep",),
    "preflight": ("provision",),
}

POLICIES = {
    "nightly-web-ubuntu": LanePolicy(
        mode="full",
        applicable=frozenset(
            {
                "auth",
                "sweep",
                "provision",
                "preflight",
                "journal_policy",
                "primary",
                "lastfailed",
                "retry",
                "smoke",
                "coverage",
                "verifier",
                "cleanup",
                "purge",
            }
        ),
        dependencies={
            **_BASE_COPY_DEPENDENCIES,
            "journal_policy": ("preflight",),
            "primary": ("journal_policy",),
            "lastfailed": ("primary",),
            "retry": ("primary", "lastfailed"),
            "smoke": ("preflight",),
            "coverage": ("primary",),
            "verifier": ("journal_policy", "primary", "retry"),
        },
    ),
    "nightly-android-macos": LanePolicy(
        mode="full",
        applicable=frozenset(
            {
                "auth",
                "sweep",
                "provision",
                "preflight",
                "journal_policy",
                "primary",
                "lastfailed",
                "retry",
                "coverage",
                "cleanup",
                "purge",
            }
        ),
        dependencies={
            **_BASE_COPY_DEPENDENCIES,
            "journal_policy": ("preflight",),
            "primary": ("journal_policy",),
            "lastfailed": ("primary",),
            "retry": ("primary", "lastfailed"),
            "coverage": ("primary",),
        },
    ),
    "nightly-readonly-windows": LanePolicy(
        mode="readonly",
        applicable=frozenset(
            {
                "auth",
                "sweep",
                "provision",
                "preflight",
                "journal_policy",
                "primary",
                "lastfailed",
                "retry",
                "coverage",
                "cleanup",
                "purge",
            }
        ),
        dependencies={
            **_BASE_COPY_DEPENDENCIES,
            "journal_policy": ("preflight",),
            "primary": ("journal_policy",),
            "lastfailed": ("primary",),
            "retry": ("primary", "lastfailed"),
            "coverage": ("primary",),
        },
    ),
    "rpc-health-web": LanePolicy(
        mode="rpc",
        applicable=frozenset(
            {
                "auth",
                "sweep",
                "provision",
                "preflight",
                "health",
                "report",
                "cleanup",
                "purge",
            }
        ),
        dependencies={
            **_BASE_COPY_DEPENDENCIES,
            "health": ("preflight",),
            "report": ("health",),
        },
    ),
    "rpc-health-android": LanePolicy(
        mode="template",
        applicable=frozenset({"auth", "template_validate", "health", "report", "purge"}),
        dependencies={
            "template_validate": ("auth",),
            "health": ("template_validate",),
            "report": ("health",),
        },
    ),
    "verify-package": LanePolicy(
        mode="full",
        applicable=frozenset(
            {
                "auth",
                "sweep",
                "provision",
                "preflight",
                "journal_policy",
                "primary",
                "lastfailed",
                "retry",
                "telemetry",
                "cleanup",
                "purge",
            }
        ),
        dependencies={
            **_BASE_COPY_DEPENDENCIES,
            "journal_policy": ("preflight",),
            "primary": ("journal_policy",),
            "lastfailed": ("primary",),
            "retry": ("primary", "lastfailed"),
            "telemetry": ("primary", "retry"),
        },
    ),
}


class OutcomeError(ValueError):
    pass


def parse_phases(values: list[str]) -> dict[str, str]:
    phases: dict[str, str] = {}
    for value in values:
        name, separator, state = value.partition("=")
        if not separator or not name or state not in STATES:
            raise OutcomeError("each phase must be NAME=success|failure|not_applicable")
        if name in phases:
            raise OutcomeError(f"duplicate phase: {name}")
        phases[name] = state
    return phases


def _dependency_allows(phase: str, dependency: str, states: dict[str, str]) -> bool:
    """Return whether a dependent phase may be applicable.

    Report/coverage/verifier/telemetry phases intentionally diagnose a failed
    producer, while ordinary forward dependencies require success.
    """
    if phase in {"report", "coverage"} and dependency in {"health", "primary"}:
        return states[dependency] in {"success", "failure"}
    if phase in {"verifier", "telemetry"} and dependency == "primary":
        return states[dependency] in {"success", "failure"}
    if phase in {"verifier", "telemetry"} and dependency == "retry":
        # Artifact verification and advisory inventory collection diagnose the
        # final producer state, including an attempted retry that failed.
        return states[dependency] in STATES
    if phase == "lastfailed" and dependency == "primary":
        return states[dependency] == "failure"
    if phase == "retry" and dependency == "primary":
        return states[dependency] == "failure"
    return states[dependency] == "success"


def aggregate(*, lane: str, mode: str, states: dict[str, str], producer: bool = True) -> list[str]:
    """Validate a complete lane record and return all failure categories."""
    policy = POLICIES.get(lane)
    if policy is None or policy.mode != mode:
        raise OutcomeError("lane/mode pair is not allowlisted")
    missing = policy.applicable - states.keys()
    extra = states.keys() - policy.applicable
    if missing or extra:
        detail = []
        if missing:
            detail.append("missing=" + ",".join(sorted(missing)))
        if extra:
            detail.append("unknown=" + ",".join(sorted(extra)))
        raise OutcomeError("phase set mismatch (" + "; ".join(detail) + ")")

    errors: list[str] = []
    root_required = policy.applicable - policy.dependencies.keys()
    for phase in root_required:
        if states[phase] == "not_applicable":
            errors.append(f"required_phase_skipped:{phase}")
    for phase, dependencies in policy.dependencies.items():
        if phase == "verifier" and not producer:
            if states[phase] != "not_applicable":
                errors.append("dependency_violation:verifier")
            continue
        if phase == "telemetry":
            # Verify Package inventory is best-effort/non-gating once its
            # producer path was actually reached. It must still be skipped when
            # setup blocked primary, otherwise outcome capture could conceal a
            # workflow dependency bug.
            allowed = all(
                _dependency_allows(phase, dependency, states) for dependency in dependencies
            )
            if not allowed and states[phase] != "not_applicable":
                errors.append("dependency_violation:telemetry")
            continue
        allowed = all(_dependency_allows(phase, dependency, states) for dependency in dependencies)
        if allowed and states[phase] == "not_applicable":
            errors.append(f"required_phase_skipped:{phase}")
        elif not allowed and states[phase] != "not_applicable":
            errors.append(f"dependency_violation:{phase}")

    primary = states.get("primary")
    retry = states.get("retry")
    lastfailed = states.get("lastfailed")
    retry_recovered = primary == "failure" and lastfailed == "success" and retry == "success"
    for phase in sorted(policy.applicable):
        if states[phase] != "failure":
            continue
        if phase == "telemetry":
            continue
        if phase == "primary" and retry_recovered:
            continue
        errors.append(f"phase_failed:{phase}")

    if primary == "failure" and lastfailed == "failure":
        errors.append("retry_unavailable:lastfailed")
    return sorted(set(errors))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lane", required=True, choices=tuple(POLICIES))
    parser.add_argument("--mode", required=True, choices=("full", "readonly", "rpc", "template"))
    parser.add_argument("--phase", action="append", default=[], metavar="NAME=STATE")
    parser.add_argument(
        "--producer",
        choices=("required", "off"),
        default="required",
        help="whether this lane/filter requires journal settlement",
    )
    parser.add_argument(
        "--lastfailed-present",
        choices=("true", "false", "not_applicable"),
        help="compatibility shorthand for the normalized lastfailed phase",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        states = parse_phases(args.phase)
        if args.lastfailed_present is not None:
            if "lastfailed" in states:
                raise OutcomeError("lastfailed was supplied twice")
            states["lastfailed"] = {
                "true": "success",
                "false": "failure",
                "not_applicable": "not_applicable",
            }[args.lastfailed_present]
        if args.producer == "off" and args.lane != "nightly-web-ubuntu":
            raise OutcomeError("producer=off is allowlisted only for filtered Web nightly")
        errors = aggregate(
            lane=args.lane,
            mode=args.mode,
            states=states,
            producer=args.producer == "required",
        )
    except OutcomeError as exc:
        print(f"CONFIGURATION: {exc}", file=sys.stderr)
        return 2
    if errors:
        print("CI E2E outcome failed: " + ", ".join(errors), file=sys.stderr)
        return 1
    print(f"CI E2E outcome succeeded for lane {args.lane}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
