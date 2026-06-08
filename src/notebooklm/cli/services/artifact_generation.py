"""CLI adapter for artifact-generation retry/wait — thin re-export over ``_app``.

The retry-with-backoff loop, the wait-for-completion orchestration, the typed
:class:`GenerationOutcome`, the status-extraction helpers, and the spinner
status-line formatter now live in the transport-neutral
:mod:`notebooklm._app.generate_retry`. This module re-exports them so existing
``notebooklm.cli.services.artifact_generation`` importers (the command layer in
``cli/generate_cmd.py`` and the direct-import tests in
``tests/unit/cli/test_generate.py``) keep resolving unchanged — including the
private ``_extract_task_id`` / ``_format_status_message`` symbols the tests
reach for by attribute.
"""

from __future__ import annotations

from ..._app.generate_retry import (
    _TYPICAL_DURATIONS as _TYPICAL_DURATIONS,
)
from ..._app.generate_retry import (
    RETRY_BACKOFF_MULTIPLIER,
    RETRY_INITIAL_DELAY,
    RETRY_MAX_DELAY,
    GenerationOutcome,
    calculate_backoff_delay,
    generate_with_retry,
    generation_outcome_from_status,
    handle_generation_result,
)
from ..._app.generate_retry import (
    _extract_generation_task_id as _extract_generation_task_id,
)
from ..._app.generate_retry import (
    _extract_task_id as _extract_task_id,
)
from ..._app.generate_retry import (
    _format_status_message as _format_status_message,
)
from ..._app.generate_retry import (
    _null_wait_context as _null_wait_context,
)

__all__ = [
    "RETRY_BACKOFF_MULTIPLIER",
    "RETRY_INITIAL_DELAY",
    "RETRY_MAX_DELAY",
    "GenerationOutcome",
    "calculate_backoff_delay",
    "generate_with_retry",
    "generation_outcome_from_status",
    "handle_generation_result",
]
