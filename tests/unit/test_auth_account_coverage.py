"""Coverage-focused tests for ``notebooklm._auth.account`` branches.

Targets the read/clear/migration helpers and error-handling branches that the
concern-aligned ``test_auth_account.py`` suite does not exercise: malformed /
non-dict storage payloads, the ``_probe_authuser`` non-200 path, legacy
``context.json`` migration cleanup, the corrupt-storage ``RuntimeError`` guard,
and the in-band clear helper's no-op / lock branches.

New file per ADR-0007: patches owning modules at the bare-name call site rather
than editing the existing concern-aligned test file.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from notebooklm._auth import account as _auth_account
from notebooklm._auth.account import (
    Account,
    _clear_in_band_account,
    _drop_legacy_account_key,
    _load_storage_state_for_write,
    _probe_authuser,
    _read_in_band_account,
    _read_legacy_account,
    clear_account_metadata,
    enumerate_accounts,
    format_authuser_value,
    get_account_email_for_storage,
    read_account_metadata_from_storage_state,
    write_account_metadata,
)


class TestProbeAuthuserNon200:
    """``_probe_authuser`` returns ``None`` for non-200 responses (line 131)."""

    @pytest.mark.asyncio
    async def test_non_200_returns_none(self):
        async def fake_get(*args, **kwargs):
            request = httpx.Request("GET", "https://notebooklm.google.com/?authuser=0")
            return httpx.Response(503, content=b"down", request=request)

        class _Client:
            get = staticmethod(fake_get)

        result = await _probe_authuser(_Client(), 0)  # type: ignore[arg-type]
        assert result is None


class TestEnumerateAccountsPokeHook:
    """``enumerate_accounts`` runs the optional poke hook before probing (line 184)."""

    @pytest.mark.asyncio
    async def test_poke_session_hook_invoked(self, monkeypatch):
        poked: list[object] = []

        async def fake_poke(client, storage_path):
            poked.append((client, storage_path))

        async def fake_probe(client, n):
            return "alice@example.com" if n == 0 else "alice@example.com"

        monkeypatch.setattr(_auth_account, "_probe_authuser", fake_probe)

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=2, poke_session=fake_poke)

        assert poked, "poke_session hook was not invoked"
        assert accounts == [Account(authuser=0, email="alice@example.com", is_default=True)]

    @pytest.mark.asyncio
    async def test_poke_session_none_skips_hook(self, monkeypatch):
        # poke_session defaults to None on the bare ``_auth.account`` entry
        # point → the hook is skipped (183->185 false arc).
        async def fake_probe(client, n):
            return "alice@example.com"

        monkeypatch.setattr(_auth_account, "_probe_authuser", fake_probe)

        jar = httpx.Cookies()
        jar.set("SID", "x", domain=".google.com")
        accounts = await enumerate_accounts(jar, max_authuser=2)

        assert accounts == [Account(authuser=0, email="alice@example.com", is_default=True)]


class TestReadInBandAccount:
    """In-band reader malformed / non-dict branches (lines 232-234, 241)."""

    def test_missing_file_returns_empty(self, tmp_path):
        assert _read_in_band_account(tmp_path / "missing.json") == {}

    def test_malformed_json_returns_empty(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("not json", encoding="utf-8")
        assert _read_in_band_account(storage) == {}

    def test_non_dict_storage_state_returns_empty(self):
        # read_account_metadata_from_storage_state guard: non-dict → {} (line 241).
        assert read_account_metadata_from_storage_state(["not", "a", "dict"]) == {}

    def test_namespace_not_dict_returns_empty(self):
        assert read_account_metadata_from_storage_state({"notebooklm": "oops"}) == {}

    def test_account_not_dict_returns_empty(self):
        assert (
            read_account_metadata_from_storage_state(
                {"notebooklm": {"account": "oops", "version": 1}}
            )
            == {}
        )

    def test_well_formed_account_returned(self):
        assert read_account_metadata_from_storage_state(
            {"notebooklm": {"version": 1, "account": {"authuser": 3}}}
        ) == {"authuser": 3}


class TestReadLegacyAccount:
    """Legacy ``context.json`` reader non-dict branch (line 260)."""

    def test_missing_context_returns_empty(self, tmp_path):
        assert _read_legacy_account(tmp_path / "storage_state.json") == {}

    def test_malformed_legacy_json_returns_empty(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text("not json", encoding="utf-8")
        assert _read_legacy_account(storage) == {}

    def test_non_dict_legacy_payload_returns_empty(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(json.dumps(["x"]), encoding="utf-8")
        assert _read_legacy_account(storage) == {}

    def test_legacy_account_returned(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"account": {"authuser": 4}}), encoding="utf-8"
        )
        assert _read_legacy_account(storage) == {"authuser": 4}


class TestDropLegacyAccountKey:
    """``_drop_legacy_account_key`` migration branches (lines 353, 366, 368)."""

    def test_no_context_file_is_noop(self, tmp_path):
        # context.json missing → early return (line ~348-349).
        _drop_legacy_account_key(tmp_path / "storage_state.json")

    def test_malformed_context_json_skipped(self, tmp_path):
        # Read under lock raises → debug log + return (line ~357-359).
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text("not json", encoding="utf-8")
        _drop_legacy_account_key(storage)  # no raise
        assert (tmp_path / "context.json").read_text(encoding="utf-8") == "not json"

    def test_non_dict_or_missing_account_key_returns(self, tmp_path):
        # data is a dict but no account key → return without write (line 360-361).
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(json.dumps({"notebook_id": "nb"}), encoding="utf-8")
        _drop_legacy_account_key(storage)
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb"
        }

    def test_account_key_removed_but_other_state_preserved(self, tmp_path):
        # account dropped, remaining state rewritten (line 363-364).
        storage = tmp_path / "storage_state.json"
        (tmp_path / "context.json").write_text(
            json.dumps({"notebook_id": "nb", "account": {"authuser": 1}}),
            encoding="utf-8",
        )
        _drop_legacy_account_key(storage)
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb"
        }

    def test_account_only_context_file_unlinked(self, tmp_path):
        # When account was the sole key, the file is removed (line 366).
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")
        _drop_legacy_account_key(storage)
        assert not context.exists()

    def test_oserror_from_lock_is_swallowed(self, tmp_path, monkeypatch):
        # Best-effort migration: an OSError acquiring the lock is swallowed
        # (lines 367-369).
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")

        class _BoomLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                raise OSError("lock unavailable")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(_auth_account, "FileLock", _BoomLock)
        _drop_legacy_account_key(storage)  # swallows OSError, no raise
        # Untouched because the lock failed before any read/write.
        assert json.loads(context.read_text(encoding="utf-8")) == {"account": {"authuser": 1}}


class TestLoadStorageStateForWrite:
    """``_load_storage_state_for_write`` synthetic + corruption guards
    (lines 430-433)."""

    def test_missing_file_returns_synthetic_document(self, tmp_path):
        result = _load_storage_state_for_write(tmp_path / "missing.json")
        assert result == {"cookies": [], "origins": []}

    def test_corrupt_json_raises_runtime_error(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("{not valid json", encoding="utf-8")
        with pytest.raises(RuntimeError, match="is corrupted"):
            _load_storage_state_for_write(storage)

    def test_non_dict_shape_raises_runtime_error(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
        with pytest.raises(RuntimeError, match="unexpected shape"):
            _load_storage_state_for_write(storage)

    def test_valid_dict_returned(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")
        assert _load_storage_state_for_write(storage) == {"cookies": []}


class TestClearInBandAccount:
    """``_clear_in_band_account`` no-op / branch coverage
    (lines 447, 469-471, 473, 481, 483-484)."""

    def test_missing_file_is_noop(self, tmp_path):
        _clear_in_band_account(tmp_path / "missing.json")  # early return

    def test_malformed_json_skipped(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text("not json", encoding="utf-8")
        _clear_in_band_account(storage)  # debug-log + return, no raise
        assert storage.read_text(encoding="utf-8") == "not json"

    def test_non_dict_payload_returns(self, tmp_path):
        # data loads but is not a dict → return (line 481).
        storage = tmp_path / "storage_state.json"
        storage.write_text(json.dumps(["x"]), encoding="utf-8")
        _clear_in_band_account(storage)
        assert json.loads(storage.read_text(encoding="utf-8")) == ["x"]

    def test_namespace_missing_account_key_returns(self, tmp_path):
        # namespace present but no account key → return (line 483-484).
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps({"cookies": [], "notebooklm": {"version": 1}}), encoding="utf-8"
        )
        _clear_in_band_account(storage)
        # Untouched.
        assert json.loads(storage.read_text(encoding="utf-8"))["notebooklm"] == {"version": 1}

    def test_account_cleared_drops_version_only_namespace(self, tmp_path):
        # account removed; remaining namespace is {version} → namespace dropped.
        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=2, email="bob@example.com")
        assert "notebooklm" in json.loads(storage.read_text(encoding="utf-8"))
        _clear_in_band_account(storage)
        data = json.loads(storage.read_text(encoding="utf-8"))
        assert "notebooklm" not in data

    def test_account_cleared_keeps_namespace_with_extra_keys(self, tmp_path):
        # namespace carries an extra (non-version) key → namespace retained.
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "notebooklm": {
                        "version": 1,
                        "account": {"authuser": 1},
                        "extra": "keep-me",
                    },
                }
            ),
            encoding="utf-8",
        )
        _clear_in_band_account(storage)
        namespace = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]
        assert "account" not in namespace
        assert namespace["extra"] == "keep-me"


class TestClearAccountMetadataFacade:
    """``clear_account_metadata`` covers in-band + legacy paths together."""

    def test_none_storage_path_is_noop(self):
        clear_account_metadata(None)

    def test_clears_both_in_band_and_legacy(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=1, email="alice@example.com")
        (tmp_path / "context.json").write_text(
            json.dumps({"notebook_id": "nb", "account": {"authuser": 1}}),
            encoding="utf-8",
        )

        clear_account_metadata(storage)

        assert "notebooklm" not in json.loads(storage.read_text(encoding="utf-8"))
        assert json.loads((tmp_path / "context.json").read_text(encoding="utf-8")) == {
            "notebook_id": "nb"
        }


class TestAccountEmailAndAuthuserValue:
    """Residual branches in email/authuser-value resolution (lines 317, 329->331)."""

    def test_get_account_email_returns_none_when_absent(self, tmp_path):
        # No metadata at all → falls through to None (line 317).
        assert get_account_email_for_storage(tmp_path / "storage_state.json") is None

    def test_get_account_email_returns_none_for_blank_email(self, tmp_path):
        # Persisted email is whitespace-only → stripped to "" → None (line 317).
        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=1, email="alice@example.com")
        # Overwrite the in-band email with a blank string.
        data = json.loads(storage.read_text(encoding="utf-8"))
        data["notebooklm"]["account"]["email"] = "   "
        storage.write_text(json.dumps(data), encoding="utf-8")
        assert get_account_email_for_storage(storage) is None

    def test_format_authuser_value_blank_email_falls_back_to_index(self):
        # Whitespace-only email is ignored; integer index is used (329->331).
        assert format_authuser_value(2, "   ") == "2"

    def test_format_authuser_value_prefers_real_email(self):
        assert format_authuser_value(0, "bob@example.com") == "bob@example.com"

    def test_format_authuser_value_default_index(self):
        assert format_authuser_value() == "0"


class TestDropLegacyMalformedUnderLock:
    """``_drop_legacy_account_key`` malformed-read-under-lock branch (line 354)."""

    def test_malformed_json_under_lock_is_skipped(self, tmp_path):
        # The file exists with content that survives the first existence check
        # but fails json parsing while holding the lock → debug-log + return.
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text("{not valid json", encoding="utf-8")
        _drop_legacy_account_key(storage)  # no raise
        assert context.read_text(encoding="utf-8") == "{not valid json"


class TestWriteAccountMetadataNamespaceReplace:
    """``write_account_metadata`` namespace handling (lines 410, 410->412)."""

    def test_non_dict_namespace_is_replaced(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps({"cookies": [], "origins": [], "notebooklm": "corrupt"}),
            encoding="utf-8",
        )
        write_account_metadata(storage, authuser=3, email="carol@example.com")
        namespace = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]
        assert namespace["version"] == 1
        assert namespace["account"] == {"authuser": 3, "email": "carol@example.com"}

    def test_existing_dict_namespace_is_updated_in_place(self, tmp_path):
        # File already carries a valid ``notebooklm`` dict namespace → the
        # ``isinstance`` guard is True so the reassignment is skipped (410->412).
        storage = tmp_path / "storage_state.json"
        storage.write_text(
            json.dumps(
                {
                    "cookies": [],
                    "origins": [],
                    "notebooklm": {"version": 1, "extra": "keep", "account": {"authuser": 0}},
                }
            ),
            encoding="utf-8",
        )
        write_account_metadata(storage, authuser=5, email="dave@example.com")
        namespace = json.loads(storage.read_text(encoding="utf-8"))["notebooklm"]
        assert namespace["account"] == {"authuser": 5, "email": "dave@example.com"}
        assert namespace["extra"] == "keep"


class TestDropLegacyTocTouRecheck:
    """Inner existence re-check return under the lock (line 354)."""

    def test_file_vanishes_after_lock_acquired(self, tmp_path, monkeypatch):
        storage = tmp_path / "storage_state.json"
        context = tmp_path / "context.json"
        context.write_text(json.dumps({"account": {"authuser": 1}}), encoding="utf-8")

        original_exists = Path.exists
        calls = {"n": 0}

        def flaky_exists(self):
            # First call (outer guard) sees the file; second call (inner
            # re-check under the lock) reports it gone, exercising the TOCTOU
            # return at line 354.
            if self == context:
                calls["n"] += 1
                if calls["n"] >= 2:
                    return False
            return original_exists(self)

        monkeypatch.setattr(Path, "exists", flaky_exists)
        _drop_legacy_account_key(storage)  # returns without touching the file
        # File is left intact because the inner re-check short-circuited.
        assert json.loads(context.read_text(encoding="utf-8")) == {"account": {"authuser": 1}}


class TestClearInBandLockFailure:
    """Best-effort OSError handling in ``_clear_in_band_account`` (line 491-492)."""

    def test_oserror_from_lock_is_swallowed(self, tmp_path, monkeypatch):
        storage = tmp_path / "storage_state.json"
        write_account_metadata(storage, authuser=1)

        class _BoomLock:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                raise OSError("lock unavailable")

            def __exit__(self, *exc):
                return False

        monkeypatch.setattr(_auth_account, "FileLock", _BoomLock)
        # Should swallow the OSError and not raise.
        _clear_in_band_account(storage)
        # File untouched because the lock failed before any write.
        assert "notebooklm" in json.loads(storage.read_text(encoding="utf-8"))
