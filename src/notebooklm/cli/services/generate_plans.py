"""CLI adapter for ``generate`` plan-building — thin re-export over ``_app``.

The plan-construction half of the ``generate`` service — the enum/format maps,
the :class:`GenerationPlan` dataclass, the :class:`GenerationPlanValidationError`,
:func:`build_generation_plan`, and the per-kind builders it dispatches to — now
lives in the transport-neutral :mod:`notebooklm._app.generate`. This module
re-exports those names so existing ``notebooklm.cli.services.generate_plans``
importers (and the ``GenerationKind`` / ``GenerationPlan`` /
``GenerationPlanValidationError`` re-export chain through
``cli/services/generate.py``) keep resolving unchanged.

``_INFOGRAPHIC_STYLE_MAP`` is re-exported (via the redundant-alias explicit
re-export idiom) because ``cli/generate_cmd.py`` imports the private name
directly through the ``cli/services/generate.py`` re-export.
"""

from __future__ import annotations

from ..._app.generate_plans import (
    _INFOGRAPHIC_STYLE_MAP as _INFOGRAPHIC_STYLE_MAP,
)
from ..._app.generate_plans import (
    GenerationKind,
    GenerationPlan,
    GenerationPlanValidationError,
    build_generation_plan,
)

__all__ = [
    "GenerationKind",
    "GenerationPlan",
    "GenerationPlanValidationError",
    "build_generation_plan",
]
