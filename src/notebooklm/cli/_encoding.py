"""Encoding-safe CLI output helpers."""

from __future__ import annotations

import sys

import click


def replace_unencodable(text: str, stream: object | None) -> str:
    """Replace characters that cannot be encoded by a text stream."""
    encoding = (getattr(stream, "encoding", "utf-8") or "utf-8") if stream else "utf-8"
    try:
        return text.encode(encoding, errors="replace").decode(encoding, errors="replace")
    except LookupError:
        return text.encode("ascii", errors="replace").decode("ascii")


def safe_echo(message: str, *, err: bool = False) -> None:
    """Echo text, replacing unencodable characters if the first write fails."""
    try:
        click.echo(message, err=err)
    except UnicodeEncodeError:
        stream = sys.stderr if err else sys.stdout
        click.echo(replace_unencodable(message, stream), err=err)
