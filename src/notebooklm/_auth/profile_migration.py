"""Legacy account migration components for profile persistence."""

from __future__ import annotations

import atexit
import copy
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, TypeAlias

from filelock import FileLock

from .._atomic_io import atomic_write_json
from .profile_account import AccountView, KeepAccount, ProfileAccount
from .profile_store import LoginWriteRequest, ProfileStore, ReplaceResult, ReplaceStatus

logger = logging.getLogger("notebooklm.auth")

_ACCOUNT_CONTEXT_KEY = "account"


@dataclass(frozen=True, slots=True, repr=False)
class InBandAccount:
    record: ProfileAccount


@dataclass(frozen=True, slots=True, repr=False)
class LegacyAccount:
    record: ProfileAccount


@dataclass(frozen=True, slots=True)
class NoAccount:
    pass


ResolvedAccount: TypeAlias = InBandAccount | LegacyAccount | NoAccount


@dataclass(frozen=True, slots=True, repr=False)
class Promoted:
    authoritative: ProfileAccount


@dataclass(frozen=True, slots=True)
class AlreadyInBand:
    pass


@dataclass(frozen=True, slots=True)
class NoLegacyRecord:
    pass


@dataclass(frozen=True, slots=True, repr=False)
class PromotionFailed:
    authoritative_after_failure: ProfileAccount | None = field(default=None, repr=False)


PromotionResult: TypeAlias = Promoted | AlreadyInBand | NoLegacyRecord | PromotionFailed


class LegacyAccountContext:
    """Own legacy ``context.json`` account reads and best-effort scrubs."""

    @staticmethod
    def _path(storage_path: Path) -> Path:
        return storage_path.with_name("context.json")

    def read(self, storage_path: Path) -> dict[str, Any] | None:
        context_path = self._path(storage_path)
        if not context_path.exists():
            return None
        try:
            data = json.loads(context_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("account metadata read failed at %s: %s", context_path, exc)
            return None
        if not isinstance(data, dict):
            return None
        account = data.get(_ACCOUNT_CONTEXT_KEY)
        if not isinstance(account, dict) or not account:
            return None
        return copy.deepcopy(account)

    def scrub(self, storage_path: Path) -> None:
        context_path = self._path(storage_path)
        if not context_path.exists():
            return
        lock_path = context_path.with_suffix(context_path.suffix + ".lock")
        try:
            with FileLock(str(lock_path), timeout=10.0):
                if not context_path.exists():
                    return
                try:
                    data = json.loads(context_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    logger.debug("legacy account-key cleanup skipped at %s: %s", context_path, exc)
                    return
                if not isinstance(data, dict) or _ACCOUNT_CONTEXT_KEY not in data:
                    return
                del data[_ACCOUNT_CONTEXT_KEY]
                if data:
                    atomic_write_json(context_path, data)
                else:
                    context_path.unlink()
        except OSError as exc:
            logger.debug("legacy account-key cleanup failed at %s: %s", context_path, exc)


class LegacyAccountMigrator:
    """Own lossless account resolution and embed-before-scrub promotion."""

    def __init__(self, contexts: LegacyAccountContext | None = None) -> None:
        self._contexts = contexts if contexts is not None else LegacyAccountContext()

    @staticmethod
    def _project_in_band(document: Any) -> tuple[InBandAccount, dict[str, Any]] | None:
        if document is None:
            return None
        payload = document.to_json()
        namespace = payload.get("notebooklm")
        account = namespace.get(_ACCOUNT_CONTEXT_KEY) if isinstance(namespace, dict) else None
        if not isinstance(account, dict) or not account:
            return None
        record = document.account_for(AccountView.ROUTE)
        if record is None:  # pragma: no cover - a non-empty mapping always has a route view
            raise AssertionError("present account must have a route projection")
        return InBandAccount(record), copy.deepcopy(account)

    @staticmethod
    def _sanitize(raw: dict[str, Any]) -> tuple[ProfileAccount, dict[str, Any]]:
        raw_authuser = raw.get("authuser")
        authuser = raw_authuser if type(raw_authuser) is int and raw_authuser >= 0 else 0
        raw_email = raw.get("email")
        email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
        compatibility: dict[str, Any] = {"authuser": authuser}
        if email is not None:
            compatibility["email"] = email
        return ProfileAccount(authuser=authuser, email=email), compatibility

    def _resolve_with_projection(
        self,
        store: ProfileStore,
    ) -> tuple[ResolvedAccount, dict[str, Any]]:
        first = self._project_in_band(store._read_account_document())
        if first is not None:
            return first

        legacy = self._contexts.read(store.path)

        second = self._project_in_band(store._read_account_document())
        if second is not None:
            return second

        if legacy is None:
            return NoAccount(), {}
        record, compatibility = self._sanitize(legacy)
        return LegacyAccount(record), compatibility

    def resolve(self, store: ProfileStore) -> ResolvedAccount:
        result, _compatibility = self._resolve_with_projection(store)
        return result

    def promote(self, store: ProfileStore) -> PromotionResult:
        if not store.path.exists():
            return NoLegacyRecord()
        legacy = self._contexts.read(store.path)
        if legacy is None:
            return NoLegacyRecord()
        record, _compatibility = self._sanitize(legacy)
        try:
            promoted = store.update_account(record, only_if_absent=True)
        except Exception as exc:  # noqa: BLE001 - migration is best-effort for ordinary failures
            logger.warning("Legacy account promotion failed for %s: %s", store.path, exc)
            return PromotionFailed()

        try:
            self._contexts.scrub(store.path)
        except Exception as exc:  # noqa: BLE001 - a committed binding remains authoritative
            logger.warning("Legacy account context cleanup failed for %s: %s", store.path, exc)
        if promoted:
            logger.info("Promoted legacy account metadata in-band for %s", store.path)
            return Promoted(record)
        return AlreadyInBand()

    def scrub(self, store: ProfileStore) -> None:
        self._contexts.scrub(store.path)


ThreadFactory: TypeAlias = Callable[..., threading.Thread]


class LegacyPromotionScheduler:
    """Own the process one-shot registry and detached promotion workers."""

    _process_default_scheduler: ClassVar[LegacyPromotionScheduler]

    def __init__(self, thread_factory: ThreadFactory | None = None) -> None:
        self._registry_lock = threading.Lock()
        self._once_paths: set[str] = set()
        self._workers: set[threading.Thread] = set()
        self._thread_factory = threading.Thread if thread_factory is None else thread_factory

    @classmethod
    def process_default(cls) -> LegacyPromotionScheduler:
        return cls._process_default_scheduler

    def schedule(self, store: ProfileStore, migrator: LegacyAccountMigrator) -> bool:
        canonical = str(store.ordering_key)
        with self._registry_lock:
            if canonical in self._once_paths:
                return False
            self._once_paths.add(canonical)
            worker = self._thread_factory(
                target=self._run,
                args=(store, migrator),
                name="notebooklm-account-promotion",
                daemon=True,
            )
            self._workers.add(worker)
            worker.start()
            return True

    def _run(self, store: ProfileStore, migrator: LegacyAccountMigrator) -> None:
        try:
            migrator.promote(store)
        except BaseException as exc:  # noqa: BLE001 - detached workers must not escape
            logger.debug("Background legacy account promotion crashed for %s: %s", store.path, exc)
        finally:
            with self._registry_lock:
                self._workers.discard(threading.current_thread())

    def drain(self, timeout_per_worker: float) -> None:
        with self._registry_lock:
            workers = list(self._workers)
        for worker in workers:
            worker.join(timeout_per_worker)

    def _scheduled_paths_for_tests(self) -> frozenset[str]:
        with self._registry_lock:
            return frozenset(self._once_paths)

    def _workers_for_tests(self) -> frozenset[threading.Thread]:
        with self._registry_lock:
            return frozenset(self._workers)

    def _reset_for_tests(self) -> None:
        with self._registry_lock:
            if self._workers:
                raise RuntimeError("promotion workers must be drained before reset")
            self._once_paths.clear()


LegacyPromotionScheduler._process_default_scheduler = LegacyPromotionScheduler()

_PROMOTION_EXIT_JOIN_SECONDS = 2.0


@atexit.register
def _drain_promotions_at_exit() -> None:
    LegacyPromotionScheduler.process_default().drain(_PROMOTION_EXIT_JOIN_SECONDS)


class LoginProfileWriter:
    """Compose one login replacement with post-commit legacy reconciliation."""

    def __init__(self, store: ProfileStore, migrator: LegacyAccountMigrator) -> None:
        self._store = store
        self._migrator = migrator

    def write(self, request: LoginWriteRequest) -> ReplaceResult:
        source = request.source.to_json()
        namespace = source.get("notebooklm")
        promote = type(request.account) is KeepAccount and not (
            type(namespace) is dict and _ACCOUNT_CONTEXT_KEY in namespace
        )

        result = self._store.replace_from_login(request)
        if result.status is not ReplaceStatus.APPLIED:
            return result
        if promote:
            self._migrator.promote(self._store)
        else:
            self._migrator.scrub(self._store)
        return result


class AccountMetadataWriter:
    """Compose account writes and clears with post-operation legacy scrub."""

    def __init__(self, store: ProfileStore, migrator: LegacyAccountMigrator) -> None:
        self._store = store
        self._migrator = migrator

    def write(self, record: ProfileAccount) -> None:
        self._store.update_account(record, only_if_absent=False)
        self._migrator.scrub(self._store)

    def clear(self) -> None:
        self._store.clear_account()
        self._migrator.scrub(self._store)


def _drop_legacy_account_key(storage_path: Path) -> None:
    """Compatibility scrub function retained as an exact storage alias."""
    LegacyAccountContext().scrub(storage_path)
