"""Tests-only append-only journal for managed E2E generation operations."""

from __future__ import annotations

import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = 1
MODE_ENV = "NOTEBOOKLM_E2E_GENERATION_JOURNAL_MODE"
PATH_ENV = "NOTEBOOKLM_E2E_GENERATION_JOURNAL"
MANAGED_ENV = "NOTEBOOKLM_E2E_MANAGED_COPIES"
GENERATION_ID_ENV = "NOTEBOOKLM_GENERATION_NOTEBOOK_ID"

FAMILIES = frozenset(
    {
        "audio",
        "video",
        "report",
        "quiz",
        "flashcards",
        "mind_map",
        "infographic",
        "slide_deck",
        "data_table",
        "study_guide",
    }
)
SURFACES = frozenset({"client", "cli", "mcp", "rest"})
ID_KINDS = frozenset({"studio_task", "note_mind_map"})
LIFECYCLES = frozenset({"settle", "test_owned"})
EVENTS = frozenset(
    {
        "started",
        "accepted",
        "persisted",
        "completed",
        "discovered_accepted",
        "rate_limited_rejected",
        "quota_no_commit_observed",
        "delete_confirmed",
    }
)
ROW_KEYS = frozenset(
    {
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
)


class JournalConfigurationError(ValueError):
    pass


def _is_under(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def _validate_journal_file(path: Path, runner_temp: Path) -> Path:
    if not _is_under(path, runner_temp):
        raise JournalConfigurationError("required journal must be under RUNNER_TEMP")
    if path.is_symlink() or not path.is_file():
        raise JournalConfigurationError("required journal must be an existing regular file")
    if os.name == "nt" and path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise JournalConfigurationError("required journal must not be a reparse point")
    if os.name != "nt" and stat.S_IMODE(path.stat().st_mode) != 0o600:
        raise JournalConfigurationError("required journal must have mode 0600")
    return path.resolve()


def journal_from_environment(
    *,
    env: dict[str, str] | os._Environ[str] | None = None,
    node_id: str,
) -> GenerationJournal | DisabledJournal:
    values = os.environ if env is None else env
    mode = values.get(MODE_ENV)
    raw_path = values.get(PATH_ENV)
    if mode in (None, ""):
        # PR 1 is opt-in; preserve unmanaged local behavior when no policy is set.
        return DisabledJournal()
    if mode == "off":
        if raw_path:
            raise JournalConfigurationError("journal path must not be set when mode is off")
        return DisabledJournal()
    if mode != "required":
        raise JournalConfigurationError("journal mode must be required or off")
    if values.get(MANAGED_ENV) != "1":
        raise JournalConfigurationError("required journal needs managed-copy activation")
    notebook_id = values.get(GENERATION_ID_ENV, "")
    if not notebook_id:
        raise JournalConfigurationError("required journal needs the managed generation binding")
    runner_temp = values.get("RUNNER_TEMP", "")
    if not runner_temp or not raw_path:
        raise JournalConfigurationError("required journal needs RUNNER_TEMP and a journal path")
    path = _validate_journal_file(Path(raw_path), Path(runner_temp))
    return GenerationJournal(path=path, notebook_id=notebook_id, node_id=node_id)


@contextmanager
def _locked_open(path: Path, flags: int) -> Iterator[int]:
    """Open the journal while holding a platform-safe sidecar lock.

    Windows byte-range locking is not reliable against byte zero of an empty
    append-only file. A private one-byte sidecar gives both platforms a stable
    lock target without placing a non-JSON sentinel in the journal itself.
    """
    lock_path = path.with_name(f".{path.name}.lock")
    if lock_path.is_symlink():
        raise JournalConfigurationError("journal lock must not be a symlink")
    if (
        os.name == "nt"
        and lock_path.exists()
        and lock_path.lstat().st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    ):
        raise JournalConfigurationError("journal lock must not be a reparse point")
    lock_flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        lock_flags |= os.O_NOFOLLOW
        flags |= os.O_NOFOLLOW
    lock_fd = os.open(lock_path, lock_flags, 0o600)
    fd: int | None = None
    try:
        lock_stat = os.fstat(lock_fd)
        if not stat.S_ISREG(lock_stat.st_mode):
            raise JournalConfigurationError("journal lock is not a regular file")
        if os.name != "nt":
            os.fchmod(lock_fd, 0o600)
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            if os.fstat(lock_fd).st_size == 0:
                os.write(lock_fd, b"\0")
                os.fsync(lock_fd)
            os.lseek(lock_fd, 0, os.SEEK_SET)
            msvcrt.locking(lock_fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd = os.open(path, flags)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise JournalConfigurationError("journal is not a regular file")
        yield fd
    finally:
        if fd is not None:
            os.close(fd)
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            try:
                os.lseek(lock_fd, 0, os.SEEK_SET)
                msvcrt.locking(lock_fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


@dataclass(frozen=True)
class _Envelope:
    operation_id: str
    notebook_id: str
    id_kind: str
    family: str
    surface: str
    node_id: str
    lifecycle: str


class JournalOperation:
    """One immutable operation envelope with explicit lifecycle transitions."""

    def __init__(
        self,
        journal: GenerationJournal,
        envelope: _Envelope,
        *,
        prior_events: list[tuple[str, str | None, str | None]] | None = None,
        write_started: bool = True,
    ) -> None:
        self._journal = journal
        self._envelope = envelope
        self._resource_id: str | None = None
        self._events: list[tuple[str, str | None, str | None]] = []
        for event, resource_id, reason in prior_events or []:
            self._record(event, resource_id=resource_id, reason=reason, append=False)
        if write_started:
            self._write("started")

    @property
    def operation_id(self) -> str:
        return self._envelope.operation_id

    @property
    def last_event(self) -> str:
        """Return the last recorded non-sensitive lifecycle event."""
        return self._events[-1][0]

    def _record(
        self,
        event: str,
        *,
        resource_id: str | None = None,
        reason: str | None = None,
        append: bool,
    ) -> None:
        if event not in EVENTS:
            raise ValueError("unknown journal event")
        previous = self._events[-1][0] if self._events else None
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
        if event not in allowed_after[previous]:
            raise ValueError("invalid journal event transition")
        if any(recorded == event for recorded, _, _ in self._events):
            raise ValueError("duplicate journal event")
        if resource_id is not None and (
            not resource_id or self._resource_id not in (None, resource_id)
        ):
            raise ValueError("resource id changed within one journal operation")
        effective_resource = resource_id or self._resource_id
        if event in {"accepted", "persisted", "discovered_accepted", "completed"} and not (
            effective_resource
        ):
            raise ValueError("ID-bearing journal event has no resource id")
        if event == "accepted" and self._envelope.id_kind != "studio_task":
            raise ValueError("accepted is valid only for Studio tasks")
        if event == "persisted" and self._envelope.id_kind != "note_mind_map":
            raise ValueError("persisted is valid only for note-backed mind maps")
        allowed_reasons = {
            "discovered_accepted": {"post_create_quota", "retry_preclean"},
            "rate_limited_rejected": {"typed_pre_acceptance"},
            "quota_no_commit_observed": {"post_create_reconciliation"},
            "delete_confirmed": {"test_teardown", "post_create_quota", "retry_preclean"},
        }.get(event)
        if (allowed_reasons is None and reason is not None) or (
            allowed_reasons is not None and reason not in allowed_reasons
        ):
            raise ValueError("journal event reason is not allowlisted")
        if event == "discovered_accepted" and self._envelope.lifecycle != "test_owned":
            raise ValueError("discovered acceptance must be test-owned")
        if event == "delete_confirmed":
            if self._envelope.lifecycle == "settle" and reason != "retry_preclean":
                raise ValueError("settling resources may be deleted only during retry pre-clean")
            if previous == "discovered_accepted" and self._events[-1][2] != reason:
                raise ValueError("recovery deletion reason changed")

        self._resource_id = effective_resource
        row: dict[str, Any] = {
            "version": VERSION,
            "operation_id": self._envelope.operation_id,
            "event": event,
            "notebook_id": self._envelope.notebook_id,
            "resource_id": self._resource_id,
            "id_kind": self._envelope.id_kind,
            "family": self._envelope.family,
            "surface": self._envelope.surface,
            "node_id": self._envelope.node_id,
            "lifecycle": self._envelope.lifecycle,
            "reason": reason,
        }
        if append:
            self._journal._append(row)
        self._events.append((event, self._resource_id, reason))

    def _write(
        self,
        event: str,
        *,
        resource_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        self._record(event, resource_id=resource_id, reason=reason, append=True)

    def accepted(self, resource_id: str) -> None:
        self._write("accepted", resource_id=resource_id)

    def persisted(self, resource_id: str) -> None:
        self._write("persisted", resource_id=resource_id)

    def completed(self, resource_id: str | None = None) -> None:
        self._write("completed", resource_id=resource_id)

    def discovered_accepted(self, resource_id: str, *, reason: str) -> None:
        if reason not in {"post_create_quota", "retry_preclean"}:
            raise ValueError("discovery reason is not allowlisted")
        self._write("discovered_accepted", resource_id=resource_id, reason=reason)

    def rate_limited_rejected(self) -> None:
        self._write("rate_limited_rejected", reason="typed_pre_acceptance")

    def quota_no_commit_observed(self) -> None:
        self._write("quota_no_commit_observed", reason="post_create_reconciliation")

    def delete_confirmed(self, resource_id: str | None = None, *, reason: str) -> None:
        if reason not in {"test_teardown", "post_create_quota", "retry_preclean"}:
            raise ValueError("delete reason is not allowlisted")
        self._write("delete_confirmed", resource_id=resource_id, reason=reason)


class GenerationJournal:
    def __init__(self, *, path: Path, notebook_id: str, node_id: str) -> None:
        self.path = path
        self.notebook_id = notebook_id
        self.node_id = node_id
        self.surface = "client"

    @contextmanager
    def producer_surface(self, surface: str) -> Iterator[None]:
        if surface not in SURFACES:
            raise ValueError("unknown journal surface")
        previous = self.surface
        self.surface = surface
        try:
            yield
        finally:
            self.surface = previous

    def operation(
        self,
        *,
        notebook_id: str,
        family: str,
        surface: str,
        id_kind: str,
        lifecycle: str,
    ) -> JournalOperation:
        if notebook_id != self.notebook_id:
            raise JournalConfigurationError("journal target is not the managed generation role")
        for value, allowed, label in (
            (family, FAMILIES, "family"),
            (surface, SURFACES, "surface"),
            (id_kind, ID_KINDS, "id kind"),
            (lifecycle, LIFECYCLES, "lifecycle"),
        ):
            if value not in allowed:
                raise ValueError(f"unknown journal {label}")
        if id_kind == "note_mind_map" and family != "mind_map":
            raise ValueError("note-backed journal operations must use the mind_map family")
        return JournalOperation(
            self,
            _Envelope(
                operation_id=str(uuid.uuid4()),
                notebook_id=notebook_id,
                id_kind=id_kind,
                family=family,
                surface=surface,
                node_id=self.node_id,
                lifecycle=lifecycle,
            ),
        )

    def recovery_operation(
        self,
        *,
        resource_id: str,
        notebook_id: str,
        family: str,
        surface: str,
        id_kind: str,
        reason: str,
    ) -> JournalOperation:
        """Resume a prior ID-bearing operation or journal an explicit discovery."""
        if reason not in {"post_create_quota", "retry_preclean"}:
            raise ValueError("discovery reason is not allowlisted")
        rows: list[dict[str, Any]] = []
        try:
            with _locked_open(self.path, os.O_RDONLY) as fd:
                payload = bytearray()
                while chunk := os.read(fd, 65536):
                    payload.extend(chunk)
            rows = [json.loads(raw_line) for raw_line in payload.decode("utf-8").splitlines()]
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, KeyError, AttributeError) as exc:
            raise JournalConfigurationError("journal recovery scan failed") from exc
        for row in rows:
            if not isinstance(row, dict) or set(row) != ROW_KEYS:
                raise JournalConfigurationError("journal recovery row has the wrong schema")
            try:
                canonical_operation_id = str(uuid.UUID(row["operation_id"]))
            except (ValueError, TypeError, AttributeError) as exc:
                raise JournalConfigurationError("journal recovery UUID is malformed") from exc
            if canonical_operation_id != row["operation_id"]:
                raise JournalConfigurationError("journal recovery UUID is not canonical")
        matching_ids = {
            row.get("operation_id") for row in rows if row.get("resource_id") == resource_id
        }
        if len(matching_ids) > 1:
            raise JournalConfigurationError("resource belongs to multiple journal operations")
        if matching_ids:
            operation_id = next(iter(matching_ids))
            operation_rows = [row for row in rows if row.get("operation_id") == operation_id]
            row = operation_rows[0]
            immutable_keys = (
                "operation_id",
                "notebook_id",
                "family",
                "surface",
                "id_kind",
                "lifecycle",
                "node_id",
            )
            if any(
                any(item.get(key) != row.get(key) for key in immutable_keys)
                for item in operation_rows
            ):
                raise JournalConfigurationError("recovered operation envelope changed")
            expected = (notebook_id, family, surface, id_kind)
            observed = (row["notebook_id"], row["family"], row["surface"], row["id_kind"])
            if observed != expected:
                raise JournalConfigurationError("recovered operation envelope does not match")
            try:
                return JournalOperation(
                    self,
                    _Envelope(
                        operation_id=row["operation_id"],
                        notebook_id=notebook_id,
                        id_kind=id_kind,
                        family=family,
                        surface=surface,
                        node_id=row["node_id"],
                        lifecycle=row["lifecycle"],
                    ),
                    prior_events=[
                        (item["event"], item["resource_id"], item["reason"])
                        for item in operation_rows
                    ],
                    write_started=False,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise JournalConfigurationError("recovered operation history is invalid") from exc
        operation = self.operation(
            notebook_id=notebook_id,
            family=family,
            surface=surface,
            id_kind=id_kind,
            lifecycle="test_owned",
        )
        operation.discovered_accepted(resource_id, reason=reason)
        return operation

    def _append(self, row: dict[str, Any]) -> None:
        payload = (json.dumps(row, separators=(",", ":"), sort_keys=True) + "\n").encode()
        with _locked_open(self.path, os.O_WRONLY | os.O_APPEND) as fd:
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError("short journal append")
            os.fsync(fd)


class DisabledOperation:
    operation_id = "disabled"
    last_event = "disabled"

    def accepted(self, resource_id: str) -> None:
        pass

    def persisted(self, resource_id: str) -> None:
        pass

    def completed(self, resource_id: str | None = None) -> None:
        pass

    def discovered_accepted(self, resource_id: str, *, reason: str) -> None:
        pass

    def rate_limited_rejected(self) -> None:
        pass

    def quota_no_commit_observed(self) -> None:
        pass

    def delete_confirmed(self, resource_id: str | None = None, *, reason: str) -> None:
        pass


class DisabledJournal:
    surface = "client"

    @contextmanager
    def producer_surface(self, surface: str) -> Iterator[None]:
        yield

    def operation(self, **kwargs: Any) -> DisabledOperation:
        return DisabledOperation()

    def recovery_operation(self, **kwargs: Any) -> DisabledOperation:
        return DisabledOperation()
