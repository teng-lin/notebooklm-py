#!/usr/bin/env python3
"""Verify legacy or journaled CI artifact generation without logging IDs."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import stat
import sys
import time
import uuid
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from notebooklm import NotebookLMClient
from notebooklm._logging import scrub_secrets

EXPECTED_FAMILIES = {
    "audio",
    "report",
    "video",
    "quiz",
    "flashcards",
    "mind_map",
    "infographic",
    "slide_deck",
    "data_table",
}
MEDIA_FAMILIES = {"audio", "video", "infographic", "slide_deck"}
FAMILIES = EXPECTED_FAMILIES | {"study_guide"}
EVENTS = {
    "started",
    "accepted",
    "persisted",
    "completed",
    "discovered_accepted",
    "rate_limited_rejected",
    "quota_no_commit_observed",
    "delete_confirmed",
}
SURFACES = {"client", "cli", "mcp", "rest"}
ID_KINDS = {"studio_task", "note_mind_map"}
LIFECYCLES = {"settle", "test_owned"}
JOURNAL_KEYS = {
    "version",
    "operation_id",
    "event",
    "notebook_id",
    "resource_id",
    "id_kind",
    "family",
    "surface",
    "node_id",
    "lifecycle",
    "reason",
}

EXIT_CONFIGURATION = 2
EXIT_EMPTY = 3
EXIT_FAILED_MAJORITY = 4
EXIT_REMOVED = 5
EXIT_TIMEOUT = 6
EXIT_READ_AUTH = 7
EXIT_JOURNAL = 8
EXIT_INSUFFICIENT_TIME = 9


class VerificationError(RuntimeError):
    exit_code = 1
    category = "verification"


class ConfigurationError(VerificationError):
    exit_code = EXIT_CONFIGURATION
    category = "configuration"


class EmptyArtifactsError(VerificationError):
    exit_code = EXIT_EMPTY
    category = "empty"


class FailedMajorityError(VerificationError):
    exit_code = EXIT_FAILED_MAJORITY
    category = "failed_majority"


class RemovedArtifactError(VerificationError):
    exit_code = EXIT_REMOVED
    category = "removed"


class SettlementTimeoutError(VerificationError):
    exit_code = EXIT_TIMEOUT
    category = "timeout"


class ReadAuthError(VerificationError):
    exit_code = EXIT_READ_AUTH
    category = "read_or_auth"


class JournalError(VerificationError):
    exit_code = EXIT_JOURNAL
    category = "journal"


class InsufficientTimeError(VerificationError):
    exit_code = EXIT_INSUFFICIENT_TIME
    category = "insufficient_verification_time"


def compute_budget(marker: str | None, *, monotonic_ns: int | None = None) -> int:
    if not marker:
        raise ConfigurationError("job monotonic start marker is missing")
    try:
        started = int(marker)
    except ValueError as exc:
        raise ConfigurationError("job monotonic start marker is malformed") from exc
    now = time.monotonic_ns() if monotonic_ns is None else monotonic_ns
    if started <= 0 or started > now:
        raise ConfigurationError("job monotonic start marker is non-positive or in the future")
    elapsed = (now - started) / 1_000_000_000
    return min(2700, math.floor(21600 - elapsed - 900))


def _family(artifact: Any) -> str:
    value = getattr(artifact, "kind", "unknown")
    return str(getattr(value, "value", value))


async def inventory_compat(client: Any, notebook_id: str) -> dict[str, Any]:
    """Preserve the detached workflow's historical pass/fail thresholds."""
    artifacts = await client.artifacts.list(notebook_id)
    notes = await client.notes.list(notebook_id)
    families = Counter(_family(artifact) for artifact in artifacts)
    completed = sum(bool(artifact.is_completed) for artifact in artifacts)
    processing = sum(bool(artifact.is_processing) for artifact in artifacts)
    failed = sum(bool(artifact.is_failed) for artifact in artifacts)
    print(f"Artifact inventory: total={len(artifacts)}")
    print(
        f"Status summary: completed={completed} processing={processing} failed={failed} "
        f"notes={len(notes)}"
    )
    print("Families: " + (", ".join(sorted(families)) if families else "none"))
    missing = EXPECTED_FAMILIES - families.keys()
    if missing:
        print("WARNING: missing artifact families: " + ", ".join(sorted(missing)))
    if not artifacts:
        raise EmptyArtifactsError("no artifacts found")
    if failed > len(artifacts) // 2:
        raise FailedMajorityError(
            f"failed artifacts exceed the compatibility threshold ({failed}/{len(artifacts)})"
        )
    return {
        "total": len(artifacts),
        "completed": completed,
        "processing": processing,
        "failed": failed,
        "families": sorted(families),
        "notes": len(notes),
    }


@dataclass
class JournalOperation:
    immutable: tuple[str, str, str, str, str, str]
    events: list[tuple[str, str | None, str | None]]

    @property
    def notebook_id(self) -> str:
        return self.immutable[0]

    @property
    def family(self) -> str:
        return self.immutable[1]

    @property
    def id_kind(self) -> str:
        return self.immutable[3]

    @property
    def lifecycle(self) -> str:
        return self.immutable[4]

    @property
    def resource_id(self) -> str | None:
        return next((resource for _, resource, _ in reversed(self.events) if resource), None)

    @property
    def event_names(self) -> set[str]:
        return {event for event, _, _ in self.events}


@dataclass(frozen=True)
class DiscoveredTarget:
    family: str
    id_kind: str


def _public_id_kind(artifact: Any) -> str:
    """Classify a merged public-inventory row by its persisted backing."""
    if (
        _family(artifact) == "mind_map"
        and not bool(getattr(artifact, "is_interactive_mind_map", False))
        and not bool(getattr(artifact, "is_unclassified_type4", False))
    ):
        return "note_mind_map"
    return "studio_task"


def _matches_operation_target(operation: JournalOperation, artifact: Any) -> bool:
    expected_family = "report" if operation.family == "study_guide" else operation.family
    return _family(artifact) == expected_family and _public_id_kind(artifact) == operation.id_kind


def _has_untracked_operation_target(
    operation: JournalOperation,
    inventory: dict[str, Any],
    tracked_ids: set[str],
) -> bool:
    return any(
        resource_id not in tracked_ids and _matches_operation_target(operation, artifact)
        for resource_id, artifact in inventory.items()
    )


def _validate_transition(
    operation: JournalOperation,
    event: str,
    resource: str | None,
    reason: str | None,
) -> None:
    previous = operation.event_names
    prior = operation.events[-1][0] if operation.events else None
    allowed_after = {
        None: {"started"},
        "started": {
            "accepted",
            "persisted",
            "discovered_accepted",
            "rate_limited_rejected",
            "quota_no_commit_observed",
        },
        "accepted": {"completed", "delete_confirmed"},
        "persisted": {"completed", "delete_confirmed"},
        "completed": {"delete_confirmed"},
        "discovered_accepted": {"delete_confirmed"},
        "rate_limited_rejected": set(),
        "quota_no_commit_observed": set(),
        "delete_confirmed": set(),
    }
    if event not in allowed_after[prior]:
        raise JournalError("operation contains an invalid event transition")
    if event in previous:
        raise JournalError("operation contains a duplicate event")
    known = operation.resource_id
    if resource is not None and known not in (None, resource):
        raise JournalError("operation resource ID changed")
    if event in {"accepted", "persisted", "discovered_accepted", "completed"} and not resource:
        raise JournalError("ID-bearing event has no resource ID")
    if event == "accepted" and operation.id_kind != "studio_task":
        raise JournalError("accepted is valid only for Studio tasks")
    if event == "persisted" and operation.id_kind != "note_mind_map":
        raise JournalError("persisted is valid only for note-backed maps")
    if event == "completed" and not (previous & {"accepted", "persisted"}):
        raise JournalError("completed precedes acceptance/persistence")
    if event == "discovered_accepted" and "started" not in previous:
        raise JournalError("discovered acceptance has no started event")
    if event == "discovered_accepted" and operation.lifecycle != "test_owned":
        raise JournalError("discovered acceptance is not test-owned")
    if event == "delete_confirmed" and not (
        previous & {"accepted", "persisted", "discovered_accepted", "completed"}
    ):
        raise JournalError("delete confirmation has no ID-bearing predecessor")
    if (
        event == "delete_confirmed"
        and operation.lifecycle == "settle"
        and reason != "retry_preclean"
    ):
        raise JournalError("settling resource has an unauthorized deletion reason")
    if event == "delete_confirmed" and prior == "discovered_accepted":
        previous_reason = operation.events[-1][2]
        if previous_reason != reason:
            raise JournalError("recovery deletion reason changed")


def parse_journal(path: Path, *, notebook_id: str) -> dict[str, JournalOperation]:
    if path.is_symlink() or not path.is_file():
        raise JournalError("journal is missing or is not a regular file")
    if os.name == "nt" and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise JournalError("journal is a reparse point")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise JournalError("journal mode is not 0600")
    operations: dict[str, JournalOperation] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise JournalError("journal could not be read") from exc
    if not lines:
        raise JournalError("journal is empty")
    for line_number, line in enumerate(lines, 1):
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise JournalError(f"journal line {line_number} is not JSON") from exc
        if not isinstance(row, dict) or set(row) != JOURNAL_KEYS:
            raise JournalError(f"journal line {line_number} has the wrong schema")
        if row["version"] != 1 or row["event"] not in EVENTS:
            raise JournalError(f"journal line {line_number} has an unknown version/event")
        if row["notebook_id"] != notebook_id:
            raise JournalError("journal contains a different notebook binding")
        if row["family"] not in FAMILIES or row["surface"] not in SURFACES:
            raise JournalError("journal contains an unknown family/surface")
        if row["id_kind"] not in ID_KINDS or row["lifecycle"] not in LIFECYCLES:
            raise JournalError("journal contains an unknown ID kind/lifecycle")
        if row["id_kind"] == "note_mind_map" and row["family"] != "mind_map":
            raise JournalError("note-backed operation does not use the mind_map family")
        if not isinstance(row["node_id"], str) or not row["node_id"]:
            raise JournalError("journal contains an empty pytest node ID")
        try:
            operation_uuid = str(uuid.UUID(row["operation_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise JournalError("journal contains a malformed operation UUID") from exc
        if operation_uuid != row["operation_id"]:
            raise JournalError("journal operation UUID is not canonical")
        resource = row["resource_id"]
        reason = row["reason"]
        if resource is not None and (not isinstance(resource, str) or not resource):
            raise JournalError("journal contains a malformed resource ID")
        if reason is not None and not isinstance(reason, str):
            raise JournalError("journal contains a malformed reason")
        expected_reasons = {
            "discovered_accepted": {"post_create_quota", "retry_preclean"},
            "rate_limited_rejected": {"typed_pre_acceptance"},
            "quota_no_commit_observed": {"post_create_reconciliation"},
            "delete_confirmed": {"test_teardown", "post_create_quota", "retry_preclean"},
        }
        allowed_reasons = expected_reasons.get(row["event"])
        if (allowed_reasons is None and reason is not None) or (
            allowed_reasons is not None and reason not in allowed_reasons
        ):
            raise JournalError("journal event has an invalid reason")
        immutable = (
            row["notebook_id"],
            row["family"],
            row["surface"],
            row["id_kind"],
            row["lifecycle"],
            row["node_id"],
        )
        operation = operations.setdefault(
            operation_uuid, JournalOperation(immutable=immutable, events=[])
        )
        if operation.immutable != immutable:
            raise JournalError("immutable operation fields changed")
        _validate_transition(operation, row["event"], resource, reason)
        operation.events.append((row["event"], resource, reason))
    resource_owners: dict[str, str] = {}
    for operation_id, operation in operations.items():
        resource_id = operation.resource_id
        if resource_id is None:
            continue
        owner = resource_owners.setdefault(resource_id, operation_id)
        if owner != operation_id:
            raise JournalError("resource ID is claimed by multiple operations")
    return operations


def generation_id_from_manifest(path: Path, role: str) -> str:
    if role != "generation":
        raise ConfigurationError("journal verifier role must be generation")
    if path.is_symlink() or not path.is_file():
        raise ConfigurationError("managed-copy manifest is not a regular file")
    if os.name == "nt" and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise ConfigurationError("managed-copy manifest is a reparse point")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise ConfigurationError("managed-copy manifest mode is not 0600")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigurationError("managed-copy manifest is unreadable") from exc
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ConfigurationError("managed-copy manifest has an unknown schema")
    copies = document.get("copies")
    if not isinstance(copies, list):
        raise ConfigurationError("managed-copy manifest has no copy list")
    matching = [copy for copy in copies if isinstance(copy, dict) and copy.get("role") == role]
    if (
        len(matching) != 1
        or not isinstance(matching[0].get("notebook_id"), str)
        or not matching[0]["notebook_id"]
    ):
        raise ConfigurationError("managed-copy manifest has no unique confirmed generation role")
    if matching[0].get("status") not in {"confirmed", "reconciled"}:
        raise ConfigurationError("managed generation role is not confirmed")
    return matching[0]["notebook_id"]


def _inventory_signature(artifacts: list[Any]) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        sorted((artifact.id, _family(artifact), artifact.status_str) for artifact in artifacts)
    )


def _snapshot_status(artifact: Any) -> str:
    """Derive polling status from one public inventory snapshot."""
    status = str(artifact.status_str)
    if (
        status == "completed"
        and _family(artifact) in MEDIA_FAMILIES
        and getattr(artifact, "url", None) is None
    ):
        return "in_progress"
    return status


async def _discover_quiet_inventory(
    client: Any,
    notebook_id: str,
    *,
    deadline: float,
    clock: Any,
    sleep: Any,
    minimum_window: float,
    quiet_polls: int,
    poll_interval: float,
) -> list[Any]:
    started = clock()
    previous: tuple[tuple[str, str, str], ...] | None = None
    stable = 0
    latest: list[Any] = []
    while True:
        latest = await client.artifacts.list(notebook_id)
        signature = _inventory_signature(latest)
        if clock() - started >= minimum_window and signature == previous:
            stable += 1
        elif clock() - started >= minimum_window:
            stable = 1
        else:
            stable = 0
        previous = signature
        if stable >= quiet_polls:
            return latest
        if clock() + poll_interval > deadline:
            raise SettlementTimeoutError("inventory did not reach the discovery quiet window")
        await sleep(poll_interval)


async def verify_journal(
    client: Any,
    *,
    notebook_id: str,
    journal_path: Path,
    timeout: float,
    clock: Any = time.monotonic,
    sleep: Any = asyncio.sleep,
    minimum_discovery_window: float = 120.0,
    quiet_polls: int = 3,
    poll_interval: float = 30.0,
    not_found_grace: int = 5,
) -> dict[str, Any]:
    if timeout < 240:
        raise InsufficientTimeError("effective verifier budget is below 240 seconds")
    operations = parse_journal(journal_path, notebook_id=notebook_id)
    deadline = clock() + timeout
    inventory = await _discover_quiet_inventory(
        client,
        notebook_id,
        deadline=deadline,
        clock=clock,
        sleep=sleep,
        minimum_window=minimum_discovery_window,
        quiet_polls=quiet_polls,
        poll_interval=poll_interval,
    )
    by_id = {artifact.id: artifact for artifact in inventory}
    accepted_operations = [
        operation
        for operation in operations.values()
        if operation.event_names & {"accepted", "persisted", "completed", "discovered_accepted"}
    ]

    authorized_deleted = {
        operation.resource_id
        for operation in operations.values()
        if "delete_confirmed" in operation.event_names
    }
    if authorized_deleted & by_id.keys():
        raise JournalError("delete-confirmed resource is still present")
    tracked = {operation.resource_id: operation for operation in accepted_operations}
    tracked.pop(None, None)
    tracked_ids = set(tracked)
    quota_no_commit_operations = [
        operation
        for operation in operations.values()
        if operation.lifecycle == "test_owned"
        and "quota_no_commit_observed" in operation.event_names
        and not (operation.event_names & {"delete_confirmed", "rate_limited_rejected"})
    ]
    unresolved_test_owned = [
        operation
        for operation in operations.values()
        if operation.lifecycle == "test_owned"
        and not (operation.event_names & {"delete_confirmed", "rate_limited_rejected"})
        and (
            "quota_no_commit_observed" not in operation.event_names
            or _has_untracked_operation_target(operation, by_id, tracked_ids)
        )
    ]
    if unresolved_test_owned:
        raise JournalError("test-owned operation has no verified deletion")
    for resource_id, operation in tracked.items():
        if resource_id in authorized_deleted:
            continue
        artifact = by_id.get(resource_id)
        if artifact is not None:
            observed_family = _family(artifact)
            expected = "report" if operation.family == "study_guide" else operation.family
            if observed_family != expected:
                raise JournalError("journaled artifact family does not match inventory")

    # Adopt crash-before-append rows from the clean notebook inventory.
    unjournaled = {
        resource_id: DiscoveredTarget(
            family=_family(artifact),
            id_kind=_public_id_kind(artifact),
        )
        for resource_id, artifact in by_id.items()
        if resource_id not in tracked
    }
    unjournaled_ids = set(unjournaled)
    unmatched_started = [
        operation for operation in operations.values() if operation.event_names == {"started"}
    ]
    unmatched_candidates = set(unjournaled_ids)
    for operation in unmatched_started:
        expected = "report" if operation.family == "study_guide" else operation.family
        candidates = {
            resource_id
            for resource_id in unmatched_candidates
            if unjournaled[resource_id].family == expected
            and unjournaled[resource_id].id_kind == operation.id_kind
        }
        if len(candidates) != 1:
            raise JournalError("unmatched started operation could not be uniquely reconciled")
        unmatched_candidates.remove(candidates.pop())
    if unmatched_candidates:
        raise JournalError("inventory artifact has no matching journal start")
    accepted_count = len(accepted_operations) + len(unjournaled_ids)
    if accepted_count == 0:
        raise EmptyArtifactsError("journal/inventory has no accepted producer operations")
    ever_visible_ids = set(by_id)
    monitored_ids = (set(tracked) - authorized_deleted) | unjournaled_ids
    statuses: dict[str, str] = {}
    missing_counts: Counter[str] = Counter()
    while True:
        current_inventory = await client.artifacts.list(notebook_id)
        current_by_id = {artifact.id: artifact for artifact in current_inventory}
        current_ids = set(current_by_id)
        if any(
            _has_untracked_operation_target(operation, current_by_id, tracked_ids)
            for operation in quota_no_commit_operations
        ):
            raise JournalError("test-owned operation has no verified deletion")
        if authorized_deleted & current_ids:
            raise JournalError("delete-confirmed resource is still present")
        if current_ids - tracked_ids - unjournaled_ids:
            raise JournalError("inventory artifact has no matching journal start")
        for resource_id, operation in tracked.items():
            artifact = current_by_id.get(resource_id)
            if artifact is None or resource_id in authorized_deleted:
                continue
            observed_family = _family(artifact)
            expected = "report" if operation.family == "study_guide" else operation.family
            if observed_family != expected:
                raise JournalError("journaled artifact family does not match inventory")

        statuses.clear()
        for resource_id in monitored_ids:
            artifact = current_by_id.get(resource_id)
            if artifact is None:
                missing_counts[resource_id] += 1
                status = (
                    "removed" if missing_counts[resource_id] >= not_found_grace else "not_found"
                )
            else:
                missing_counts[resource_id] = 0
                status = _snapshot_status(artifact)
            statuses[resource_id] = status

        removed = sum(status == "removed" for status in statuses.values())
        failed = sum(status == "failed" for status in statuses.values())
        completed = sum(status == "completed" for status in statuses.values())
        pending = len(statuses) - removed - failed - completed
        if removed:
            removed_ids = {
                resource_id for resource_id, status in statuses.items() if status == "removed"
            }
            if removed_ids & ever_visible_ids:
                raise RemovedArtifactError("one or more accepted artifacts were delisted")
            raise RemovedArtifactError("one or more accepted artifacts were removed")
        ever_visible_ids.update(current_ids)
        if pending == 0:
            never_visible = (set(tracked) - authorized_deleted) - ever_visible_ids
            if never_visible:
                raise RemovedArtifactError("one or more accepted artifacts never became listable")
            if not (monitored_ids & ever_visible_ids):
                raise EmptyArtifactsError("generation copy has no discovered persistent artifacts")
            denominator = completed + failed
            if denominator == 0:
                raise EmptyArtifactsError("no terminal artifacts were observed")
            if failed > denominator // 2:
                raise FailedMajorityError(
                    f"failed artifacts exceed terminal majority ({failed}/{denominator})"
                )
            families = {_family(artifact) for artifact in current_inventory}
            missing = EXPECTED_FAMILIES - families
            if missing:
                print("WARNING: missing artifact families: " + ", ".join(sorted(missing)))
            print(
                f"Journal verification: accepted={accepted_count} "
                f"completed={completed} failed={failed} pending=0 removed=0"
            )
            print("Families: " + ", ".join(sorted(families)))
            return {
                "accepted": accepted_count,
                "completed": completed,
                "failed": failed,
                "pending": 0,
                "removed": 0,
                "families": sorted(families),
            }
        if clock() + poll_interval > deadline:
            raise SettlementTimeoutError(
                f"artifact settlement timed out (completed={completed} failed={failed} "
                f"pending={pending})"
            )
        await sleep(poll_interval)


async def _run_client(args: argparse.Namespace) -> None:
    async with NotebookLMClient.from_storage(backend=args.backend) as client:
        if args.mode == "inventory-compat":
            notebook_id = os.environ.get(args.notebook_id_env, "")
            if not notebook_id:
                raise ConfigurationError("generation notebook binding is empty")
            await inventory_compat(client, notebook_id)
            return
        notebook_id = generation_id_from_manifest(args.manifest, args.role)
        await verify_journal(
            client,
            notebook_id=notebook_id,
            journal_path=args.journal,
            timeout=args.timeout,
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compute-budget", action="store_true")
    parser.add_argument("--mode", choices=("inventory-compat", "journal"))
    parser.add_argument("--backend", choices=("web", "android"))
    parser.add_argument("--notebook-id-env")
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--role")
    parser.add_argument("--journal", type=Path)
    parser.add_argument("--timeout", type=float)
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if args.compute_budget:
        if any(
            value is not None
            for value in (
                args.mode,
                args.backend,
                args.notebook_id_env,
                args.manifest,
                args.role,
                args.journal,
                args.timeout,
            )
        ):
            raise ConfigurationError("--compute-budget cannot be combined with verification")
        return
    if args.mode is None or args.backend is None:
        raise ConfigurationError("verification requires --mode and --backend")
    if args.mode == "inventory-compat":
        if args.notebook_id_env is None:
            raise ConfigurationError("inventory compatibility requires --notebook-id-env")
        if args.notebook_id_env != "NOTEBOOKLM_GENERATION_NOTEBOOK_ID":
            raise ConfigurationError("notebook ID env name is not allowlisted")
        if any(
            value is not None for value in (args.manifest, args.role, args.journal, args.timeout)
        ):
            raise ConfigurationError("inventory compatibility received journal-only arguments")
    elif any(value is None for value in (args.manifest, args.role, args.journal, args.timeout)):
        raise ConfigurationError("journal mode requires manifest, role, journal, and timeout")
    elif not math.isfinite(args.timeout) or args.timeout < 240:
        # This check deliberately runs before NotebookLMClient.from_storage so
        # an exhausted job budget goes directly to teardown without auth/read IO.
        raise InsufficientTimeError("effective verifier budget is below 240 seconds")
    else:
        if os.environ.get("NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE") != "required":
            raise ConfigurationError("journal verification requires journal mode=required")
        configured = os.environ.get("NOTEBOOKLM_E2E_GENERATION_JOURNAL")
        if not configured or Path(configured).resolve() != args.journal.resolve():
            raise ConfigurationError("journal argument does not match the required journal binding")
        runner_temp = os.environ.get("RUNNER_TEMP")
        if not runner_temp:
            raise ConfigurationError("journal verification requires RUNNER_TEMP")
        runner_directory = Path(runner_temp).resolve()
        for label, path in (("journal", args.journal), ("manifest", args.manifest)):
            try:
                path.resolve().relative_to(runner_directory)
            except ValueError as exc:
                raise ConfigurationError(f"required {label} is outside RUNNER_TEMP") from exc


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        _validate_args(args)
        if args.compute_budget:
            print(compute_budget(os.environ.get("NOTEBOOKLM_CI_JOB_STARTED_MONOTONIC_NS")))
            return 0
        asyncio.run(_run_client(args))
    except VerificationError as exc:
        print(f"{exc.category}: {scrub_secrets(exc)}", file=sys.stderr)
        return exc.exit_code
    except Exception:
        # Arbitrary upstream exception text can carry resource IDs even after
        # credential scrubbing. The category is actionable without echoing it.
        print("read_or_auth: artifact inventory request failed", file=sys.stderr)
        return EXIT_READ_AUTH
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
