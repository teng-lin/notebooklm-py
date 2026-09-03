#!/usr/bin/env python3
"""Select one opaque CI account slot without reading any credentials."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

SCHEMA_VERSION = 1
ALLOWED_SLOTS = ("A", "B", "C")
LANE_OFFSETS = {
    "nightly-web-ubuntu": 0,
    "nightly-android-macos": 1,
    "nightly-readonly-windows": 2,
    "rpc-health-web": 2,
    "rpc-health-android": 3,
    "verify-package": 0,
}
_EPOCH = date(1970, 1, 1)


class ConfigurationError(ValueError):
    """The non-secret pool configuration is invalid."""


def utc_epoch_day(value: str) -> int:
    """Return the zero-based UTC epoch day for an ISO calendar date."""
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ConfigurationError("rotation day must be an ISO date (YYYY-MM-DD)") from exc
    return (parsed - _EPOCH).days


def parse_enabled_slots(value: str | None) -> tuple[str, ...]:
    """Parse the exact ordered subset of compile-time account aliases."""
    if value is None or not value:
        raise ConfigurationError("NOTEBOOKLM_CI_ACCOUNT_SLOTS is missing or empty")
    # The longest valid spelling is A,B,C. This also rejects whitespace damage
    # before splitting, without accepting a potentially surprising normalization.
    if len(value) > len(",".join(ALLOWED_SLOTS)) or any(char.isspace() for char in value):
        raise ConfigurationError("enabled slots contain whitespace or are overlong")
    slots = value.split(",")
    if any(not slot for slot in slots):
        raise ConfigurationError("enabled slots contain an empty member")
    if any(slot not in ALLOWED_SLOTS for slot in slots):
        raise ConfigurationError("enabled slots must be an ordered subset of A,B,C")
    if len(set(slots)) != len(slots):
        raise ConfigurationError("enabled slots must be distinct")
    canonical_order = tuple(slot for slot in ALLOWED_SLOTS if slot in slots)
    if tuple(slots) != canonical_order:
        raise ConfigurationError("enabled slots must preserve A,B,C order")
    return tuple(slots)


def select_account(
    *,
    enabled_slots: str | None,
    lane: str,
    rotation_day: str,
    manual_base: str = "auto",
) -> dict[str, str]:
    """Return the stable four-field selection record."""
    slots = parse_enabled_slots(enabled_slots)
    if lane not in LANE_OFFSETS:
        raise ConfigurationError("lane is not allowlisted")
    day = utc_epoch_day(rotation_day)
    if manual_base == "auto":
        base_index = day % len(slots)
    else:
        if manual_base not in ALLOWED_SLOTS:
            raise ConfigurationError("manual base must be auto, A, B, or C")
        if manual_base not in slots:
            raise ConfigurationError("manual base slot is not enabled")
        base_index = slots.index(manual_base)
    selected = slots[(base_index + LANE_OFFSETS[lane]) % len(slots)]
    return {
        "account_slot": selected,
        "master_token_secret_name": f"NOTEBOOKLM_MASTER_TOKEN_JSON_{selected}",
        "lane": lane,
        "rotation_day": rotation_day,
    }


def append_github_output(path: Path, record: dict[str, str]) -> None:
    """Append the allowlisted record to an explicitly named GitHub output file."""
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        for key in (
            "account_slot",
            "master_token_secret_name",
            "lane",
            "rotation_day",
        ):
            stream.write(f"{key}={record[key]}\n")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enabled-slots", required=True)
    parser.add_argument("--lane", required=True, choices=tuple(LANE_OFFSETS))
    parser.add_argument("--rotation-day", required=True)
    parser.add_argument("--manual-base", default="auto", choices=("auto", *ALLOWED_SLOTS))
    parser.add_argument("--github-output", type=Path)
    parser.add_argument("--json", action="store_true", help="emit compact JSON on stdout")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        record = select_account(
            enabled_slots=args.enabled_slots,
            lane=args.lane,
            rotation_day=args.rotation_day,
            manual_base=args.manual_base,
        )
        if args.github_output is not None:
            append_github_output(args.github_output, record)
    except (ConfigurationError, OSError) as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(record, separators=(",", ":"), sort_keys=True))
    else:
        print(
            f"Selected account slot {record['account_slot']} for {record['lane']} "
            f"on {record['rotation_day']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
