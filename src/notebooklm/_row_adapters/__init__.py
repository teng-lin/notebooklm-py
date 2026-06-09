"""Private positional-RPC-row adapter package.

Cohesive cluster promoted from the former flat ``_row_adapters_*.py`` modules (issue #1328).
Re-exports the typed row views; importers may also reach submodules directly
(``from .._row_adapters.sources import SourceRow``).
"""

from . import artifacts, chat, labels, notes, sources
from .artifacts import ArtifactRow, ReportSuggestionRow
from .chat import (
    AnswerRow,
    CitationDetail,
    CitationRow,
    ErrorPayloadRow,
    PassageRow,
    StreamFrameRow,
    TextLeafRow,
)
from .labels import LabelRow
from .notes import NoteRow
from .sources import SourceRow, SourceRowShape

__all__ = [
    "artifacts",
    "chat",
    "labels",
    "notes",
    "sources",
    "AnswerRow",
    "ArtifactRow",
    "CitationDetail",
    "CitationRow",
    "ErrorPayloadRow",
    "LabelRow",
    "NoteRow",
    "PassageRow",
    "ReportSuggestionRow",
    "SourceRow",
    "SourceRowShape",
    "StreamFrameRow",
    "TextLeafRow",
]
