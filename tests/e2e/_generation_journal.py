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
def _locked_append(path: Path) -> Iterator[int]:
    flags = os.O_WRONLY | os.O_APPEND
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        yield fd
    finally:
        if os.name == "nt":  # pragma: no cover - exercised on Windows CI
            import msvcrt

            try:
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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
        resource_id: str | None = None,
        write_started: bool = True,
    ) -> None:
        self._journal = journal
        self._envelope = envelope
        self._resource_id = resource_id
        if write_started:
            self._write("started")

    @property
    def operation_id(self) -> str:
        return self._envelope.operation_id

    def _write(
        self,
        event: str,
        *,
        resource_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        if event not in EVENTS:
            raise ValueError("unknown journal event")
        if resource_id is not None:
            if not resource_id or self._resource_id not in (None, resource_id):
                raise ValueError("resource id changed within one journal operation")
            self._resource_id = resource_id
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
        self._journal._append(row)

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
        matches: dict[str, dict[str, Any]] = {}
        try:
            for raw_line in self.path.read_text(encoding="utf-8").splitlines():
                row = json.loads(raw_line)
                if row.get("resource_id") == resource_id:
                    matches[row["operation_id"]] = row
        except (OSError, json.JSONDecodeError, KeyError, AttributeError) as exc:
            raise JournalConfigurationError("journal recovery scan failed") from exc
        if len(matches) > 1:
            raise JournalConfigurationError("resource belongs to multiple journal operations")
        if matches:
            row = next(iter(matches.values()))
            expected = (notebook_id, family, surface, id_kind)
            observed = (row["notebook_id"], row["family"], row["surface"], row["id_kind"])
            if observed != expected:
                raise JournalConfigurationError("recovered operation envelope does not match")
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
                resource_id=resource_id,
                write_started=False,
            )
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
        with _locked_append(self.path) as fd:
            written = os.write(fd, payload)
            if written != len(payload):
                raise OSError("short journal append")
            os.fsync(fd)


class DisabledOperation:
    operation_id = "disabled"

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
