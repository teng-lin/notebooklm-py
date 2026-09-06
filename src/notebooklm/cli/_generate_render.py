"""CLI-owned generation labels, duration hints, and exit policy."""

from __future__ import annotations

from collections.abc import Mapping

from .._app.generate_retry import GenerationOutcome, GenerationWaitStarted
from .._app.generation_requests import GenerationKind, GenerationRequest, ReportGenerationRequest
from ..types import ReportFormat

_DISPLAY_NAME: Mapping[str, str] = {
    "audio": "audio",
    "video": "video",
    "cinematic-video": "video",
    "slide-deck": "slide deck",
    "revise-slide": "slide revision",
    "quiz": "quiz",
    "flashcards": "flashcards",
    "infographic": "infographic",
    "data-table": "data table",
    "mind-map": "mind map",
}

_REPORT_DISPLAY: Mapping[ReportFormat, str] = {
    ReportFormat.BRIEFING_DOC: "briefing document",
    ReportFormat.STUDY_GUIDE: "study guide",
    ReportFormat.BLOG_POST: "blog post",
    ReportFormat.CUSTOM: "custom report",
}

_TYPICAL_DURATIONS: Mapping[GenerationKind, str] = {
    "audio": "typically 2-5 min",
    "video": "typically 5-15 min",
    "cinematic-video": "typically 30-40 min",
    "slide-deck": "typically 1-3 min",
    "quiz": "typically 30-60 sec",
    "flashcards": "typically 30-60 sec",
    "infographic": "typically 1-3 min",
    "data-table": "typically 30-90 sec",
    "mind-map": "typically 30-90 sec",
    "report": "typically 1-3 min",
}


def generation_display_name(request: GenerationRequest) -> str:
    """Return the CLI label for a typed request."""

    if isinstance(request, ReportGenerationRequest):
        return _REPORT_DISPLAY[request.report_format]
    return _DISPLAY_NAME[request.kind]


def format_generation_wait(event: GenerationWaitStarted) -> str:
    """Render the spinner line from a semantic executor event."""

    label = _DISPLAY_NAME.get(event.kind, event.kind.replace("-", " "))
    hint = _TYPICAL_DURATIONS.get(event.kind)
    suffix = f" ({hint})" if hint else ""
    base = f"Waiting for {label} generation{suffix}..."
    if event.elapsed <= 0:
        return base
    return f"{base} [{int(event.elapsed)}s elapsed]"


def generation_exit_code(outcome: GenerationOutcome) -> int:
    """Apply the CLI failure exit policy."""

    return 1 if outcome.status in {"failed", "rate_limited"} else 0


__all__ = [
    "_DISPLAY_NAME",
    "_REPORT_DISPLAY",
    "_TYPICAL_DURATIONS",
    "format_generation_wait",
    "generation_display_name",
    "generation_exit_code",
]
