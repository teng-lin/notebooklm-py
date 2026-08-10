"""Frozen missing/corrupt-input policy matrix for auth credential persistence.

Faults are injected at pathlib/stdlib seams, not on auditable ``notebooklm._auth`` modules.
Every row pins the concrete result/exception, exact logger event, backup bytes, final bytes,
and atomic-write count before the implementation bodies move.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from http.cookiejar import Cookie
from pathlib import Path

import httpx
import pytest

from notebooklm._auth import master_token, storage
from notebooklm._auth.master_token import MasterTokenError


@pytest.fixture(autouse=True)
def _capture_auth_logs_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG, logger="notebooklm.auth")
    caplog.set_level(logging.DEBUG, logger="notebooklm.auth.master_token")


def _cookie_jar(*, malformed: bool = False) -> httpx.Cookies:
    jar = httpx.Cookies()
    for name in ("SID", "APISID", "SAPISID"):
        jar.set(name, "v", domain=".google.com", path="/")
    if malformed:
        cookie = Cookie(
            version=0,
            name="BAD",
            value="supersecret",
            port=None,
            port_specified=False,
            domain=".google.com",
            domain_specified=True,
            domain_initial_dot=True,
            path="/",
            path_specified=True,
            secure=False,
            expires=None,
            discard=True,
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        )
        jar.jar.set_cookie(cookie)
        cookie.domain = 123  # type: ignore[assignment]  # deliberate malformed captured row
    return jar


def _source_state(*, malformed: bool = False) -> dict[str, object]:
    rows: list[object] = [
        {
            "name": "SID",
            "value": "sid",
            "domain": ".google.com",
            "path": "/",
            "futureCookieField": "preserved",
        },
        {
            "name": "__Secure-1PSIDTS",
            "value": "ts",
            "domain": ".google.com",
            "path": "/",
        },
    ]
    if malformed:
        rows.append("secret-looking-opaque-row")
        rows.append({"name": 1, "value": "supersecret"})
    return {"cookies": rows, "origins": [{"origin": "https://discarded.test"}]}


@pytest.fixture
def write_counter(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []
    real_replace = os.replace

    def recording_replace(source: str | bytes, destination: str | bytes) -> None:
        calls.append((os.fspath(source), os.fspath(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(os, "replace", recording_replace)
    return calls


def _install_read_error(monkeypatch: pytest.MonkeyPatch, target: Path, error: Exception) -> None:
    real_read_text = Path.read_text

    def read_text(path: Path, *args: object, **kwargs: object) -> str:
        if path == target:
            raise error
        return real_read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


def _bytes(path: Path) -> bytes | None:
    return path.read_bytes() if path.exists() else None


def _backup_bytes(path: Path) -> bytes | None:
    backup = path.with_name(path.name + ".bak")
    return backup.read_bytes() if backup.exists() else None


def _pretty_bytes(payload: object) -> bytes:
    return json.dumps(payload, indent=2).encode()


def _assert_logs(
    caplog: pytest.LogCaptureFixture,
    expected: list[tuple[str, int, str]],
) -> None:
    assert [
        (record.name, record.levelno, record.getMessage()) for record in caplog.records
    ] == expected


@dataclass(frozen=True)
class MergeReadCase:
    category: str
    initial: bytes | None
    injected_error: type[Exception] | None
    level: int
    message: str


MERGE_READ_CASES = [
    MergeReadCase(
        "absent",
        None,
        None,
        logging.DEBUG,
        "Skipping cookie sync: Storage file not found at {path}",
    ),
    MergeReadCase(
        "oserror",
        b"{}",
        OSError,
        logging.WARNING,
        "Failed to read storage state for cookie sync: OSError",
    ),
    MergeReadCase(
        "unicode",
        b"\xff",
        None,
        logging.WARNING,
        "Failed to read storage state for cookie sync: UnicodeDecodeError",
    ),
    MergeReadCase(
        "json",
        b"{",
        None,
        logging.WARNING,
        "Failed to read storage state for cookie sync: JSONDecodeError",
    ),
    MergeReadCase(
        "non-object",
        b"[]",
        None,
        logging.WARNING,
        "storage_state at {path} has an invalid 'cookies' key/payload; rotated cookies will not be persisted",
    ),
    MergeReadCase(
        "invalid-cookies",
        b'{"cookies":{}}',
        None,
        logging.WARNING,
        "storage_state at {path} has an invalid 'cookies' key/payload; rotated cookies will not be persisted",
    ),
]


@pytest.mark.parametrize("case", MERGE_READ_CASES, ids=lambda case: case.category)
@pytest.mark.parametrize("original_snapshot", [None, {}], ids=["legacy", "baseline"])
@pytest.mark.parametrize("return_result", [False, True], ids=["bool", "result"])
def test_cookie_merge_read_policy_is_literal_for_both_algorithms_and_projections(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    case: MergeReadCase,
    original_snapshot: storage.CookieSnapshot | None,
    return_result: bool,
) -> None:
    path = tmp_path / "storage_state.json"
    if case.initial is not None:
        path.write_bytes(case.initial)
    if case.injected_error is OSError:
        _install_read_error(monkeypatch, path, OSError("read denied"))
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        actual = storage.merge_cookie_delta(
            _cookie_jar(),
            path,
            original_snapshot=original_snapshot,
            return_result=return_result,
        )
    if return_result:
        assert type(actual) is storage.CookieSaveResult
        assert actual == storage.CookieSaveResult(False)
    else:
        assert actual is False
    assert _bytes(path) == case.initial
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [("notebooklm.auth", case.level, case.message.format(path=path))])


@pytest.mark.parametrize("algorithm", ["legacy", "baseline"])
@pytest.mark.parametrize("return_result", [False, True], ids=["bool", "result"])
def test_cookie_merge_preserves_unrelated_malformed_rows(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    algorithm: str,
    return_result: bool,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            {
                "cookies": [
                    {"name": "SID", "value": "old", "domain": ".google.com", "path": "/"},
                    "opaque-row",
                ]
            }
        ),
        encoding="utf-8",
    )
    opened_jar = httpx.Cookies()
    opened_jar.set("SID", "old", domain=".google.com", path="/")
    original_snapshot = None if algorithm == "legacy" else storage.snapshot_cookie_jar(opened_jar)
    jar = httpx.Cookies()
    jar.set("SID", "new", domain=".google.com", path="/")
    actual = storage.merge_cookie_delta(
        jar,
        path,
        original_snapshot=original_snapshot,
        return_result=return_result,
    )
    if return_result:
        assert type(actual) is storage.CookieSaveResult
        assert actual == storage.CookieSaveResult(True)
    else:
        assert actual is True
    expected = {
        "cookies": [
            {
                "name": "SID",
                "value": "new",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "secure": False,
                "httpOnly": True,
                "sameSite": "None",
            },
            "opaque-row",
        ]
    }
    assert path.read_bytes() == _pretty_bytes(expected)
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(
        caplog,
        [
            (
                "notebooklm.auth",
                logging.DEBUG,
                f"Successfully synced 1 refreshed cookies to {path}",
            )
        ],
    )


@dataclass(frozen=True)
class AccountReadCase:
    category: str
    initial: bytes
    injected_error: type[Exception] | None
    exception: type[Exception]
    message_mode: str
    message: str


ACCOUNT_READ_CASES = [
    AccountReadCase("oserror", b"{}", OSError, OSError, "equal", "read denied"),
    AccountReadCase(
        "unicode",
        b"\xff",
        None,
        UnicodeDecodeError,
        "equal",
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
    ),
    AccountReadCase(
        "json", b"{", None, RuntimeError, "prefix", "storage state at {path} is corrupted:"
    ),
    AccountReadCase(
        "non-object",
        b"[]",
        None,
        RuntimeError,
        "equal",
        "storage state at {path} has unexpected shape: list",
    ),
]


@pytest.mark.parametrize("case", ACCOUNT_READ_CASES, ids=lambda case: case.category)
def test_account_update_corruption_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    case: AccountReadCase,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(case.initial)
    if case.injected_error is OSError:
        _install_read_error(monkeypatch, path, OSError("read denied"))
    with pytest.raises(case.exception) as raised:
        storage.update_account_metadata(path, authuser=2, email="a@example.com")
    expected = case.message.format(path=path)
    if case.message_mode == "equal":
        assert str(raised.value) == expected
    else:
        assert str(raised.value).startswith(expected)
    assert _bytes(path) == case.initial
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [])


def test_account_update_absent_creates_the_literal_empty_document(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    assert storage.update_account_metadata(path, authuser=2, email="a@example.com") is True
    assert (
        path.read_bytes()
        == b"""{
  "cookies": [],
  "origins": [],
  "notebooklm": {
    "version": 1,
    "account": {
      "authuser": 2,
      "email": "a@example.com"
    }
  }
}"""
    )
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(caplog, [])


def test_valid_account_update_preserves_unrelated_document_data(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    original = {
        "cookies": ["opaque", {"name": "SID", "future": "keep"}],
        "origins": [{"future": True}],
        "future": {"keep": True},
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    assert storage.update_account_metadata(path, authuser=3) is True
    expected = {
        **original,
        "notebooklm": {"version": 1, "account": {"authuser": 3}},
    }
    assert path.read_bytes() == _pretty_bytes(expected)
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(caplog, [])


@dataclass(frozen=True)
class ClearReadCase:
    category: str
    initial: bytes | None
    injected_error: type[Exception] | None
    exception: type[Exception] | None
    message: str | None


CLEAR_READ_CASES = [
    ClearReadCase("absent", None, None, None, None),
    ClearReadCase(
        "oserror", b"{}", OSError, None, "in-band account clear skipped at {path}: read denied"
    ),
    ClearReadCase("unicode", b"\xff", None, UnicodeDecodeError, None),
    ClearReadCase(
        "json",
        b"{",
        None,
        None,
        "in-band account clear skipped at {path}: Expecting property name enclosed in double quotes: line 1 column 2 (char 1)",
    ),
    ClearReadCase("non-object", b"[]", None, None, None),
]


@pytest.mark.parametrize("case", CLEAR_READ_CASES, ids=lambda case: case.category)
def test_account_clear_corruption_policy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    case: ClearReadCase,
) -> None:
    path = tmp_path / "storage_state.json"
    if case.initial is not None:
        path.write_bytes(case.initial)
    if case.injected_error is OSError:
        _install_read_error(monkeypatch, path, OSError("read denied"))
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        if case.exception:
            with pytest.raises(case.exception) as raised:
                storage.clear_in_band_account(path)
            assert (
                str(raised.value)
                == "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
            )
        else:
            assert storage.clear_in_band_account(path) is None
    if case.category == "absent":
        assert _bytes(path) == _pretty_bytes(
            {
                "cookies": [],
                "origins": [],
                "notebooklm": {"version": 1, "account_route_cleared": True},
            }
        )
    else:
        assert _bytes(path) == case.initial
    assert _backup_bytes(path) is None
    assert len(write_counter) == (1 if case.category == "absent" else 0)
    expected_logs = (
        [("notebooklm.auth", logging.DEBUG, case.message.format(path=path))] if case.message else []
    )
    _assert_logs(caplog, expected_logs)


def test_valid_account_clear_preserves_cookies_origins_unknown_data_and_namespace_siblings(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, write_counter: list[tuple[str, str]]
) -> None:
    path = tmp_path / "storage_state.json"
    initial = {
        "cookies": [{"name": "SID", "value": "old", "domain": ".google.com", "future": "keep"}],
        "origins": [{"origin": "https://example.test", "localStorage": []}],
        "future": {"keep": True},
        "notebooklm": {
            "version": 1,
            "account": {"authuser": 2, "email": "a@example.com"},
            "another": {"keep": True},
        },
    }
    path.write_text(json.dumps(initial), encoding="utf-8")
    assert storage.clear_in_band_account(path) is None
    expected = {
        **initial,
        "notebooklm": {
            "version": 1,
            "another": {"keep": True},
            "account_route_cleared": True,
        },
    }
    assert path.read_bytes() == _pretty_bytes(expected)
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(caplog, [])


@pytest.mark.parametrize(
    "category,initial", [("absent", None), ("json", b"{"), ("non-object", b"[]")]
)
def test_remint_tolerated_destination_replaces_once_without_namespace_or_backup(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    category: str,
    initial: bytes | None,
) -> None:
    path = tmp_path / "storage_state.json"
    if initial is not None:
        path.write_bytes(initial)
    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        outcome = storage.replace_from_remint(
            path, _source_state(malformed=True), carry_account=True
        )
    assert outcome == storage.WriteOutcome(storage.WriteStatus.OK)
    expected = {"cookies": _source_state()["cookies"], "origins": []}
    assert path.read_bytes() == _pretty_bytes(expected)
    written = json.loads(path.read_text())
    assert written["cookies"][0]["futureCookieField"] == "preserved"
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(
        caplog,
        [
            (
                "notebooklm.auth",
                logging.WARNING,
                "Skipping malformed storage_state cookie entry (not a dict): type=str",
            ),
            (
                "notebooklm.auth",
                logging.WARNING,
                "Skipping storage_state cookie with missing/empty/non-str name (keys=['name', 'value'] types={name: int, value: str})",
            ),
        ],
    )
    assert "secret-looking" not in caplog.text
    assert "supersecret" not in caplog.text


def test_remint_destination_oserror_is_swallowed_replaced_once_and_not_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(b"original")
    _install_read_error(monkeypatch, path, OSError("read denied"))
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert storage.replace_from_remint(
            path, _source_state(), carry_account=True
        ) == storage.WriteOutcome(storage.WriteStatus.OK)
    expected = {"cookies": _source_state()["cookies"], "origins": []}
    assert path.read_bytes() == _pretty_bytes(expected)
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(caplog, [])


def test_remint_destination_unicode_escapes_with_bytes_unchanged(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError) as raised:
        storage.replace_from_remint(path, _source_state(), carry_account=True)
    assert (
        str(raised.value)
        == "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    )
    assert path.read_bytes() == b"\xff"
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [])


@pytest.mark.parametrize("initial", [None, b"\xff arbitrary not-json"], ids=["absent", "invalid"])
def test_login_backup_false_never_reads_destination_and_filters_malformed_source(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    initial: bytes | None,
) -> None:
    path = tmp_path / "storage_state.json"
    if initial is not None:
        path.write_bytes(initial)
    real_open = Path.open
    destination_reads: list[str] = []
    guard_active = True

    def guarded_open(target: Path, mode: str = "r", *args: object, **kwargs: object):
        if guard_active and target == path and ("r" in mode or "+" in mode):
            destination_reads.append(mode)
            raise AssertionError("backup=False must not read the destination")
        return real_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        outcome = storage.replace_from_login(
            path, _source_state(malformed=True), include_domains=None, backup=False
        )
    guard_active = False
    assert destination_reads == []
    assert outcome == storage.LoginWriteOutcome(storage.LoginWriteStatus.OK)
    expected = {"cookies": _source_state()["cookies"], "origins": []}
    assert path.read_bytes() == _pretty_bytes(expected)
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(
        caplog,
        [
            (
                "notebooklm.auth",
                logging.WARNING,
                "Skipping malformed storage_state cookie entry (not a dict): type=str",
            ),
            (
                "notebooklm.auth",
                logging.WARNING,
                "Skipping storage_state cookie with missing/empty/non-str name (keys=['name', 'value'] types={name: int, value: str})",
            ),
        ],
    )
    assert "secret-looking" not in caplog.text
    assert "supersecret" not in caplog.text


def test_login_backup_true_copies_exact_invalid_destination_before_one_write(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    original = b"\xff arbitrary not-json"
    path.write_bytes(original)
    outcome = storage.replace_from_login(path, _source_state(), include_domains=None, backup=True)
    assert outcome == storage.LoginWriteOutcome(
        storage.LoginWriteStatus.OK, backup_path=path.with_name(path.name + ".bak")
    )
    assert _backup_bytes(path) == original
    expected = {"cookies": _source_state()["cookies"], "origins": []}
    assert path.read_bytes() == _pretty_bytes(expected)
    assert len(write_counter) == 1
    _assert_logs(caplog, [])


def test_login_required_drop_and_backup_error_leave_destination_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    import shutil

    path = tmp_path / "storage_state.json"
    path.write_bytes(b"existing")
    outcome = storage.replace_from_login(
        path,
        {"cookies": [{"name": "NID", "value": "x", "domain": ".google.com"}]},
        include_domains=None,
        backup=True,
    )
    assert outcome == storage.LoginWriteOutcome(
        storage.LoginWriteStatus.REQUIRED_COOKIES_DROPPED,
        missing_required=("SID", "__Secure-1PSIDTS"),
        present_names=("NID",),
        backup_path=None,
    )
    assert outcome.status is storage.LoginWriteStatus.REQUIRED_COOKIES_DROPPED
    assert outcome.missing_required == ("SID", "__Secure-1PSIDTS")
    assert outcome.present_names == ("NID",)
    assert outcome.backup_path is None
    assert path.read_bytes() == b"existing"
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(
        caplog,
        [
            (
                "notebooklm.auth",
                logging.DEBUG,
                f"replace_from_login: 2 required cookie(s) dropped by the write-time "
                f"domain policy for {path}; writing nothing",
            )
        ],
    )
    caplog.clear()

    def fail_copy(*_args: object, **_kwargs: object) -> None:
        raise OSError("copy denied")

    monkeypatch.setattr(shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="^copy denied$"):
        storage.replace_from_login(path, _source_state(), include_domains=None, backup=True)
    assert path.read_bytes() == b"existing"
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [])


@pytest.mark.parametrize("initial", [b"{", b"[]"], ids=["json", "non-object"])
def test_minted_session_owner_gate_refuses_corrupt_existing_destination_exactly(
    tmp_path: Path,
    initial: bytes,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(initial)
    with pytest.raises(MasterTokenError) as raised:
        storage.persist_minted_jar(path, _cookie_jar(), email="a@example.com")
    assert str(raised.value) == (
        "This profile has no recorded account owner; refusing to overwrite its session "
        "with a freshly minted one without force=True."
    )
    assert path.read_bytes() == initial
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [])


def test_minted_session_filters_malformed_rows_with_value_free_warning(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        actual = storage.persist_minted_jar(
            path, _cookie_jar(malformed=True), email="a@example.com"
        )
    assert actual is None
    expected_rows = [
        {
            "name": name,
            "value": "v",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
            "sameSite": "None",
        }
        for name in ("SID", "APISID", "SAPISID")
    ]
    expected = {
        "cookies": expected_rows,
        "origins": [],
        "notebooklm": {
            "version": 1,
            "account": {"authuser": 0, "email": "a@example.com"},
        },
    }
    assert path.read_bytes() == _pretty_bytes(expected)
    _assert_logs(
        caplog,
        [
            (
                "notebooklm.auth",
                logging.WARNING,
                "Skipping storage_state cookie with non-str domain (keys=['domain', 'expires', 'httpOnly', 'name', 'path', 'sameSite', 'secure', 'value'] types={domain: int, expires: int, httpOnly: bool, name: str, path: str, sameSite: str, secure: bool, value: str})",
            )
        ],
    )
    assert "supersecret" not in caplog.text
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1


def _owned_storage_state(email: str) -> dict[str, object]:
    return {
        "cookies": [{"name": "OLD", "value": "old", "domain": ".google.com", "path": "/"}],
        "origins": [{"origin": "https://preserved.test"}],
        "future": {"preserved": True},
        "notebooklm": {
            "version": 99,
            "account": {"authuser": 3, "email": email},
            "sibling": {"preserved": True},
        },
    }


def _minted_owned_state(email: str) -> dict[str, object]:
    return {
        "cookies": [
            {
                "name": name,
                "value": "v",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": False,
                "sameSite": "None",
            }
            for name in ("SID", "APISID", "SAPISID")
        ],
        "origins": [{"origin": "https://preserved.test"}],
        "future": {"preserved": True},
        "notebooklm": {
            "version": 1,
            "account": {"authuser": 0, "email": email},
            "sibling": {"preserved": True},
        },
    }


def _unknown_owner_storage_state() -> dict[str, object]:
    state = _owned_storage_state("unused@example.com")
    state["notebooklm"] = {
        "version": 99,
        "sibling": {"preserved": True},
    }
    return state


def _minted_unknown_owner_state(email: str) -> dict[str, object]:
    state = _minted_owned_state(email)
    state["notebooklm"] = {
        "version": 1,
        "sibling": {"preserved": True},
        "account": {"authuser": 0, "email": email},
    }
    return state


def test_minted_session_matching_owner_replaces_once_without_log_or_backup(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(_pretty_bytes(_owned_storage_state("owner@example.com")))
    actual = storage.persist_minted_jar(path, _cookie_jar(), email="OWNER@example.com")
    assert actual is None
    assert path.read_bytes() == _pretty_bytes(_minted_owned_state("OWNER@example.com"))
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(caplog, [])


def test_minted_session_mismatched_owner_refuses_without_write_log_or_backup(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    original = _pretty_bytes(_owned_storage_state("owner@example.com"))
    path.write_bytes(original)
    with pytest.raises(MasterTokenError) as raised:
        storage.persist_minted_jar(
            path,
            _cookie_jar(),
            email="other@example.com",
            refuse_unknown_owner=False,
        )
    assert str(raised.value) == (
        "This profile already belongs to owner@example.com, but the mint is for "
        "other@example.com. Minting here would overwrite owner@example.com's session and "
        "master token. Pass force=True to overwrite this profile intentionally."
    )
    assert path.read_bytes() == original
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [])


def test_minted_session_unknown_owner_proceeds_when_refusal_is_disabled(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(_pretty_bytes(_unknown_owner_storage_state()))
    actual = storage.persist_minted_jar(
        path,
        _cookie_jar(),
        email="new@example.com",
        refuse_unknown_owner=False,
    )
    assert actual is None
    assert path.read_bytes() == _pretty_bytes(_minted_unknown_owner_state("new@example.com"))
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(
        caplog,
        [
            (
                "notebooklm.auth",
                logging.DEBUG,
                "persist_minted_jar: existing storage has no recorded owner; proceeding "
                "with refuse_unknown_owner=False (re-mint from a token already paired "
                "with this storage_path, not a fresh account selection).",
            )
        ],
    )


def test_minted_session_force_overwrites_mismatched_owner_once_with_exact_log(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(_pretty_bytes(_owned_storage_state("owner@example.com")))
    actual = storage.persist_minted_jar(path, _cookie_jar(), email="other@example.com", force=True)
    assert actual is None
    assert path.read_bytes() == _pretty_bytes(_minted_owned_state("other@example.com"))
    assert _backup_bytes(path) is None
    assert len(write_counter) == 1
    _assert_logs(
        caplog,
        [
            (
                "notebooklm.auth",
                logging.DEBUG,
                "persist_minted_jar: force=True bypasses the account-ownership guard.",
            )
        ],
    )


@pytest.mark.parametrize("category", ["oserror", "unicode"])
def test_minted_session_read_failures_escape_with_destination_unchanged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    category: str,
) -> None:
    path = tmp_path / "storage_state.json"
    initial = b"{}" if category == "oserror" else b"\xff"
    path.write_bytes(initial)
    if category == "oserror":
        _install_read_error(monkeypatch, path, OSError("read denied"))
        expected_type: type[Exception] = OSError
        expected_message = "read denied"
    else:
        expected_type = UnicodeDecodeError
        expected_message = "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte"
    with pytest.raises(expected_type) as raised:
        storage.persist_minted_jar(path, _cookie_jar(), email="a@example.com")
    assert str(raised.value) == expected_message
    assert path.read_bytes() == initial
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [])


@dataclass(frozen=True)
class MasterReadCase:
    category: str
    initial: bytes | None
    injected_error: type[Exception] | None
    exception: type[Exception] | None
    message_mode: str | None
    message: str | None
    cause: type[Exception] | None


MASTER_READ_CASES = [
    MasterReadCase("absent", None, None, None, None, None, None),
    MasterReadCase(
        "oserror",
        b"{}",
        OSError,
        MasterTokenError,
        "prefix",
        "Unreadable master_token.json: read denied",
        OSError,
    ),
    MasterReadCase(
        "unicode",
        b"\xff",
        None,
        UnicodeDecodeError,
        "equal",
        "'utf-8' codec can't decode byte 0xff in position 0: invalid start byte",
        None,
    ),
    MasterReadCase(
        "json",
        b"{",
        None,
        MasterTokenError,
        "prefix",
        "Unreadable master_token.json:",
        json.JSONDecodeError,
    ),
    MasterReadCase(
        "non-object",
        b"[]",
        None,
        MasterTokenError,
        "equal",
        "master_token.json is malformed or an unsupported version.",
        None,
    ),
    MasterReadCase(
        "invalid-record",
        b'{"version":1}',
        None,
        MasterTokenError,
        "equal",
        "master_token.json is malformed or an unsupported version.",
        None,
    ),
]


@pytest.mark.parametrize("case", MASTER_READ_CASES, ids=lambda case: case.category)
def test_master_token_read_policy_has_no_write_backup_or_log(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
    case: MasterReadCase,
) -> None:
    path = tmp_path / "master_token.json"
    if case.initial is not None:
        path.write_bytes(case.initial)
    if case.injected_error is OSError:
        _install_read_error(monkeypatch, path, OSError("read denied"))
    if case.exception is None:
        assert master_token.read_master_token(path) is None
    else:
        with pytest.raises(case.exception) as raised:
            master_token.read_master_token(path)
        assert case.message is not None
        if case.message_mode == "equal":
            assert str(raised.value) == case.message
        else:
            assert str(raised.value).startswith(case.message)
        if case.cause:
            assert isinstance(raised.value.__cause__, case.cause)
    assert _bytes(path) == case.initial
    assert _backup_bytes(path) is None
    assert write_counter == []
    _assert_logs(caplog, [])


def test_arbitrary_master_token_path_may_be_named_storage_state_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    write_counter: list[tuple[str, str]],
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_bytes(b"\xff opaque existing credential bytes")
    real_open = Path.open
    destination_reads: list[str] = []
    guard_active = True

    def guarded_open(target: Path, mode: str = "r", *args: object, **kwargs: object):
        if guard_active and target == path and ("r" in mode or "+" in mode):
            destination_reads.append(mode)
            raise AssertionError("master-token replacement must not read the destination")
        return real_open(target, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    result = master_token.write_master_token(
        path, email="a@example.com", master_token="secret", android_id="android"
    )
    guard_active = False
    assert result is None
    assert destination_reads == []
    assert path.read_bytes() == _pretty_bytes(
        {
            "version": 1,
            "email": "a@example.com",
            "android_id": "android",
            "master_token": "secret",
        }
    )
    assert master_token.read_master_token(path) == {
        "version": 1,
        "email": "a@example.com",
        "android_id": "android",
        "master_token": "secret",
    }
    assert _backup_bytes(path) is None
    assert [destination for _, destination in write_counter] == [str(path)]
    _assert_logs(caplog, [])
