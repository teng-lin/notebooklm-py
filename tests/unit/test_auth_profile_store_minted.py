"""Direct tests for the path-owned freshly minted session replacement."""

from __future__ import annotations

import contextlib
import inspect
import json
import logging
import threading
from dataclasses import FrozenInstanceError, fields
from http.cookiejar import Cookie as HttpCookie
from pathlib import Path
from typing import Any

import httpx
import pytest

import notebooklm.auth as public_auth
from notebooklm import _auth
from notebooklm._auth import master_token, profile_store, storage
from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.profile_document import ProfileDocument
from notebooklm._auth.profile_store import (
    MintedSessionWriteRequest,
    ProfileStore,
    _MintedSessionOwnershipRefused,
)
from notebooklm._auth.storage_lock import LockRequest, LockState
from notebooklm.exceptions import LockUnavailableError

UNKNOWN_OWNER_MESSAGE = (
    "This profile has no recorded account owner; refusing to overwrite its session "
    "with a freshly minted one without force=True."
)


def _cookie(
    value: Any = "secret-value",
    *,
    name: Any = "SID",
    domain: Any = ".google.com",
    path: Any = "/",
    expires: Any = -1,
    http_only: Any = True,
    secure: Any = False,
    same_site: Any = "None",
) -> Cookie:
    return Cookie(
        name=name,
        value=value,
        domain=domain,
        path=path,
        expires=expires,
        http_only=http_only,
        secure=secure,
        same_site=same_site,
    )


def _request(
    *,
    email: Any = "owner@example.com",
    cookies: CookieJar | None = None,
    force: Any = False,
    refuse_unknown_owner: Any = True,
) -> MintedSessionWriteRequest:
    return MintedSessionWriteRequest(
        cookies if cookies is not None else CookieJar((_cookie(),)),
        email,
        force,
        refuse_unknown_owner,
    )


def _owned_payload(email: Any = "owner@example.com") -> dict[str, Any]:
    return {
        "cookies": [{"name": "OLD", "value": "old", "domain": ".google.com"}],
        "origins": [],
        "notebooklm": {
            "version": 99,
            "account": {"authuser": 7, "email": email},
        },
    }


def _write(path: Path, payload: Any) -> bytes:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path.read_bytes()


class RecordingLocks:
    def __init__(
        self,
        state: LockState = LockState.HELD,
        *,
        events: list[str] | None = None,
        exit_error: Exception | None = None,
    ) -> None:
        self.state = state
        self.requests: list[LockRequest] = []
        self.events = events
        self.exit_error = exit_error

    @contextlib.contextmanager
    def acquire(self, request: LockRequest):
        self.requests.append(request)
        if self.events is not None:
            self.events.append("acquire")
        try:
            yield self.state
        finally:
            if self.events is not None:
                self.events.append("release")
            if self.exit_error is not None:
                raise self.exit_error


class MutableEmail(str):
    def __new__(cls, value: str, notes: list[str]):
        instance = super().__new__(cls, value)
        instance.notes = notes
        return instance

    def __deepcopy__(self, memo: dict[int, object]):
        existing = memo.get(id(self))
        if existing is not None:
            return existing
        copied = type(self)(str(self), list(self.notes))
        memo[id(self)] = copied
        return copied


def test_request_shape_validation_redaction_and_non_exports_are_exact() -> None:
    assert str(inspect.signature(MintedSessionWriteRequest)) == (
        "(cookies: 'CookieJar', email: 'str | None', force: 'bool' = False, "
        "refuse_unknown_owner: 'bool' = True) -> None"
    )
    assert [item.name for item in fields(MintedSessionWriteRequest)] == [
        "cookies",
        "email",
        "force",
        "refuse_unknown_owner",
    ]
    assert set(MintedSessionWriteRequest.__slots__) == {
        "cookies",
        "email",
        "force",
        "refuse_unknown_owner",
    }
    assert fields(MintedSessionWriteRequest)[0].repr is False

    request = MintedSessionWriteRequest(
        CookieJar((_cookie(value="cookie-secret", name="SECRET_COOKIE"),)),
        "email-secret@example.com",
    )
    with pytest.raises(FrozenInstanceError):
        request.email = "changed@example.com"  # type: ignore[misc]
    rendered = f"{request!r} {request!s}"
    for secret in (
        "cookie-secret",
        "SECRET_COOKIE",
        "email-secret@example.com",
        "owner",
        ".A.json.lock",
    ):
        assert secret not in rendered

    for invalid in (
        {},
        httpx.Cookies(),
        ProfileDocument.empty(),
        lambda: None,
        Path("A.json"),
        object(),
    ):
        with pytest.raises(TypeError, match="^cookies must be a CookieJar$"):
            MintedSessionWriteRequest(invalid, None)  # type: ignore[arg-type]

    assert "MintedSessionWriteRequest" not in storage.__all__
    assert not hasattr(public_auth, "MintedSessionWriteRequest")
    assert not hasattr(_auth, "MintedSessionWriteRequest")


def test_request_uses_one_shared_memo_and_severs_cookie_email_aliases() -> None:
    email = MutableEmail("owner@example.com", ["caller-owned"])
    source_items = [_cookie(value=email)]
    source = CookieJar(source_items)

    request = MintedSessionWriteRequest(source, email)
    copied_cookie = next(iter(request.cookies))

    assert request.cookies is not source
    assert request.email is not email
    assert copied_cookie is not source_items[0]
    assert copied_cookie.value is request.email
    email.notes.append("mutated-after-request")
    source_items.clear()
    assert request.email.notes == ["caller-owned"]  # type: ignore[union-attr]
    assert tuple(request.cookies) == (copied_cookie,)


def test_request_copy_failure_escapes_without_rewriting_the_exception() -> None:
    marker = RuntimeError("email copy failed")

    class FailsCopy:
        def __deepcopy__(self, memo: dict[int, object]) -> object:
            raise marker

    with pytest.raises(RuntimeError) as raised:
        MintedSessionWriteRequest(CookieJar((_cookie(),)), FailsCopy())  # type: ignore[arg-type]
    assert raised.value is marker


def test_store_uses_one_exact_raw_bounded_lock_and_never_orders_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "nested" / "A.json"
    locks = RecordingLocks()
    commits: list[tuple[Path, dict[str, Any]]] = []

    monkeypatch.setattr(
        profile_store,
        "_commit_profile_json",
        lambda target, payload: commits.append((target, payload)),
    )

    assert ProfileStore(path, locks=locks).replace_minted_session(_request()) is None  # type: ignore[arg-type]
    assert path.parent.is_dir()
    assert locks.requests == [
        LockRequest(
            path=path.with_name(".A.json.lock"),
            blocking=False,
            operation="persist_minted_jar",
            deadline_seconds=90.0,
        )
    ]
    assert len(commits) == 1
    assert commits[0][0] is path


@pytest.mark.parametrize("state", [LockState.CONTENDED, LockState.UNAVAILABLE])
def test_store_lock_miss_raises_exactly_before_every_held_operation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    state: LockState,
) -> None:
    path = tmp_path / "custom.json"
    locks = RecordingLocks(state)
    store = ProfileStore(path, locks=locks)  # type: ignore[arg-type]
    monkeypatch.setattr(
        store,
        "read_document",
        lambda: pytest.fail("lock miss must not read"),
    )
    monkeypatch.setattr(
        profile_store,
        "filter_storage_state_cookies_by_domain_policy",
        lambda state: pytest.fail("lock miss must not filter"),
    )
    monkeypatch.setattr(
        profile_store,
        "_commit_profile_json",
        lambda path, payload: pytest.fail("lock miss must not commit"),
    )

    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.auth"),
        pytest.raises(LockUnavailableError) as raised,
    ):
        store.replace_minted_session(_request())
    assert str(raised.value) == (
        f"persist_minted_jar: storage lock unavailable at {tmp_path / '.custom.json.lock'}"
    )
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert caplog.records == []


@pytest.mark.parametrize("raw", ["{", "[]"])
def test_corrupt_existing_destination_is_unknown_and_refused_without_filter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    path = tmp_path / "A.json"
    path.write_text(raw, encoding="utf-8")
    original = path.read_bytes()
    monkeypatch.setattr(
        profile_store,
        "filter_storage_state_cookies_by_domain_policy",
        lambda state: pytest.fail("owner refusal must precede filtering"),
    )

    with pytest.raises(_MintedSessionOwnershipRefused) as raised:
        ProfileStore(path, locks=RecordingLocks()).replace_minted_session(_request())  # type: ignore[arg-type]
    assert str(raised.value) == UNKNOWN_OWNER_MESSAGE
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "namespace",
    [
        None,
        [],
        {},
        {"account": None},
        {"account": {}},
        {"account": {"email": 7}},
        {"account": {"email": ""}},
        {"account": {"email": "   "}},
    ],
)
def test_all_unknown_owner_shapes_share_the_exact_refusal(
    tmp_path: Path,
    namespace: object,
) -> None:
    path = tmp_path / "A.json"
    payload: dict[str, object] = {"cookies": [], "origins": []}
    if namespace is not None:
        payload["notebooklm"] = namespace
    original = _write(path, payload)

    with pytest.raises(_MintedSessionOwnershipRefused) as raised:
        ProfileStore(path, locks=RecordingLocks()).replace_minted_session(_request())  # type: ignore[arg-type]
    assert str(raised.value) == UNKNOWN_OWNER_MESSAGE
    assert path.read_bytes() == original


def test_owner_is_trimmed_and_casefolded_but_requested_email_is_not_trimmed(
    tmp_path: Path,
) -> None:
    path = tmp_path / "A.json"
    _write(path, _owned_payload("  Owner@Example.COM  "))

    store = ProfileStore(path, locks=RecordingLocks())  # type: ignore[arg-type]
    assert store.replace_minted_session(_request(email="owner@example.com")) is None
    assert json.loads(path.read_text(encoding="utf-8"))["notebooklm"]["account"] == {
        "authuser": 0,
        "email": "owner@example.com",
    }

    _write(path, _owned_payload("owner@example.com"))
    with pytest.raises(_MintedSessionOwnershipRefused) as raised:
        store.replace_minted_session(_request(email=" owner@example.com "))
    assert "mint is for  owner@example.com " in str(raised.value)


def test_known_owner_mismatch_none_message_and_alien_casefold_behavior(
    tmp_path: Path,
) -> None:
    path = tmp_path / "A.json"
    original = _write(path, _owned_payload())
    store = ProfileStore(path, locks=RecordingLocks())  # type: ignore[arg-type]

    with pytest.raises(_MintedSessionOwnershipRefused) as raised:
        store.replace_minted_session(_request(email=None, refuse_unknown_owner=False))
    assert str(raised.value) == (
        "This profile already belongs to owner@example.com, but the mint is for "
        "(no account). Minting here would overwrite owner@example.com's session and "
        "master token. Pass force=True to overwrite this profile intentionally."
    )
    assert path.read_bytes() == original

    class TruthyAlien:
        def __bool__(self) -> bool:
            return True

    with pytest.raises(AttributeError):
        store.replace_minted_session(_request(email=TruthyAlien()))
    assert path.read_bytes() == original


def test_unknown_relaxation_and_force_have_exact_logs_and_truthiness(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "A.json"
    _write(path, {"cookies": [], "origins": [], "notebooklm": {"future": "keep"}})
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert (
            ProfileStore(path, locks=RecordingLocks()).replace_minted_session(  # type: ignore[arg-type]
                _request(email="new@example.com", refuse_unknown_owner=0)
            )
            is None
        )
    assert [record.getMessage() for record in caplog.records] == [
        "persist_minted_jar: existing storage has no recorded owner; proceeding with "
        "refuse_unknown_owner=False (re-mint from a token already paired with this "
        "storage_path, not a fresh account selection)."
    ]

    caplog.clear()
    path.write_text("{", encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        assert (
            ProfileStore(path, locks=RecordingLocks()).replace_minted_session(  # type: ignore[arg-type]
                _request(email="forced@example.com", force=1)
            )
            is None
        )
    assert [record.getMessage() for record in caplog.records] == [
        "persist_minted_jar: force=True bypasses the account-ownership guard."
    ]


def test_minted_session_replaces_account_clear_tombstone(tmp_path: Path) -> None:
    path = tmp_path / "A.json"
    _write(
        path,
        {
            "cookies": [],
            "origins": [],
            "notebooklm": {
                "version": 1,
                "account_route_cleared": True,
                "future": "keep",
            },
        },
    )

    ProfileStore(path, locks=RecordingLocks()).replace_minted_session(  # type: ignore[arg-type]
        _request(email="new@example.com", force=True)
    )

    assert json.loads(path.read_text(encoding="utf-8"))["notebooklm"] == {
        "version": 1,
        "future": "keep",
        "account": {"authuser": 0, "email": "new@example.com"},
    }


def test_filter_fidelity_and_destination_preservation(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "A.json"
    _write(
        path,
        {
            "future": {"preserved": True},
            "cookies": [{"name": "OLD", "value": "old", "domain": ".google.com"}],
            "origins": {"odd": "preserved"},
            "notebooklm": {
                "sibling": {"preserved": True},
                "version": 99,
                "account": {"email": "owner@example.com", "authuser": 7},
                "tail": "preserved",
            },
        },
    )
    cookies = CookieJar(
        (
            _cookie("first", expires=7),
            _cookie("other-path", path="/other", secure=True),
            _cookie("bare", domain="google.com", http_only=False),
            _cookie("drop", name="YT", domain=".youtube.com"),
            _cookie("trusted", name="MEDIA", domain="lh3.googleusercontent.com", expires=7.25),
            _cookie("later", expires=8),
            _cookie("malformed-secret", name="BAD", domain=123),
        )
    )
    with caplog.at_level(logging.WARNING, logger="notebooklm.auth"):
        assert (
            ProfileStore(path, locks=RecordingLocks()).replace_minted_session(  # type: ignore[arg-type]
                _request(cookies=cookies)
            )
            is None
        )

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["future"] == {"preserved": True}
    assert payload["origins"] == {"odd": "preserved"}
    assert list(payload["notebooklm"]) == ["sibling", "version", "account", "tail"]
    assert payload["notebooklm"] == {
        "sibling": {"preserved": True},
        "version": 1,
        "account": {"authuser": 0, "email": "owner@example.com"},
        "tail": "preserved",
    }
    assert [(row["name"], row["domain"], row["path"]) for row in payload["cookies"]] == [
        ("SID", ".google.com", "/"),
        ("SID", ".google.com", "/other"),
        ("SID", "google.com", "/"),
        ("MEDIA", "lh3.googleusercontent.com", "/"),
    ]
    assert payload["cookies"][0] == {
        "name": "SID",
        "value": "later",
        "domain": ".google.com",
        "path": "/",
        "expires": 8,
        "httpOnly": True,
        "secure": False,
        "sameSite": "None",
    }
    assert type(payload["cookies"][3]["expires"]) is float
    assert "malformed-secret" not in caplog.text
    assert "Skipping storage_state cookie with non-str domain" in caplog.text


@pytest.mark.parametrize("email", [None, "", 0])
def test_empty_filtered_output_commits_and_email_is_truthy_only(
    tmp_path: Path,
    email: Any,
) -> None:
    path = tmp_path / f"{email!r}.json"
    cookies = CookieJar((_cookie(name="YT", domain=".youtube.com"),))
    assert (
        ProfileStore(path, locks=RecordingLocks()).replace_minted_session(  # type: ignore[arg-type]
            _request(email=email, cookies=cookies)
        )
        is None
    )
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["cookies"] == []
    assert payload["origins"] == []
    assert payload["notebooklm"]["account"] == {"authuser": 0}


@pytest.mark.parametrize("category", ["exists", "read", "unicode", "filter", "commit"])
def test_non_owner_failures_escape_unchanged_in_exact_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    category: str,
) -> None:
    path = tmp_path / "A.json"
    if category != "exists":
        _write(path, _owned_payload())
    events: list[str] = []
    locks = RecordingLocks(events=events)
    store = ProfileStore(path, locks=locks)  # type: ignore[arg-type]
    marker = RuntimeError(f"{category} failure secret")

    real_exists = Path.exists
    real_read = store.read_document
    real_filter = profile_store.filter_storage_state_cookies_by_domain_policy

    def exists(target: Path) -> bool:
        if target == path:
            events.append("exists")
            if category == "exists":
                raise marker
        return real_exists(target)

    def read() -> ProfileDocument:
        events.append("read")
        if category == "read":
            raise marker
        if category == "unicode":
            raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        return real_read()

    def filter_state(state: dict[str, Any]) -> dict[str, Any]:
        events.append("filter")
        if category == "filter":
            raise marker
        return real_filter(state)

    def commit(target: Path, payload: dict[str, Any]) -> None:
        events.append("commit")
        if category == "commit":
            raise marker

    monkeypatch.setattr(Path, "exists", exists)
    monkeypatch.setattr(store, "read_document", read)
    monkeypatch.setattr(
        profile_store, "filter_storage_state_cookies_by_domain_policy", filter_state
    )
    monkeypatch.setattr(profile_store, "_commit_profile_json", commit)

    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth"):
        if category == "unicode":
            with pytest.raises(UnicodeDecodeError) as raised:
                store.replace_minted_session(_request())
        else:
            with pytest.raises(RuntimeError) as raised:
                store.replace_minted_session(_request())
            assert raised.value is marker
    expected = {
        "exists": ["acquire", "exists", "release"],
        "read": ["acquire", "exists", "read", "release"],
        "unicode": ["acquire", "exists", "read", "release"],
        "filter": ["acquire", "exists", "read", "filter", "release"],
        "commit": ["acquire", "exists", "read", "filter", "commit", "release"],
    }
    assert events == expected[category]
    assert caplog.records == []


def test_refusal_is_raised_only_after_clean_release_and_cleanup_failure_wins(
    tmp_path: Path,
) -> None:
    path = tmp_path / "A.json"
    _write(path, _owned_payload("other@example.com"))
    events: list[str] = []
    clean_store = ProfileStore(path, locks=RecordingLocks(events=events))  # type: ignore[arg-type]
    with pytest.raises(_MintedSessionOwnershipRefused) as raised:
        clean_store.replace_minted_session(_request())
    assert events == ["acquire", "release"]
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None

    cleanup = OSError("cleanup failed")
    failing_store = ProfileStore(  # type: ignore[arg-type]
        path,
        locks=RecordingLocks(events=[], exit_error=cleanup),
    )
    with pytest.raises(OSError) as cleanup_raised:
        failing_store.replace_minted_session(_request())
    assert cleanup_raised.value is cleanup
    assert cleanup_raised.value.__cause__ is None
    assert cleanup_raised.value.__context__ is None


def test_latest_owner_is_decided_inside_the_held_transaction(
    tmp_path: Path,
) -> None:
    path = tmp_path / "A.json"
    _write(path, _owned_payload("owner@example.com"))
    acquire_started = threading.Event()
    let_body_run = threading.Event()

    class BarrierLocks:
        requests: list[LockRequest] = []

        @contextlib.contextmanager
        def acquire(self, request: LockRequest):
            self.requests.append(request)
            acquire_started.set()
            assert let_body_run.wait(timeout=5)
            yield LockState.HELD

    store = ProfileStore(path, locks=BarrierLocks())  # type: ignore[arg-type]
    result: list[BaseException | None] = []

    def replace() -> None:
        try:
            store.replace_minted_session(_request())
        except BaseException as exc:  # noqa: BLE001 - asserted below
            result.append(exc)
        else:
            result.append(None)

    worker = threading.Thread(target=replace)
    worker.start()
    assert acquire_started.wait(timeout=5)
    newest = _write(path, _owned_payload("concurrent@example.com"))
    let_body_run.set()
    worker.join(timeout=5)

    assert not worker.is_alive()
    assert len(result) == 1
    assert isinstance(result[0], _MintedSessionOwnershipRefused)
    assert "concurrent@example.com" in str(result[0])
    assert path.read_bytes() == newest


def _raw_http_cookie(
    *,
    name: str = "SID",
    value: str = "secret-value",
    domain: Any = ".google.com",
) -> HttpCookie:
    return HttpCookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=domain,
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=False,
        expires=-1,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


def test_adapter_eagerly_iterates_raw_jar_once_and_snapshots_both_inputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw_cookie = _raw_http_cookie()

    class LiveJar:
        def __init__(self) -> None:
            self.iterations = 0

        @property
        def jar(self):
            self.iterations += 1
            if self.iterations > 1:
                raise AssertionError("live jar must be observed exactly once")
            return (raw_cookie,)

    live = LiveJar()
    email = MutableEmail("owner@example.com", ["before"])
    captured: list[tuple[Path, MintedSessionWriteRequest]] = []

    class FakeStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def replace_minted_session(self, request: MintedSessionWriteRequest) -> None:
            captured.append((self.path, request))
            raw_cookie.value = "mutated-after-snapshot"
            email.notes.append("mutated-after-snapshot")

    monkeypatch.setattr(storage, "ProfileStore", FakeStore)
    assert storage.persist_minted_jar(tmp_path / "custom.json", live, email=email) is None  # type: ignore[arg-type]
    assert live.iterations == 1
    assert len(captured) == 1
    target, request = captured[0]
    assert target == tmp_path / "custom.json"
    assert request.email == "owner@example.com"
    assert request.email.notes == ["before"]  # type: ignore[union-attr]
    assert request.cookies.to_storage_rows() == [
        {
            "name": "SID",
            "value": "secret-value",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": False,
            "sameSite": "None",
        }
    ]


def test_adapter_snapshot_failure_precedes_store_parent_lock_filter_read_commit_and_logs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    marker = RuntimeError("email copy failed")

    class FailsCopy:
        def __deepcopy__(self, memo: dict[int, object]) -> object:
            raise marker

    monkeypatch.setattr(
        storage,
        "ProfileStore",
        lambda path: pytest.fail("copy failure must precede store construction"),
    )
    path = tmp_path / "missing" / "A.json"
    with (
        caplog.at_level(logging.DEBUG, logger="notebooklm.auth"),
        pytest.raises(RuntimeError) as raised,
    ):
        storage.persist_minted_jar(
            path,
            httpx.Cookies(),
            email=FailsCopy(),  # type: ignore[arg-type]
        )
    assert raised.value is marker
    assert not path.parent.exists()
    assert caplog.records == []


@pytest.mark.parametrize(
    "message",
    [
        UNKNOWN_OWNER_MESSAGE,
        "This profile already belongs to owner@example.com, but the mint is for "
        "other@example.com. Minting here would overwrite owner@example.com's session and "
        "master token. Pass force=True to overwrite this profile intentionally.",
    ],
)
def test_adapter_translates_only_private_refusal_outside_handler_without_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    message: str,
) -> None:
    class RefusingStore:
        def __init__(self, path: Path) -> None:
            pass

        def replace_minted_session(self, request: MintedSessionWriteRequest) -> None:
            raise _MintedSessionOwnershipRefused(message)

    monkeypatch.setattr(storage, "ProfileStore", RefusingStore)
    with pytest.raises(master_token.MasterTokenError) as raised:
        storage.persist_minted_jar(tmp_path / "A.json", httpx.Cookies(), email="other@example.com")
    assert type(raised.value) is master_token.MasterTokenError
    assert str(raised.value) == message
    assert raised.value.__cause__ is None
    assert raised.value.__context__ is None


def test_adapter_preserves_non_owner_exception_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    marker = OSError("commit failed")

    class FailingStore:
        def __init__(self, path: Path) -> None:
            pass

        def replace_minted_session(self, request: MintedSessionWriteRequest) -> None:
            raise marker

    monkeypatch.setattr(storage, "ProfileStore", FailingStore)
    with pytest.raises(OSError) as raised:
        storage.persist_minted_jar(tmp_path / "A.json", httpx.Cookies(), email=None)
    assert raised.value is marker
