"""Registry data for the ``notebooklm download <type>`` leaf commands.

This module is intentionally **data-only** — no Click decorators, no async
runtime calls. The 9 leaf commands (audio / video / slide-deck / infographic
/ report / mind-map / data-table / quiz / flashcards) all share the same
option block and dispatch shape; the only axes of variation are captured
here as ``DownloadTypeSpec`` rows. The ``register_download_command`` factory
in :mod:`notebooklm.cli.download_cmd` builds each Click leaf from one row.

Adding a new download type is a single registry edit + a corresponding
``ArtifactsAPI.download_<name>`` coroutine on the client.

See also:
    - :mod:`notebooklm._app.download` — the transport-neutral plan / executor
      that consumes ``DownloadTypeSpec`` rows at run time.
    - :mod:`notebooklm.cli.services.download` — the CLI adapter that
      re-exports those types and projects typed results to CLI envelopes.
"""

from __future__ import annotations

from ..types import ArtifactType

# The dataclass + format-extension table live in the transport-neutral
# ``_app.download`` core and are re-exported by ``cli.services.download``.
# Import through the service adapter to preserve the historical CLI import
# surface while keeping this file data-only.
from .services.download import FORMAT_EXTENSIONS, DownloadTypeSpec


# Help-example fragments share enough structure that we build them
# programmatically to keep the registry compact and avoid 9-way string-copy
# drift. Each leaf appends a docstring of shape:
#
#     <help_summary>
#
#     \b
#     Examples:
#       <examples body>
#
# The body below is the per-leaf differing portion.
def _stock_examples(name: str, ext: str, default_dir: str, extra: str = "") -> str:
    """Build the canonical "Examples:" block for a leaf command.

    Mirrors the prose the original hand-written leaves used so ``--help``
    output stays familiar to existing users. ``extra`` is appended verbatim
    inside the block (used by quiz/flashcards/slide-deck for format-specific
    lines).
    """
    body = (
        f"    # Download latest {name} to default filename\n"
        f"    notebooklm download {name}\n"
        f"\n"
        f"    # Download to specific path\n"
        f"    notebooklm download {name} my-{name}{ext}\n"
        f"\n"
        f"    # Download all {name} files to directory\n"
        f"    notebooklm download {name} --all {default_dir}/\n"
        f"\n"
        f"    # Download specific artifact by name\n"
        f'    notebooklm download {name} --name "chapter 3"\n'
        f"\n"
        f"    # Preview without downloading\n"
        f"    notebooklm download {name} --all --dry-run"
    )
    if extra:
        body = body + "\n\n" + extra
    return body


DOWNLOAD_SPECS: list[DownloadTypeSpec] = [
    DownloadTypeSpec(
        name="audio",
        kind=ArtifactType.AUDIO,
        extension=".mp3",
        default_dir="./audio",
        download_attr="download_audio",
        help_summary="Download audio overview(s) to file.",
        help_examples=_stock_examples("audio", ".mp3", "./audio"),
    ),
    DownloadTypeSpec(
        name="video",
        kind=ArtifactType.VIDEO,
        extension=".mp4",
        default_dir="./video",
        download_attr="download_video",
        help_summary="Download video overview(s) to file.",
        help_examples=_stock_examples("video", ".mp4", "./video"),
    ),
    DownloadTypeSpec(
        name="slide-deck",
        kind=ArtifactType.SLIDE_DECK,
        extension=".pdf",
        default_dir="./slide-decks",
        download_attr="download_slide_deck",
        format_choices=("pdf", "pptx"),
        format_default="pdf",
        format_help="Download format: pdf (default) or pptx",
        format_extension_map={"pdf": ".pdf", "pptx": ".pptx"},
        format_kwarg="output_format",
        # Click param name is the legacy ``slide_format`` (not ``output_format``)
        # because the original hand-written leaf used that variable name. Kept
        # to avoid churning ``download_cmd.py`` kwargs flowing through the
        # factory.
        format_param_name="slide_format",
        # Slide-deck only binds output_format when the user picks pptx —
        # keep that wiring so the underlying API call stays correct.
        forward_format_only_if_set=True,
        help_summary="Download slide deck(s) as PDF or PPTX.",
        help_examples=_stock_examples(
            "slide-deck",
            ".pdf",
            "./slide-decks",
            extra=("    # Download as PPTX\n    notebooklm download slide-deck --format pptx"),
        ),
    ),
    DownloadTypeSpec(
        name="infographic",
        kind=ArtifactType.INFOGRAPHIC,
        extension=".png",
        default_dir="./infographic",
        download_attr="download_infographic",
        help_summary="Download infographic(s) to file.",
        help_examples=_stock_examples("infographic", ".png", "./infographic"),
    ),
    DownloadTypeSpec(
        name="report",
        kind=ArtifactType.REPORT,
        extension=".md",
        default_dir="./reports",
        download_attr="download_report",
        help_summary="Download report(s) as markdown files.",
        help_examples=_stock_examples("report", ".md", "./reports"),
    ),
    DownloadTypeSpec(
        name="mind-map",
        kind=ArtifactType.MIND_MAP,
        extension=".json",
        default_dir="./mind-maps",
        download_attr="download_mind_map",
        help_summary="Download mind map(s) as JSON files.",
        help_examples=_stock_examples("mind-map", ".json", "./mind-maps"),
    ),
    DownloadTypeSpec(
        name="data-table",
        kind=ArtifactType.DATA_TABLE,
        extension=".csv",
        default_dir="./data-tables",
        download_attr="download_data_table",
        help_summary="Download data table(s) as CSV files.",
        help_examples=_stock_examples("data-table", ".csv", "./data-tables"),
    ),
    DownloadTypeSpec(
        name="quiz",
        kind=ArtifactType.QUIZ,
        extension=".json",
        default_dir="./quizzes",
        download_attr="download_quiz",
        format_choices=("json", "markdown", "html"),
        format_default="json",
        format_help="Output format: json (default), markdown, or html",
        format_extension_map=dict(FORMAT_EXTENSIONS),
        format_kwarg="output_format",
        # Quiz/flashcards always forward the format kwarg so the underlying
        # API serializes the requested representation regardless of default.
        forward_format_only_if_set=False,
        help_summary="Download quiz questions.",
        help_examples=_stock_examples(
            "quiz",
            ".json",
            "./quizzes",
            extra=(
                "    # Download as markdown or html\n"
                "    notebooklm download quiz --format markdown quiz.md\n"
                "    notebooklm download quiz --format html quiz.html\n\n"
                "    # Machine-readable output\n"
                "    notebooklm download quiz --json"
            ),
        ),
    ),
    DownloadTypeSpec(
        name="flashcards",
        kind=ArtifactType.FLASHCARDS,
        extension=".json",
        default_dir="./flashcards",
        download_attr="download_flashcards",
        format_choices=("json", "markdown", "html"),
        format_default="json",
        format_help="Output format: json (default), markdown, or html",
        format_extension_map=dict(FORMAT_EXTENSIONS),
        format_kwarg="output_format",
        forward_format_only_if_set=False,
        help_summary="Download flashcard deck.",
        help_examples=_stock_examples(
            "flashcards",
            ".json",
            "./flashcards",
            extra=(
                "    # Download as markdown or html\n"
                "    notebooklm download flashcards --format markdown cards.md\n"
                "    notebooklm download flashcards --format html cards.html\n\n"
                "    # Machine-readable output\n"
                "    notebooklm download flashcards --json"
            ),
        ),
    ),
]


# Quick lookup by name; the cinematic-video alias never appears here because
# it is a pure Click alias to ``download_video`` registered in download_cmd.py.
DOWNLOAD_SPECS_BY_NAME: dict[str, DownloadTypeSpec] = {s.name: s for s in DOWNLOAD_SPECS}
