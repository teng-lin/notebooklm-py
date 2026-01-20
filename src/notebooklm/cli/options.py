"""Shared CLI option decorators.

Provides reusable option decorators to reduce boilerplate in commands.
"""

import click


def notebook_option(f):
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


def json_option(f):
    """Add --json output flag."""
    return click.option(
        "--json",
        "json_output",
        is_flag=True,
        help="Output as JSON",
    )(f)


def wait_option(f):
    """Add --wait/--no-wait flag for generation commands."""
    return click.option(
        "--wait/--no-wait",
        default=False,
        help="Wait for completion (default: no-wait)",
    )(f)


def source_option(f):
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


def artifact_option(f):
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


def output_option(f):
    """Add --output/-o option for output file path."""
    return click.option(
        "-o",
        "--output",
        "output_path",
        type=click.Path(),
        default=None,
        help="Output file path",
    )(f)


def retry_option(f):
    """Add --retry option for rate limit retry with exponential backoff."""
    return click.option(
        "--retry",
        "max_retries",
        type=int,
        default=0,
        help="Retry N times with exponential backoff on rate limit",
    )(f)


# Composite decorators for common patterns


def standard_options(f):
    """Apply notebook + json options (most common pattern)."""
    return notebook_option(json_option(f))


def generate_options(f):
    """Apply notebook + json + wait + retry options for generation commands."""
    return notebook_option(json_option(wait_option(retry_option(f))))
