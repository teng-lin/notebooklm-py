"""Registry data for the ``notebooklm download <type>`` leaf commands.

This module is intentionally **data-only** — no Click decorators, no async
runtime calls. The 9 leaf commands (audio / video / slide-deck / infographic
/ report / mind-map / data-table / quiz / flashcards) all share the same
option block and dispatch shape; the only axes of variation are captured
here as ``DownloadTypeSpec`` rows. The ``register_download_command`` factory
in :mod:`notebooklm.cli.download_cmd` builds each Click leaf from one row.

The behavioural rows come from :mod:`notebooklm._app.download_specs`; this
adapter adds only Click help prose and the legacy ``slide_format`` parameter
name. Adding a type or format never requires repeating its extension or MIME.

See also:
    - :mod:`notebooklm._app.download` — the transport-neutral plan / executor
      that consumes ``DownloadTypeSpec`` rows at run time.
    - :mod:`notebooklm.cli.services.download` — the CLI adapter that
      re-exports those types and projects typed results to CLI envelopes.
"""

from __future__ import annotations

from dataclasses import replace

from .._app.download_specs import DOWNLOAD_SPECS_BY_NAME as SHARED_DOWNLOAD_SPECS

# The dataclass + format-extension table live in the transport-neutral
# ``_app.download`` core and are re-exported by ``cli.services.download``.
# Import through the service adapter to preserve the historical CLI import
# surface while keeping this file data-only.
from .services.download import DownloadTypeSpec


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


_HELP_SUMMARY_OVERRIDES: dict[str, str] = {
    "audio": "Download audio overview(s) to file.",
    "video": "Download video overview(s) to file.",
    "slide-deck": "Download slide deck(s) as PDF or PPTX.",
    "infographic": "Download infographic(s) to file.",
    "report": "Download report(s) as markdown files.",
    "mind-map": "Download mind map(s) as JSON files.",
    "data-table": "Download data table(s) as CSV files.",
    "quiz": "Download quiz questions.",
    "flashcards": "Download flashcard deck.",
}


def _format_help(spec: DownloadTypeSpec) -> str:
    """Render Click's format help from the shared legal-value axis."""
    if not spec.format_choices:
        return ""
    values = [f"{spec.format_default} (default)", *spec.format_choices[1:]]
    if len(values) == 2:
        choices = " or ".join(values)
    else:
        choices = f"{', '.join(values[:-1])}, or {values[-1]}"
    label = "Download format" if spec.name == "slide-deck" else "Output format"
    return f"{label}: {choices}"


def _extra_examples(spec: DownloadTypeSpec) -> str:
    """Return the small Click-only example residue for format-bearing leaves."""
    alternatives = spec.format_choices[1:]
    if spec.name == "slide-deck" and alternatives:
        output_format = alternatives[0]
        return (
            f"    # Download as {output_format.upper()}\n"
            f"    notebooklm download slide-deck --format {output_format}"
        )
    if spec.name in {"quiz", "flashcards"} and alternatives:
        stem = "quiz" if spec.name == "quiz" else "cards"
        commands = "\n".join(
            f"    notebooklm download {spec.name} --format {output_format} "
            f"{stem}{spec.format_extension_map[output_format]}"
            for output_format in alternatives
        )
        return (
            f"    # Download as {' or '.join(alternatives)}\n"
            f"{commands}\n\n"
            "    # Machine-readable output\n"
            f"    notebooklm download {spec.name} --json"
        )
    return ""


def _cli_spec(spec: DownloadTypeSpec) -> DownloadTypeSpec:
    """Add Click-only fields to one shared behavioural spec."""
    return replace(
        spec,
        help_summary=_HELP_SUMMARY_OVERRIDES.get(
            spec.name, f"Download {spec.name} artifact(s) to file."
        ),
        help_examples=_stock_examples(
            spec.name,
            spec.extension,
            spec.default_dir,
            extra=_extra_examples(spec),
        ),
        format_help=_format_help(spec),
        # Public compatibility residue from the original hand-written Click leaf.
        format_param_name="slide_format" if spec.name == "slide-deck" else "output_format",
    )


DOWNLOAD_SPECS: list[DownloadTypeSpec] = [
    _cli_spec(spec) for spec in SHARED_DOWNLOAD_SPECS.values()
]


# Quick lookup by name; the cinematic-video alias never appears here because
# it is a pure Click alias to ``download_video`` registered in download_cmd.py.
DOWNLOAD_SPECS_BY_NAME: dict[str, DownloadTypeSpec] = {s.name: s for s in DOWNLOAD_SPECS}
