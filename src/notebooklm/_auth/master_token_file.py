"""Path-owned persistence for one master-token credential file."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..exceptions import LockUnavailableError
from .credential_io import _commit_master_token_json
from .master_token_types import (
    MasterToken,
    _master_token_from_legacy_record,
    _master_token_to_legacy_record,
)
from .paths import _storage_state_lock_path
from .storage_lock import (
    _LOCK_ACQUIRE_DEADLINE_SECONDS,
    LockRequest,
    LockState,
    StorageLockManager,
)


@dataclass(frozen=True, slots=True, repr=False)
class _MasterTokenRead:
    token: MasterToken
    raw: dict[str, Any] = field(repr=False)


def _ensure_secure_parent_dir(path: Path) -> None:
    """Create the credential parent and best-effort tighten it on POSIX."""
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    if sys.platform != "win32":
        with contextlib.suppress(OSError):
            os.chmod(parent, 0o700)


class MasterTokenFile:
    """Own reads and writes for one explicit, uncanonicalized token path."""

    def __init__(
        self,
        path: Path,
        *,
        locks: StorageLockManager | None = None,
    ) -> None:
        self._path = path
        self._locks = locks if locks is not None else StorageLockManager.process_default()

    def _read_present_with_raw(self) -> _MasterTokenRead:
        """Read one known-present path and pair its typed and legacy forms."""
        raw = json.loads(self._path.read_text(encoding="utf-8"))
        token = _master_token_from_legacy_record(raw)
        return _MasterTokenRead(token=token, raw=raw)

    def _read_with_raw(self) -> _MasterTokenRead | None:
        """Probe once, then read a present path without a second probe."""
        if not self._path.exists():
            return None
        return self._read_present_with_raw()

    def read(self) -> MasterToken | None:
        """Read the typed credential, or ``None`` when the path is absent."""
        result = self._read_with_raw()
        return None if result is None else result.token

    def write(self, token: MasterToken) -> None:
        """Canonically commit one token under its bounded sibling lock."""
        payload = _master_token_to_legacy_record(token)
        _ensure_secure_parent_dir(self._path)
        lock_path = _storage_state_lock_path(self._path)
        request = LockRequest(
            path=lock_path,
            blocking=False,
            operation="write_master_token",
            deadline_seconds=_LOCK_ACQUIRE_DEADLINE_SECONDS,
        )
        with self._locks.acquire(request) as state:
            if state is not LockState.HELD:
                raise LockUnavailableError(
                    f"write_master_token: storage lock unavailable at {lock_path}"
                )
            _commit_master_token_json(self._path, payload)
