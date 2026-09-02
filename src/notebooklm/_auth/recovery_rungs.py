"""Neutral process-level registry for optional authentication recovery rungs."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Protocol


class HeadlessRungStatus(Enum):
    """Terminal classification of an installed headless recovery rung."""

    SUCCEEDED = "succeeded"
    UNAVAILABLE = "unavailable"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HeadlessRungOutcome:
    """Credential-free result returned by a headless recovery rung."""

    status: HeadlessRungStatus
    reason: str

    @property
    def succeeded(self) -> bool:
        """Whether the rung persisted fresh authentication material."""
        return self.status is HeadlessRungStatus.SUCCEEDED


class HeadlessRung(Protocol):
    """Optional blocking L3 implementation installed by the public facade."""

    def __call__(
        self,
        *,
        storage_path: Path,
        allow_headless: bool,
    ) -> HeadlessRungOutcome: ...


_RUNG_LOCK = threading.Lock()
_installed_rung: HeadlessRung | None = None


def install_headless_rung(rung: HeadlessRung | None) -> HeadlessRung | None:
    """Install ``rung`` process-wide and return the previous implementation."""
    global _installed_rung
    with _RUNG_LOCK:
        previous = _installed_rung
        _installed_rung = rung
    return previous


def installed_headless_rung() -> HeadlessRung | None:
    """Return the currently installed process-level headless recovery rung."""
    with _RUNG_LOCK:
        return _installed_rung
