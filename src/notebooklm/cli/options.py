"""Shared CLI option decorators.

Provides reusable option decorators to reduce boilerplate in commands.
"""

import click
from click.decorators import FC


def notebook_option(f: FC) -> FC:
    """Add --notebook/-n option for notebook ID.

    The option defaults to None, allowing context-based resolution.
    Supports partial ID matching (e.g., 'abc' matches 'abc123...').
    """
    return click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        help="Notebook ID (uses current if not set). Supports partial IDs.",
    )(f)


def json_option(f: FC) -> FC:
    """Add --json output flag."""
    return click.option(
        "--json",
        "json_output",
        is_flag=True,
        help="Output as JSON",
    )(f)


def wait_option(f: FC) -> FC:
    """Add --wait/--no-wait flag for generation commands."""
    return click.option(
        "--wait/--no-wait",
        default=False,
        help="Wait for completion (default: no-wait)",
    )(f)


def source_option(f: FC) -> FC:
    """Add --source/-s option for source ID.

    Supports partial ID matching (e.g., 'abc' matches 'abc123...').
    """
    return click.option(
        "-s",
        "--source",
        "source_id",
        required=True,
        help="Source ID. Supports partial IDs.",
    )(f)


def artifact_option(f: FC) -> FC:
    """Add --artifact/-a option for artifact ID.

    Supports partial ID matching (e.g., 'abc' matches 'abc123...').
    """
    return click.option(
        "-a",
        "--artifact",
        "artifact_id",
        required=True,
        help="Artifact ID. Supports partial IDs.",
    )(f)


def output_option(f: FC) -> FC:
    """Add --output/-o option for output file path."""
    return click.option(
        "-o",
        "--output",
        "output_path",
        type=click.Path(),
        default=None,
        help="Output file path",
    )(f)


def prompt_file_option(f: FC) -> FC:
    """Add --prompt-file option for reading prompt/query text from a file."""
    return click.option(
        "--prompt-file",
        "prompt_file",
        type=click.Path(exists=True, dir_okay=False),
        default=None,
        help="Read prompt/query text from a file instead of the positional argument",
    )(f)


def retry_option(f: FC) -> FC:
    """Add --retry option for rate limit retry with exponential backoff."""
    return click.option(
        "--retry",
        "max_retries",
        type=int,
        default=0,
        help="Retry N times with exponential backoff on rate limit",
    )(f)


# Composite decorators for common patterns


def standard_options(f: FC) -> FC:
    """Apply notebook + json options (most common pattern)."""
    return notebook_option(json_option(f))


def generate_options(f: FC) -> FC:
    """Apply notebook + json + wait + retry options for generation commands."""
    return notebook_option(json_option(wait_option(retry_option(f))))
