"""Shared CLI option decorators.

Provides reusable option decorators to reduce boilerplate in commands.

Shell completion
-----------------------------

The ``-n/--notebook``, ``-s/--source``, and ``-a/--artifact`` options below
attach Click ``shell_complete`` callbacks that emit live IDs from the active
profile. Activate completion in your shell once (see ``docs/cli-reference.md``),
then ``notebooklm <cmd> -n <TAB>`` will list real notebook IDs.

The callbacks are intentionally **best-effort**: if auth is missing, the
network is offline, or any exception fires, they return ``[]`` so the user
just gets no suggestions instead of an error printed by their shell. This
keeps tab-completion safe to use even in fresh shells without credentials.
"""

from collections.abc import Callable

import click
from click.decorators import FC

from . import completion as _completion


def _complete_notebooks(ctx, param, incomplete):
    """Best-effort ``shell_complete`` for the ``-n/--notebook`` option.

    Lists notebooks in the active profile, filters by ``incomplete`` prefix,
    and returns up to 50 ``CompletionItem`` rows (id + title shown as the
    description).

    Failure mode: returns ``[]`` on any exception. Shell completion runs in
    a fresh subprocess invoked by the user's shell on every TAB; raising or
    printing here would surface as garbage in the user's terminal. Network
    failures, missing auth, and rate limits all degrade silently to "no
    suggestions" — exactly what the user expects when ``notebooklm list``
    would also fail.
    """
    return _completion.complete_notebooks(ctx, incomplete)


def _resolve_notebook_for_completion(ctx) -> str | None:
    """Resolve the notebook id usable for sub-resource completion.

    Walks the same precedence ladder as ``helpers.require_notebook`` but
    silently — completion must never raise. Order:

    1. ``-n/--notebook`` flag value already parsed into the current command
       context (or any parent Click context — important when the flag is
       declared on the top-level group).
    2. ``NOTEBOOKLM_NOTEBOOK`` environment variable.
    3. The persisted active-notebook context written by ``notebooklm use``.

    Returns ``None`` when no notebook can be resolved, in which case the
    caller should return an empty completion list rather than guess.
    """
    return _completion.resolve_notebook(ctx)


def _complete_sources(ctx, param, incomplete):
    """Best-effort ``shell_complete`` for the ``-s/--source`` option.

    Resolves the active notebook (flag > env > context), then lists its
    sources and filters by ``incomplete`` prefix. Returns ``[]`` on any
    failure — see ``_complete_notebooks`` for the rationale.
    """
    return _completion.complete_sources(
        ctx,
        incomplete,
        notebook_resolver=_resolve_notebook_for_completion,
    )


def _complete_artifacts(ctx, param, incomplete):
    """Best-effort ``shell_complete`` for the ``-a/--artifact`` option.

    Same shape as ``_complete_sources`` but lists artifacts in the resolved
    notebook. Returns ``[]`` on any failure.
    """
    return _completion.complete_artifacts(
        ctx,
        incomplete,
        notebook_resolver=_resolve_notebook_for_completion,
    )


def notebook_option(f: FC) -> FC:
    """Add --notebook/-n option for notebook ID.

    The option defaults to None and falls back to the ``NOTEBOOKLM_NOTEBOOK``
    environment variable before context-based resolution kicks in
    inside ``helpers.require_notebook``. Click's native ``envvar=`` wiring is
    used so the binding shows up in ``--help`` automatically (``show_envvar=True``)
    and so the env value reaches the command body via the same ``notebook_id``
    kwarg that the flag would, with no per-command boilerplate.

    Supports partial ID matching (e.g., 'abc' matches 'abc123...').

    Tab completion: when shell completion is activated for
    ``notebooklm`` (see ``docs/cli-reference.md``), ``-n <TAB>`` lists real
    notebook IDs from the active profile. Best-effort — returns no
    suggestions on auth / network failure.
    """
    return click.option(
        "-n",
        "--notebook",
        "notebook_id",
        default=None,
        envvar="NOTEBOOKLM_NOTEBOOK",
        show_envvar=True,
        help="Notebook ID (uses current if not set). Supports partial IDs.",
        shell_complete=_complete_notebooks,
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


def wait_polling_options(
    default_timeout: int = 300,
    default_interval: int = 2,
) -> Callable[[FC], FC]:
    """Bundle the shared ``--timeout`` / ``--interval`` polling flags.

    Used by every long-running CLI command so the flag surface stays uniform
    across ``generate <kind> --wait``, ``artifact wait``, and ``source wait``.
    Returns a decorator so each call site can supply its own historical
    defaults without diverging on flag name or help text.

    The ``--wait`` flag is intentionally NOT bundled here. It is a *trigger*
    flag on ``generate <kind>`` (paired with ``wait_option`` /
    ``generate_options``) and is implicit on ``artifact wait`` /
    ``source wait`` (those subcommands ARE the wait). Bundling ``--wait``
    here would either force-add it to commands that don't need it, or
    interact awkwardly with ``--wait/--no-wait``'s tri-state default on
    ``generate``. Keeping the trigger separate makes the surface uniform
    and honest about intent.

    Args:
        default_timeout: Default value for ``--timeout`` in seconds. Each
            command keeps its own historical default (e.g. ``generate audio``
            uses 300, ``source wait`` uses 120) so this PR is purely
            additive — no command changes its existing wait ceiling.
        default_interval: Default value for ``--interval`` in seconds. Most
            commands use 2 to match the existing ``artifact wait`` default;
            ``source wait`` uses 1 to match its underlying
            ``wait_until_ready`` default.

    Returns:
        A decorator that adds ``--timeout`` and ``--interval`` Click options
        to the wrapped command. The wrapped function gains two kwargs:
        ``timeout`` (int) and ``interval`` (int).

    Example:
        @click.command()
        @wait_polling_options(default_timeout=600, default_interval=2)
        def my_long_running_cmd(timeout: int, interval: int) -> None:
            ...
    """

    def decorator(f: FC) -> FC:
        f = click.option(
            "--interval",
            default=default_interval,
            # ``IntRange(min=1)`` rejects 0/negative at parse time — otherwise
            # the poll loop would either busy-spin (interval=0) or sleep
            # backwards (interval<0); both surface as opaque runtime errors.
            type=click.IntRange(min=1),
            help=f"Seconds between status checks (default: {default_interval})",
        )(f)
        f = click.option(
            "--timeout",
            default=default_timeout,
            type=int,
            help=f"Maximum seconds to wait (default: {default_timeout})",
        )(f)
        return f

    return decorator


def source_option(f: FC) -> FC:
    """Add --source/-s option for source ID.

    Supports partial ID matching (e.g., 'abc' matches 'abc123...').

    Tab completion: when shell completion is activated, ``-s
    <TAB>`` lists source IDs from the resolved active notebook. Resolution
    follows the same precedence as the command body (``-n`` flag > env >
    persisted context); without a resolvable notebook the completer returns
    no suggestions silently.
    """
    return click.option(
        "-s",
        "--source",
        "source_id",
        required=True,
        help="Source ID. Supports partial IDs.",
        shell_complete=_complete_sources,
    )(f)


def multi_source_option(f: FC) -> FC:
    """Add the ``--source/-s`` option used by ``generate <kind>`` commands.

    Multi-valued, optional ``--source`` flag that collects source IDs into a
    ``source_ids`` tuple (matches the ``multiple=True`` shape used by every
    ``generate`` subcommand pre-extraction). Distinct from
    :func:`source_option`, which is single-valued and required (used by
    ``download source-content`` and friends).

    Tab completion follows the same notebook-resolution rules as
    :func:`source_option`.
    """
    # Decl order matches the pre-extraction inline ``@click.option("--source",
    # "-s", "source_ids", ...)`` form in cli/generate_cmd.py. Click preserves
    # decl order in ``param.opts`` and the CLI-contract baseline pins it; the
    # long-flag-first order must stay even though the single-source
    # :func:`source_option` above uses short-flag-first.
    return click.option(
        "--source",
        "-s",
        "source_ids",
        multiple=True,
        help="Limit to specific source IDs",
        shell_complete=_complete_sources,
    )(f)


def language_option(f: FC) -> FC:
    """Add the shared ``--language`` flag used by generation commands.

    Resolution chain (handled by the command body, not the decorator):
    ``--language`` flag > ``NOTEBOOKLM_HL`` env var > config file > ``"en"``
    default. The decorator only declares the flag; the body must call
    ``resolve_language(language)`` to walk the chain.
    """
    return click.option(
        "--language",
        default=None,
        help="Output language (default: --language > NOTEBOOKLM_HL env > config > 'en')",
    )(f)


def artifact_option(f: FC) -> FC:
    """Add --artifact/-a option for artifact ID.

    Supports partial ID matching (e.g., 'abc' matches 'abc123...').

    Tab completion: when shell completion is activated, ``-a
    <TAB>`` lists artifact IDs from the resolved active notebook. See
    ``source_option`` for the resolution rules.
    """
    return click.option(
        "-a",
        "--artifact",
        "artifact_id",
        required=True,
        help="Artifact ID. Supports partial IDs.",
        shell_complete=_complete_artifacts,
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


class _PromptFilePath(click.ParamType):
    """``--prompt-file`` value: a regular file OR the literal ``-`` (stdin).

    Replaces ``click.Path(exists=True, dir_okay=False)`` so the Unix ``-``
    convention works. For real paths we still want
    ``click.Path``'s existence + dir-check guarantees so a typo surfaces at
    parse time instead of inside the command body. ``-`` is passed through
    untouched and the downstream ``resolve_prompt`` helper interprets it as
    "read stdin".
    """

    name = "prompt_file"

    def convert(self, value, param, ctx):
        if value == "-":
            return value
        # Delegate to the standard ``click.Path`` validator for non-stdin
        # paths so behavior on real files is unchanged.
        return click.Path(exists=True, dir_okay=False).convert(value, param, ctx)


def prompt_file_option(f: FC) -> FC:
    """Add --prompt-file option for reading prompt/query text from a file.

    Accepts a path to a regular file OR the literal ``-`` to read from
    stdin.
    """
    return click.option(
        "--prompt-file",
        "prompt_file",
        type=_PromptFilePath(),
        default=None,
        help=(
            "Read prompt/query text from a file (or '-' for stdin) "
            "instead of the positional argument"
        ),
    )(f)


def retry_option(f: FC) -> FC:
    """Add --retry option for rate limit retry with exponential backoff."""
    return click.option(
        "--retry",
        "max_retries",
        # ``IntRange(min=0)`` rejects negatives at parse time; 0 stays valid
        # so the default "no retries" behavior is unchanged.
        type=click.IntRange(min=0),
        default=0,
        help="Retry N times with exponential backoff on rate limit",
    )(f)


def list_options(f: FC) -> FC:
    """Add ``--limit`` and ``--no-truncate`` flags shared by every ``list``-style command.

    Used by the top-level ``notebooklm list``, ``notebooklm source list``,
    and ``notebooklm artifact list`` so the output-shaping flag surface
    stays uniform across list-style commands as notebooks grow large
    enough that the default rendering becomes unreadable or unparseable.
    The wrapped function gains two kwargs:

    - ``limit`` (``int | None``) — when non-``None``, the command must slice
      its result set to the first ``limit`` rows BEFORE rendering (and before
      counting in the JSON envelope). Default ``None`` means "show every
      row" so the existing behavior is preserved exactly when neither flag
      is passed; callers do offset-based slicing client-side (no server-side
      cursors in scope for this phase).
    - ``no_truncate`` (``bool``) — when ``True``, the command must NOT impose
      ``max_width`` constraints on free-form columns (titles, IDs, etc.) so
      long values render in full. JSON output is structurally unaffected by
      this flag (JSON never truncates).

    The companion ``--no-truncate`` flag on ``notebooklm chat history`` is
    NOT bundled here — that command does not gain ``--limit`` (it already has
    ``-l/--limit`` with different semantics: a server-side cap on the number
    of Q/A turns to fetch), so it wires ``--no-truncate`` directly. Bundling
    a divergent two-flag set would push us toward a misleading shared name.
    """
    f = click.option(
        "--no-truncate",
        "no_truncate",
        is_flag=True,
        default=False,
        help="Disable column truncation in the rendered table (default: truncate).",
    )(f)
    f = click.option(
        "--limit",
        "limit",
        # ``IntRange(min=0)`` rejects negatives. 0 is intentionally valid and
        # means "show no rows" (consistent with the underlying ``rows[:0]``
        # slice in notebook/source/artifact list commands). Users who want
        # "no limit" omit the flag entirely — that's why the default is
        # ``None``, not 0.
        type=click.IntRange(min=0),
        default=None,
        help=(
            "Show at most N rows. 0 = show no rows. Omit for unlimited. "
            "Applies to both text and --json output."
        ),
    )(f)
    return f


# Composite decorators for common patterns


def standard_options(f: FC) -> FC:
    """Apply notebook + json options (most common pattern)."""
    return notebook_option(json_option(f))


def generate_options(f: FC) -> FC:
    """Apply notebook + json + wait + retry options for generation commands."""
    return notebook_option(json_option(wait_option(retry_option(f))))
