"""Explicit web-companion Drive export compatibility."""

from __future__ import annotations

from .._backend import BackendAdapter
from .._deadline import RuntimeDeadline
from .._semantic.records import ARTIFACT_EXPORT_DEF, DriveExportInput, DriveExportResult


class DriveExportService:
    """Export report/data-table representations without genericizing Drive."""

    __slots__ = ("_backend",)

    def __init__(self, backend: BackendAdapter) -> None:
        self._backend = backend

    async def export(
        self,
        value: DriveExportInput,
        *,
        deadline: RuntimeDeadline | None = None,
    ) -> DriveExportResult:
        return await self._backend.invoke(ARTIFACT_EXPORT_DEF, value, deadline=deadline)


__all__ = ["DriveExportService"]
