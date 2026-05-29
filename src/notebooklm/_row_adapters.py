"""Compatibility re-exports for NotebookLM row adapters.

The domain implementations live in ``_row_adapters_artifacts``,
``_row_adapters_notes``, and ``_row_adapters_sources``. This module remains
as the import-compatibility shim for existing private callers and tests that
still import from ``notebooklm._row_adapters``.
"""

from __future__ import annotations

from ._row_adapters_artifacts import ArtifactRow
from ._row_adapters_notes import NoteRow
from ._row_adapters_sources import SourceRow, SourceRowShape

__all__ = ["ArtifactRow", "NoteRow", "SourceRow", "SourceRowShape"]
