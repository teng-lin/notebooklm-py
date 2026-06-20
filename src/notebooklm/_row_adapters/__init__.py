"""Private positional-RPC-row adapter package.

Cohesive cluster promoted from the former flat ``_row_adapters_*.py`` modules (issue #1328).
Re-exports the typed row views; importers may also reach submodules directly
(``from .._row_adapters.sources import SourceRow``).
"""

from . import artifacts, chat, labels, notebooks, notes, research, sources
from .artifacts import ArtifactRow, ReportSuggestionRow
from .chat import (
    AnswerRow,
    CitationDetail,
    CitationRow,
    ConversationTurnRow,
    ErrorPayloadRow,
    PassageRow,
    StreamFrameRow,
    TextLeafRow,
    unwrap_conversation_turns,
)
from .labels import LabelRow
from .notes import NoteRow
from .research import (
    ImportedSourceRow,
    ResearchResultRow,
    ResearchStartRow,
    ResearchTaskInfoRow,
    ResearchTaskRow,
    unwrap_import_rows,
    unwrap_poll_tasks,
)
from .sources import SourceRow, SourceRowShape

__all__ = [
    "artifacts",
    "chat",
    "labels",
    "notebooks",
    "notes",
    "research",
    "sources",
    "AnswerRow",
    "ArtifactRow",
    "CitationDetail",
    "CitationRow",
    "ConversationTurnRow",
    "ErrorPayloadRow",
    "ImportedSourceRow",
    "LabelRow",
    "NoteRow",
    "PassageRow",
    "ReportSuggestionRow",
    "ResearchResultRow",
    "ResearchStartRow",
    "ResearchTaskInfoRow",
    "ResearchTaskRow",
    "SourceRow",
    "SourceRowShape",
    "StreamFrameRow",
    "TextLeafRow",
    "unwrap_conversation_turns",
    "unwrap_import_rows",
    "unwrap_poll_tasks",
]
