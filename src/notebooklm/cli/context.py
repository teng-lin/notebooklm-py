"""CLI context persistence helpers."""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

import click
from filelock import FileLock, Timeout

from ..io import atomic_update_json, atomic_write_json
from ..paths import get_context_path

logger = logging.getLogger(__name__)
ContextPathFn = Callable[..., Path]
ContextClearStatus = Literal["cleared", "unchanged", "contended", "unavailable"]


def _describe_json_shape(value: Any) -> str:
    """Return a compact diagnostic description for unexpected JSON payloads."""
    return f"{type(value).__name__} {value!r}"


def _current_storage_override() -> Path | None:
    """Resolve the active ``--storage`` override from the current Click context."""
    ctx = click.get_current_context(silent=True)
    if ctx is None or not ctx.obj:
        return None
    storage = ctx.obj.get("storage_path")
    if storage is None:
        return None
    return Path(storage).expanduser().resolve()


def _resolve_context_path(context_path_fn: ContextPathFn | None = None) -> Path:
    context_path_fn = context_path_fn or get_context_path
    return context_path_fn(storage_path=_current_storage_override())


def _get_context_value(key: str, *, context_path_fn: ContextPathFn | None = None) -> str | None:
    """Read a single value from context.json."""
    context_file = _resolve_context_path(context_path_fn)
    if not context_file.exists():
        return None
    try:
        data = json.loads(context_file.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            logger.warning(
                "Context file %s has invalid shape; expected JSON object, got %s.",
                context_file,
                _describe_json_shape(data),
            )
            return None
        return data.get(key)
    except json.JSONDecodeError:
        logger.warning(
            "Context file %s is corrupted; cannot read '%s'. Run 'notebooklm clear' to reset.",
            context_file,
            key,
        )
        return None
    except OSError as e:
        logger.warning("Cannot read context file %s: %s", context_file, e)
        return None


def _set_context_value(
    key: str, value: str | None, *, context_path_fn: ContextPathFn | None = None
) -> None:
    """Set or clear a single value in context.json."""
    context_file = _resolve_context_path(context_path_fn)
    if not context_file.exists():
        # Conversation updates are context-only: callers must select a notebook
        # first, which creates the file and account metadata to preserve.
        return

    def _mutate(existing: Any) -> dict[str, Any]:
        data = dict(existing) if isinstance(existing, dict) else {}
        if value is not None:
            data[key] = value
        elif key in data:
            del data[key]
        return data

    try:
        atomic_update_json(context_file, _mutate)
    except json.JSONDecodeError:
        logger.warning(
            "Context file %s is corrupted; cannot update '%s'. Run 'notebooklm clear' to reset.",
            context_file,
            key,
        )
    except OSError as e:
        logger.warning("Failed to write context file %s for key '%s': %s", context_file, key, e)


def get_current_notebook(*, context_path_fn: ContextPathFn | None = None) -> str | None:
    """Get the current notebook ID from context."""
    return _get_context_value("notebook_id", context_path_fn=context_path_fn)


def set_current_notebook(
    notebook_id: str,
    title: str | None = None,
    is_owner: bool | None = None,
    created_at: str | None = None,
    *,
    context_path_fn: ContextPathFn | None = None,
) -> None:
    """Set the current notebook context."""
    context_file = _resolve_context_path(context_path_fn)

    def _mutate(existing: Any) -> dict[str, Any]:
        existing_dict = existing if isinstance(existing, dict) else {}
        data: dict[str, Any] = {}
        if isinstance(existing_dict.get("account"), dict):
            data["account"] = existing_dict["account"]
        data["notebook_id"] = notebook_id
        if title:
            data["title"] = title
        if is_owner is not None:
            data["is_owner"] = is_owner
        if created_at:
            data["created_at"] = created_at
        return data

    atomic_update_json(context_file, _mutate, recover_from_corrupt=True)


def clear_context(
    *, clear_account: bool = False, context_path_fn: ContextPathFn | None = None
) -> bool:
    """Clear the current context."""
    context_file = _resolve_context_path(context_path_fn)
    return _clear_context_file(context_file, clear_account=clear_account) == "cleared"


def _clear_context_file(context_file: Path, *, clear_account: bool) -> ContextClearStatus:
    """Clear context file data and report the precise lock/storage outcome."""
    if not context_file.exists():
        return "unchanged"
    lock_path = context_file.with_suffix(context_file.suffix + ".lock")
    context_file.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = FileLock(str(lock_path), timeout=10.0)
        with lock:
            return _clear_context_file_locked(context_file, clear_account=clear_account)
    except Timeout as e:
        logger.warning("Context file %s lock is contended; clear skipped: %s", context_file, e)
        return "contended"
    except OSError as e:
        logger.warning("Context file %s is unavailable; clear skipped: %s", context_file, e)
        return "unavailable"


def _clear_context_file_locked(context_file: Path, *, clear_account: bool) -> ContextClearStatus:
    """Clear context file data while the file lock is held."""
    try:
        if not context_file.exists():
            return "unchanged"
        if clear_account:
            context_file.unlink(missing_ok=True)
            return "cleared"
        try:
            data = json.loads(context_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            context_file.unlink(missing_ok=True)
            return "cleared"
        if not isinstance(data, dict):
            context_file.unlink(missing_ok=True)
            return "cleared"
        original = dict(data)
        account = original.get("account")
        # ``clear`` intentionally removes every non-account field so future
        # notebook/conversation context keys do not need explicit pop entries.
        data.clear()
        if "account" in original:
            data["account"] = account
        if not data:
            context_file.unlink(missing_ok=True)
            return "cleared"
        if data != original:
            atomic_write_json(context_file, data)
            return "cleared"
        return "unchanged"
    except OSError as e:
        logger.warning("Context file %s is unavailable; clear skipped: %s", context_file, e)
        return "unavailable"


def get_current_conversation(*, context_path_fn: ContextPathFn | None = None) -> str | None:
    """Get the current conversation ID from context."""
    return _get_context_value("conversation_id", context_path_fn=context_path_fn)


def set_current_conversation(
    conversation_id: str | None, *, context_path_fn: ContextPathFn | None = None
) -> None:
    """Set or clear the current conversation ID in context."""
    _set_context_value("conversation_id", conversation_id, context_path_fn=context_path_fn)
