"""CLI prompt and stdin input helpers."""

from __future__ import annotations

from pathlib import Path

import click


def read_stdin_text(*, source_label: str = "stdin") -> str:
    """Read all of stdin as UTF-8 text and strip surrounding whitespace.

    Centralizes the Unix ``-`` stdin convention used by ``ask``, ``note
    create``, ``source add``, and ``--prompt-file -``. Uses
    ``click.get_text_stream("stdin").read()`` so ``CliRunner.invoke(input=...)``
    in tests is honored without monkey-patching ``sys.stdin``.

    Args:
        source_label: Label used in error messages, e.g. ``"prompt file"``, so
            the failure mode identifies which input was empty or invalid.

    Returns:
        UTF-8 stdin text with surrounding whitespace removed.

    Raises:
        click.ClickException: If stdin yields a non-UTF-8 byte sequence.
    """
    try:
        text = click.get_text_stream("stdin").read()
    except UnicodeDecodeError as e:
        raise click.ClickException(f"{source_label} (stdin) is not valid UTF-8: {e}") from e
    return text.strip()


def resolve_prompt(
    argument_value: str | None,
    prompt_file: str | None,
    param_name: str = "prompt",
    *,
    required: bool = False,
) -> str:
    """Resolve prompt text from a positional argument or ``--prompt-file``.

    Exactly one source may be provided. The file/stdin path is read as UTF-8
    with surrounding whitespace stripped. Positional argument values are
    preserved verbatim for backward compatibility. When ``required`` is true and
    neither source yields text, a ``UsageError`` is raised; otherwise an empty
    string is returned.

    The literal ``-`` is recognized as "read stdin" for either source, matching
    the Unix convention.

    Args:
        argument_value: Value of the positional CLI argument, if any.
        prompt_file: Path passed via ``--prompt-file``, or ``None``.
        param_name: Name of the positional argument, used in error messages.
        required: When true, raise ``UsageError`` if both sources are empty.

    Returns:
        Resolved prompt text, or an empty string when no source is provided and
        ``required`` is false. File/stdin sources are whitespace-stripped;
        positional arguments are preserved verbatim.

    Raises:
        click.UsageError: Both sources are provided, or ``required`` is true and
            both are empty.
        click.ClickException: Prompt file is unreadable or not valid UTF-8.
    """
    if argument_value and prompt_file:
        raise click.UsageError(
            f"Cannot use both the {param_name} argument and --prompt-file. Choose one."
        )

    if prompt_file == "-" or argument_value == "-":
        label = "prompt file" if prompt_file == "-" else param_name
        text = read_stdin_text(source_label=label)
    elif prompt_file:
        path = Path(prompt_file)
        if not path.is_file():
            raise click.ClickException(f"Prompt file '{prompt_file}' is not a regular file.")
        try:
            text = path.read_text(encoding="utf-8").strip()
        except OSError as e:
            raise click.ClickException(f"Failed to read prompt file '{prompt_file}': {e}") from e
        except UnicodeDecodeError as e:
            raise click.ClickException(
                f"Prompt file '{prompt_file}' is not valid UTF-8: {e}"
            ) from e
    else:
        text = argument_value or ""

    if required and not text:
        raise click.UsageError(f"Provide a {param_name} argument or --prompt-file.")
    return text
