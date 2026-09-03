"""Durable local state and title contracts for disposable CI notebooks.

This module intentionally contains no NotebookLM transport code.  Keeping the
manifest and title grammar neutral makes the destructive manager small enough
to audit and lets unit tests exercise crash durability without authentication.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

MANIFEST_VERSION = 1
MANIFEST_REPOSITORY = "teng-lin/notebooklm-py"
RESERVED_PREFIX = "notebooklm-py-ci/"

ACCOUNT_SLOTS = ("A", "B", "C")
BACKENDS = ("web", "android")
LANES = (
    "nightly-web-ubuntu",
    "nightly-android-macos",
    "nightly-readonly-windows",
    "rpc-health-web",
    "rpc-health-android",
    "verify-package",
)
_LEGACY_TITLE_LANES = ("nightly-web-windows", "nightly-android-windows")
LANE_BACKENDS: Mapping[str, str] = {
    "nightly-web-ubuntu": "web",
    "nightly-android-macos": "android",
    "nightly-readonly-windows": "web",
    "rpc-health-web": "web",
    "rpc-health-android": "android",
    "verify-package": "web",
}
ROLES = ("reference", "generation", "multi-source", "rpc")
MODE_ROLES: Mapping[str, tuple[str, ...]] = {
    "full": ("reference", "generation", "multi-source"),
    "readonly": ("reference",),
    "rpc": ("rpc",),
}
_LANE_MODES: Mapping[str, tuple[str, ...]] = {
    "nightly-web-ubuntu": ("full",),
    "nightly-android-macos": ("full",),
    "nightly-readonly-windows": ("readonly",),
    "rpc-health-web": ("rpc",),
    "rpc-health-android": (),
    "verify-package": ("full",),
}
_ROLE_LANES: Mapping[str, tuple[str, ...]] = {
    "reference": (
        "nightly-web-ubuntu",
        "nightly-android-macos",
        "nightly-readonly-windows",
        *_LEGACY_TITLE_LANES,
        "verify-package",
    ),
    "generation": (
        "nightly-web-ubuntu",
        "nightly-android-macos",
        *_LEGACY_TITLE_LANES,
        "verify-package",
    ),
    "multi-source": (
        "nightly-web-ubuntu",
        "nightly-android-macos",
        *_LEGACY_TITLE_LANES,
        "verify-package",
    ),
    "rpc": ("rpc-health-web",),
}
COPY_STATUSES = ("intent", "confirmed", "reconciled", "deleted", "delete_failed")
ERROR_CATEGORIES = (
    "AUTHENTICATION",
    "CLEANUP",
    "CONFIGURATION",
    "COPY_UNRESOLVED",
    "QUOTA",
    "REGRESSION",
    "TEMPLATE_ACCESS",
)

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,256}$")
_TITLE_RE = re.compile(
    rf"^{re.escape(RESERVED_PREFIX)}"
    rf"(?P<run_id>[0-9]+)/(?P<run_attempt>[0-9]+)/"
    rf"(?P<lane>{'|'.join(re.escape(value) for value in (*LANES, *_LEGACY_TITLE_LANES))})/"
    rf"(?P<role>{'|'.join(re.escape(value) for value in ROLES)})/"
    r"(?P<nonce>[0-9a-f]{32})$"
)


class ManifestError(ValueError):
    """The local lifecycle manifest is unsafe or internally inconsistent."""


@dataclass(frozen=True)
class ParsedTitle:
    """Trusted fields decoded from a normative disposable notebook title."""

    run_id: str
    run_attempt: str
    lane: str
    role: str
    nonce: str


def is_valid_notebook_id(value: object) -> bool:
    """Return whether *value* is a minimally well-formed opaque notebook ID."""

    return isinstance(value, str) and bool(_ID_RE.fullmatch(value))


def build_title(run_id: str, run_attempt: str, lane: str, role: str, nonce: str) -> str:
    """Build a title only from fields accepted by :func:`parse_title`."""

    title = f"{RESERVED_PREFIX}{run_id}/{run_attempt}/{lane}/{role}/{nonce}"
    parsed = parse_title(title)
    if parsed is None:
        raise ManifestError("invalid disposable notebook title fields")
    return title


def parse_title(title: object) -> ParsedTitle | None:
    """Parse the exact reserved title grammar, returning ``None`` on mismatch."""

    if not isinstance(title, str):
        return None
    match = _TITLE_RE.fullmatch(title)
    if match is None:
        return None
    parsed = ParsedTitle(**match.groupdict())
    if parsed.lane not in _ROLE_LANES[parsed.role]:
        return None
    return parsed


def _require_decimal(value: object, field: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[0-9]+", value) is None:
        raise ManifestError(f"manifest {field} must be a decimal string")
    return value


def new_manifest(
    *,
    run_id: str,
    run_attempt: str,
    lane: str,
    mode: str,
    account_slot: str,
    backend: str,
    template_fingerprint: str,
) -> dict[str, Any]:
    """Create and validate an empty version-one manifest."""

    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "repository": MANIFEST_REPOSITORY,
        "run_id": run_id,
        "run_attempt": run_attempt,
        "lane": lane,
        "mode": mode,
        "account_slot": account_slot,
        "backend": backend,
        "template_fingerprint": template_fingerprint,
        "copies": [],
    }
    return validate_manifest(manifest)


def validate_manifest(
    manifest: object,
    *,
    template_id: str | None = None,
    expected: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    """Validate every field that can authorize later notebook deletion.

    The returned object is the same dictionary, narrowed for callers.  No
    unknown top-level or copy-row keys are accepted: a newer writer must bump
    the schema rather than silently weakening an older cleanup reader.
    """

    if not isinstance(manifest, dict):
        raise ManifestError("manifest root must be an object")
    required_keys = {
        "version",
        "repository",
        "run_id",
        "run_attempt",
        "lane",
        "mode",
        "account_slot",
        "backend",
        "template_fingerprint",
        "copies",
    }
    if set(manifest) != required_keys:
        raise ManifestError("manifest has missing or unknown fields")
    if manifest["version"] != MANIFEST_VERSION:
        raise ManifestError("unsupported manifest version")
    if manifest["repository"] != MANIFEST_REPOSITORY:
        raise ManifestError("manifest repository mismatch")
    run_id = _require_decimal(manifest["run_id"], "run_id")
    run_attempt = _require_decimal(manifest["run_attempt"], "run_attempt")
    lane = manifest["lane"]
    mode = manifest["mode"]
    account_slot = manifest["account_slot"]
    backend = manifest["backend"]
    fingerprint = manifest["template_fingerprint"]
    if lane not in LANES:
        raise ManifestError("manifest lane is not allowlisted")
    if mode not in MODE_ROLES:
        raise ManifestError("manifest mode is not allowlisted")
    if mode not in _LANE_MODES[lane]:
        raise ManifestError("manifest lane and mode are not a normative combination")
    if any(lane not in _ROLE_LANES[role] for role in MODE_ROLES[mode]):
        raise ManifestError("manifest lane and mode are not a normative combination")
    if account_slot not in ACCOUNT_SLOTS:
        raise ManifestError("manifest account slot is not allowlisted")
    if backend not in BACKENDS:
        raise ManifestError("manifest backend is not allowlisted")
    if LANE_BACKENDS[lane] != backend:
        raise ManifestError("manifest lane and backend are not a normative combination")
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ManifestError("manifest template fingerprint is malformed")
    if expected:
        for field, value in expected.items():
            if field not in required_keys or manifest[field] != value:
                raise ManifestError(f"manifest {field} mismatch")

    copies = manifest["copies"]
    if not isinstance(copies, list):
        raise ManifestError("manifest copies must be a list")
    allowed_roles = MODE_ROLES[mode]
    if len(copies) > len(allowed_roles):
        raise ManifestError("manifest contains too many copy rows")
    copy_keys = {
        "role",
        "title",
        "status",
        "baseline_verified_empty",
        "candidate_notebook_id",
        "notebook_id",
        "prepared",
        "last_error_category",
    }
    seen_roles: set[str] = set()
    seen_ids: dict[str, str] = {}
    for index, row in enumerate(copies):
        if not isinstance(row, dict) or set(row) != copy_keys:
            raise ManifestError("manifest copy row has missing or unknown fields")
        role = row["role"]
        if role != allowed_roles[index] or role in seen_roles:
            raise ManifestError("manifest roles are duplicated or out of order")
        seen_roles.add(role)
        parsed = parse_title(row["title"])
        if (
            parsed is None
            or parsed.run_id != run_id
            or parsed.run_attempt != run_attempt
            or parsed.lane != lane
            or parsed.role != role
        ):
            raise ManifestError("manifest copy title is not normative")
        if row["status"] not in COPY_STATUSES:
            raise ManifestError("manifest copy status is invalid")
        if row["baseline_verified_empty"] is not True:
            raise ManifestError("manifest copy lacks a verified-empty baseline")
        if not isinstance(row["prepared"], bool):
            raise ManifestError("manifest prepared flag must be boolean")
        if row["prepared"] and row["status"] == "intent":
            raise ManifestError("an unconfirmed copy cannot be prepared")
        category = row["last_error_category"]
        if category is not None and category not in ERROR_CATEGORIES:
            raise ManifestError("manifest error category is invalid")
        for field in ("candidate_notebook_id", "notebook_id"):
            notebook_id = row[field]
            if notebook_id is None:
                continue
            if not is_valid_notebook_id(notebook_id):
                raise ManifestError(f"manifest {field} is malformed")
            if template_id is not None and notebook_id == template_id:
                raise ManifestError("manifest attempts to register the template")
            prior_role = seen_ids.get(notebook_id)
            if prior_role is not None and prior_role != role:
                raise ManifestError("manifest notebook IDs are duplicated across roles")
            seen_ids[notebook_id] = role
        if row["status"] in {"confirmed", "reconciled", "deleted"}:
            if row["notebook_id"] is None:
                raise ManifestError("trusted copy status requires a confirmed notebook ID")
        if row["prepared"] and row["notebook_id"] is None:
            raise ManifestError("a prepared copy requires a confirmed notebook ID")
    return manifest


def _is_reparse_point(path: Path) -> bool:
    """Best-effort reparse detection without importing Windows-only modules."""

    file_attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(file_attributes & reparse_flag)


def _validate_runner_temp_location(path: Path, runner_temp: Path | None) -> None:
    if runner_temp is None:
        return
    root = runner_temp.resolve()
    resolved_parent = path.parent.resolve()
    if resolved_parent != root and root not in resolved_parent.parents:
        raise ManifestError("manifest path must stay below RUNNER_TEMP")


def validate_local_path(
    path: Path,
    *,
    runner_temp: Path | None = None,
    windows: bool | None = None,
) -> None:
    """Fail closed on symlink/non-regular state and Windows path escape."""

    windows = os.name == "nt" if windows is None else windows
    _validate_runner_temp_location(path, runner_temp)
    if windows:
        if runner_temp is None:
            raise ManifestError("RUNNER_TEMP is required on Windows")
        for candidate in (path.parent, path):
            if candidate.is_symlink() or (candidate.exists() and _is_reparse_point(candidate)):
                raise ManifestError("manifest path may not contain a reparse point")
    if path.is_symlink():
        raise ManifestError("manifest must be a regular file")
    if path.exists():
        mode = path.lstat().st_mode
        if not stat.S_ISREG(mode):
            raise ManifestError("manifest must be a regular file")
        if not windows and stat.S_IMODE(mode) != 0o600:
            raise ManifestError("manifest file mode must be 0600")
    if path.parent.is_symlink():
        raise ManifestError("manifest parent must be a real directory")
    if path.parent.exists():
        parent_mode = path.parent.lstat().st_mode
        if not stat.S_ISDIR(parent_mode):
            raise ManifestError("manifest parent must be a real directory")
        if not windows and stat.S_IMODE(parent_mode) != 0o700:
            raise ManifestError("manifest parent mode must be 0700")


class AtomicJSONStore:
    """Crash-durable, mode-safe JSON manifest storage."""

    def __init__(
        self,
        path: Path,
        *,
        runner_temp: Path | None = None,
        windows: bool | None = None,
        replace: Callable[[str, str], None] = os.replace,
        fsync: Callable[[int], None] = os.fsync,
    ) -> None:
        self.path = path
        self.runner_temp = runner_temp
        self.windows = os.name == "nt" if windows is None else windows
        self._replace = replace
        self._fsync = fsync

    def read(
        self,
        *,
        template_id: str | None = None,
        expected: Mapping[str, object] | None = None,
    ) -> dict[str, Any]:
        validate_local_path(
            self.path,
            runner_temp=self.runner_temp,
            windows=self.windows,
        )
        try:
            raw = self.path.read_text(encoding="utf-8")
            value = json.loads(raw)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise ManifestError("manifest cannot be read or decoded") from exc
        return validate_manifest(value, template_id=template_id, expected=expected)

    def write(self, manifest: Mapping[str, Any], *, template_id: str | None = None) -> None:
        checked = validate_manifest(dict(manifest), template_id=template_id)
        parent = self.path.parent
        # Reject an escaped runner path before creating its parent. Existing
        # caller-owned directories are validated but never chmod'ed.
        if self.windows and self.runner_temp is None:
            raise ManifestError("RUNNER_TEMP is required on Windows")
        _validate_runner_temp_location(self.path, self.runner_temp)
        try:
            parent.mkdir(mode=0o700, parents=True, exist_ok=False)
            parent_created = True
        except FileExistsError:
            parent_created = False
        if not self.windows:
            parent_mode = parent.lstat().st_mode
            if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
                raise ManifestError("manifest parent must be a real directory")
            if parent_created:
                os.chmod(parent, 0o700)
        validate_local_path(
            self.path,
            runner_temp=self.runner_temp,
            windows=self.windows,
        )
        fd, temp_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=parent)
        try:
            if not self.windows:
                os.fchmod(fd, 0o600)
            payload = json.dumps(checked, sort_keys=True, separators=(",", ":")) + "\n"
            with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
                fd = -1
                handle.write(payload)
                handle.flush()
                self._fsync(handle.fileno())
            self._replace(temp_name, str(self.path))
            if not self.windows:
                os.chmod(self.path, 0o600)
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                directory_fd = os.open(parent, flags)
                try:
                    self._fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if fd >= 0:
                os.close(fd)
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def new_copy_row(*, role: str, title: str) -> dict[str, Any]:
    """Return a complete intent row suitable for an atomic pre-dispatch write."""

    return {
        "role": role,
        "title": title,
        "status": "intent",
        "baseline_verified_empty": True,
        "candidate_notebook_id": None,
        "notebook_id": None,
        "prepared": False,
        "last_error_category": None,
    }


def github_env_lines(mode: str, copies: Sequence[Mapping[str, Any]]) -> list[str]:
    """Build the validated GITHUB_ENV block, keeping activation last."""

    expected_roles = MODE_ROLES.get(mode)
    if expected_roles is None or tuple(row.get("role") for row in copies) != expected_roles:
        raise ManifestError("cannot publish an incomplete role set")
    ids: dict[str, str] = {}
    for row in copies:
        notebook_id = row.get("notebook_id")
        if (
            row.get("status") not in {"confirmed", "reconciled"}
            or row.get("prepared") is not True
            or not is_valid_notebook_id(notebook_id)
        ):
            raise ManifestError("cannot publish an unprepared role")
        ids[str(row["role"])] = str(notebook_id)
    if len(set(ids.values())) != len(ids):
        raise ManifestError("cannot publish duplicate role IDs")
    if mode == "full":
        return [
            f"NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID={ids['reference']}",
            f"NOTEBOOKLM_GENERATION_NOTEBOOK_ID={ids['generation']}",
            f"NOTEBOOKLM_MULTI_SOURCE_NOTEBOOK_ID={ids['multi-source']}",
            "NOTEBOOKLM_E2E_MANAGED_MODE=full",
            "NOTEBOOKLM_E2E_REFERENCE_PREPARED=1",
            "NOTEBOOKLM_E2E_MANAGED_COPIES=1",
        ]
    if mode == "readonly":
        return [
            f"NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID={ids['reference']}",
            "NOTEBOOKLM_E2E_MANAGED_MODE=readonly",
            "NOTEBOOKLM_E2E_REFERENCE_PREPARED=1",
            "NOTEBOOKLM_E2E_MANAGED_COPIES=1",
        ]
    return [
        f"NOTEBOOKLM_READ_ONLY_NOTEBOOK_ID={ids['rpc']}",
        f"NOTEBOOKLM_GENERATION_NOTEBOOK_ID={ids['rpc']}",
        "NOTEBOOKLM_E2E_MANAGED_MODE=rpc",
    ]


def atomic_append_lines(path: Path, lines: Sequence[str]) -> None:
    """Append one logical block through a sibling replacement and directory fsync."""

    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    prefix = existing if not existing or existing.endswith("\n") else existing + "\n"
    payload = prefix + "\n".join(lines) + "\n"
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", closefd=True) as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        if os.name != "nt":
            directory_fd = os.open(parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
