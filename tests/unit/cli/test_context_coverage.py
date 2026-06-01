"""Coverage gap tests for ``notebooklm.cli.context``.

Targets the error-handling branches of the context-file read/write/clear
helpers: corrupted-JSON and OS-error warnings, lock contention and
unavailability during ``clear``, and the locked-clear fast paths
(missing file race, corrupt JSON, non-dict payload, no-op when unchanged).

All tests pass an explicit ``context_path_fn`` so nothing touches the real
``~/.notebooklm/context.json``.
"""

from __future__ import annotations

import json
from pathlib import Path

from filelock import Timeout

from notebooklm.cli import context as ctx


def _path_fn(target: Path):
    """Build a ``context_path_fn`` that always returns ``target``."""

    def _fn(*, storage_path=None):
        return target

    return _fn


# ---------------------------------------------------------------------------
# _get_context_value
# ---------------------------------------------------------------------------


class TestGetContextValue:
    def test_invalid_shape_warns_and_returns_none(self, tmp_path, caplog):
        target = tmp_path / "context.json"
        target.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        with caplog.at_level("WARNING"):
            result = ctx._get_context_value("notebook_id", context_path_fn=_path_fn(target))
        assert result is None
        assert "invalid shape" in caplog.text

    def test_corrupt_json_warns_and_returns_none(self, tmp_path, caplog):
        target = tmp_path / "context.json"
        target.write_text("{not json", encoding="utf-8")
        with caplog.at_level("WARNING"):
            result = ctx._get_context_value("notebook_id", context_path_fn=_path_fn(target))
        assert result is None
        assert "corrupted" in caplog.text

    def test_os_error_warns_and_returns_none(self, tmp_path, caplog, monkeypatch):
        target = tmp_path / "context.json"
        target.write_text(json.dumps({"notebook_id": "nb_1"}), encoding="utf-8")

        def _boom(*a, **kw):
            raise OSError("read failed")

        monkeypatch.setattr(Path, "read_text", _boom)
        with caplog.at_level("WARNING"):
            result = ctx._get_context_value("notebook_id", context_path_fn=_path_fn(target))
        assert result is None
        assert "Cannot read context file" in caplog.text


# ---------------------------------------------------------------------------
# _set_context_value
# ---------------------------------------------------------------------------


class TestSetContextValue:
    def test_corrupt_json_warns(self, tmp_path, caplog, monkeypatch):
        target = tmp_path / "context.json"
        target.write_text(json.dumps({"notebook_id": "nb_1"}), encoding="utf-8")

        def _boom(*a, **kw):
            raise json.JSONDecodeError("bad", "doc", 0)

        monkeypatch.setattr(ctx, "atomic_update_json", _boom)
        with caplog.at_level("WARNING"):
            ctx._set_context_value("conversation_id", "c1", context_path_fn=_path_fn(target))
        assert "corrupted" in caplog.text

    def test_os_error_warns(self, tmp_path, caplog, monkeypatch):
        target = tmp_path / "context.json"
        target.write_text(json.dumps({"notebook_id": "nb_1"}), encoding="utf-8")

        def _boom(*a, **kw):
            raise OSError("disk full")

        monkeypatch.setattr(ctx, "atomic_update_json", _boom)
        with caplog.at_level("WARNING"):
            ctx._set_context_value("conversation_id", "c1", context_path_fn=_path_fn(target))
        assert "Failed to write context file" in caplog.text


# ---------------------------------------------------------------------------
# _clear_context_file (lock-level outcomes)
# ---------------------------------------------------------------------------


class TestClearContextFileLockOutcomes:
    def test_lock_timeout_returns_contended(self, tmp_path, caplog, monkeypatch):
        target = tmp_path / "context.json"
        target.write_text(json.dumps({"notebook_id": "nb_1"}), encoding="utf-8")

        class _FakeLock:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                raise Timeout("lock-path")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(ctx, "FileLock", _FakeLock)
        with caplog.at_level("WARNING"):
            status = ctx._clear_context_file(target, clear_account=False)
        assert status == "contended"
        assert "contended" in caplog.text

    def test_lock_os_error_returns_unavailable(self, tmp_path, caplog, monkeypatch):
        target = tmp_path / "context.json"
        target.write_text(json.dumps({"notebook_id": "nb_1"}), encoding="utf-8")

        class _FakeLock:
            def __init__(self, *a, **kw):
                pass

            def __enter__(self):
                raise OSError("lock unavailable")

            def __exit__(self, *a):
                return False

        monkeypatch.setattr(ctx, "FileLock", _FakeLock)
        with caplog.at_level("WARNING"):
            status = ctx._clear_context_file(target, clear_account=False)
        assert status == "unavailable"
        assert "unavailable" in caplog.text


# ---------------------------------------------------------------------------
# _clear_context_file_locked
# ---------------------------------------------------------------------------


class TestClearContextFileLocked:
    def test_missing_file_race_returns_unchanged(self, tmp_path):
        target = tmp_path / "context.json"  # never created
        assert ctx._clear_context_file_locked(target, clear_account=False) == "unchanged"

    def test_corrupt_json_unlinks_and_clears(self, tmp_path):
        target = tmp_path / "context.json"
        target.write_text("{not json", encoding="utf-8")
        assert ctx._clear_context_file_locked(target, clear_account=False) == "cleared"
        assert not target.exists()

    def test_non_dict_payload_unlinks_and_clears(self, tmp_path):
        target = tmp_path / "context.json"
        target.write_text(json.dumps(["list", "payload"]), encoding="utf-8")
        assert ctx._clear_context_file_locked(target, clear_account=False) == "cleared"
        assert not target.exists()

    def test_account_preserved_writes_and_clears(self, tmp_path):
        target = tmp_path / "context.json"
        target.write_text(
            json.dumps({"account": {"email": "a@b.c"}, "notebook_id": "nb_1"}),
            encoding="utf-8",
        )
        assert ctx._clear_context_file_locked(target, clear_account=False) == "cleared"
        data = json.loads(target.read_text(encoding="utf-8"))
        assert data == {"account": {"email": "a@b.c"}}

    def test_only_account_field_is_noop_unchanged(self, tmp_path):
        # data == original after clearing non-account fields → "unchanged".
        target = tmp_path / "context.json"
        original = {"account": {"email": "a@b.c"}}
        target.write_text(json.dumps(original), encoding="utf-8")
        assert ctx._clear_context_file_locked(target, clear_account=False) == "unchanged"
        assert json.loads(target.read_text(encoding="utf-8")) == original

    def test_os_error_during_clear_returns_unavailable(self, tmp_path, caplog, monkeypatch):
        target = tmp_path / "context.json"
        target.write_text(json.dumps({"notebook_id": "nb_1"}), encoding="utf-8")

        real_read_text = Path.read_text

        def _boom(self, *a, **kw):
            if self == target:
                raise OSError("read failed mid-clear")
            return real_read_text(self, *a, **kw)

        monkeypatch.setattr(Path, "read_text", _boom)
        with caplog.at_level("WARNING"):
            status = ctx._clear_context_file_locked(target, clear_account=False)
        assert status == "unavailable"
        assert "unavailable" in caplog.text


# ---------------------------------------------------------------------------
# clear_context public wrapper (sanity over "cleared" mapping)
# ---------------------------------------------------------------------------


class TestClearContextWrapper:
    def test_returns_true_when_cleared(self, tmp_path):
        target = tmp_path / "context.json"
        target.write_text(json.dumps({"notebook_id": "nb_1"}), encoding="utf-8")
        assert ctx.clear_context(clear_account=True, context_path_fn=_path_fn(target)) is True
        assert not target.exists()

    def test_returns_false_when_missing(self, tmp_path):
        target = tmp_path / "context.json"
        assert ctx.clear_context(context_path_fn=_path_fn(target)) is False
