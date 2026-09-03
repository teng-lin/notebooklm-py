"""Direct behavior tests for the login-cookie application boundary."""

from __future__ import annotations

import ast
import asyncio
import gc
import inspect
import traceback
import warnings
import weakref
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

import notebooklm._app.login_cookie as login_cookie_app
import notebooklm.auth as auth
import notebooklm.cli.services.login.browser_accounts as browser_accounts_module
import notebooklm.cli.services.login.chromium_accounts as chromium_accounts_module
import notebooklm.cli.services.login.cookie_domains as cookie_domains_module
import notebooklm.cli.services.login.cookie_jar as cookie_jar_module
from notebooklm._app.login_cookie import (
    BrowserCookieProbeFailure,
    BrowserCookieProbeRequest,
    BrowserCookieProbeSuccess,
    CookieDomainFailure,
    CookieImportFailure,
    CookieImportRequest,
    CookieImportSuccess,
    has_usable_secondary_binding,
    import_cookie_payload,
    normalize_cookie_payload,
    parse_cookie_domain_labels,
    probe_browser_cookie_jar,
    project_browser_account,
    resolve_cookie_domains,
)
from notebooklm._auth.profile_store import ReplaceResult, ReplaceStatus
from notebooklm.cli import _cookie_import as cookie_import_module
from tests._guardrails._ast_semantics import semantic_hash as _portable_semantic_hash


def _row(name: str, value: str = "value") -> dict[str, Any]:
    return {"name": name, "value": value, "domain": ".google.com", "path": "/"}


def _valid_rows() -> list[dict[str, Any]]:
    return [_row("SID"), _row("__Secure-1PSIDTS"), _row("OSID")]


class _WeakList(list[object]):
    __slots__ = ("__weakref__",)


class _DirectBaseException(BaseException):
    pass


def _release_exception_traceback(error: BaseException) -> None:
    traceback.clear_frames(error.__traceback__)
    error.__traceback__ = None


def _writer_result(
    status: ReplaceStatus = ReplaceStatus.APPLIED,
    *,
    missing_required: tuple[str, ...] = (),
    present_names: tuple[str, ...] = (),
    backup_path: Path | None = None,
) -> ReplaceResult:
    return ReplaceResult(status, missing_required, present_names, backup_path)


def test_request_and_result_repr_redact_cookie_values(tmp_path: Path) -> None:
    request = CookieImportRequest(
        payload={"secret": "cookie-value"},
        storage_path=tmp_path / "state.json",
        include_domains=set(),
        include_optional=False,
    )
    success = CookieImportSuccess(storage_state={"secret": "cookie-value"}, backup_path=None)

    assert "cookie-value" not in repr(request)
    assert "cookie-value" not in repr(success)


def test_domain_parse_resolve_and_invalid_are_deterministic() -> None:
    selection = parse_cookie_domain_labels((" YouTube,docs", "youtube,,"))
    assert not isinstance(selection, CookieDomainFailure)
    assert selection.labels == frozenset({"youtube", "docs"})
    assert selection.extraction_domains == tuple(sorted(selection.extraction_domains))
    assert auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL["youtube"] <= selection.optional_domains

    failure = parse_cookie_domain_labels(("youtube,wat",))
    assert isinstance(failure, CookieDomainFailure)
    assert failure.invalid_labels == ("wat",)
    assert "all" in failure.supported_labels
    with pytest.raises(KeyError):
        resolve_cookie_domains({"wat"})


def test_normalize_payload_shapes_defaults_and_secure_prefix() -> None:
    payload = {
        "cookies": [
            {
                "name": "__Secure-SID",
                "value": "x",
                "domain": ".google.com",
                "expirationDate": 42,
            }
        ],
        "origins": [{"origin": "credential-bearing"}],
    }
    result = normalize_cookie_payload(payload)

    assert isinstance(result, dict)
    assert result["origins"] == []
    assert result["cookies"] == [
        {
            "name": "__Secure-SID",
            "value": "x",
            "domain": ".google.com",
            "expires": 42,
            "path": "/",
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        }
    ]


@pytest.mark.parametrize("payload, code", [({}, "INVALID_SHAPE"), ([1], "INVALID_ENTRY")])
def test_normalize_payload_rejects_invalid_boundaries(payload: object, code: str) -> None:
    result = normalize_cookie_payload(payload)
    assert isinstance(result, CookieImportFailure)
    assert result.code == code


@pytest.mark.parametrize(
    "rows, expected",
    [
        ([_row("OSID", "")], False),
        ([_row("OSID", ""), _row("OSID", "ok")], True),
        ([_row("APISID"), _row("SAPISID"), _row("LSID")], True),
        ([_row("APISID"), _row("SAPISID")], False),
    ],
)
def test_secondary_binding_uses_nonempty_duplicate_aware_names(
    rows: list[dict[str, Any]], expected: bool
) -> None:
    assert has_usable_secondary_binding({"cookies": rows}) is expected


def test_import_preserves_callback_order_identity_and_backup(tmp_path: Path) -> None:
    events: list[tuple[str, object, object]] = []
    domains = {"youtube"}
    filtered: dict[str, Any] = {"cookies": _valid_rows(), "origins": ["drop"]}
    backup_path = tmp_path / "state.json.bak"

    def filter_state(
        state: dict[str, Any], *, include_domains: set[str] | None, include_optional: bool
    ) -> dict[str, Any]:
        events.append(("filter", state, include_domains))
        assert include_domains is domains
        assert include_optional is True
        return filtered

    def writer(
        path: Path,
        state: dict[str, Any],
        *,
        include_domains: set[str] | None,
        include_optional: bool,
        account_mode: str,
        backup: bool,
    ) -> Any:
        events.append(("writer", state, include_domains))
        assert path == tmp_path / "state.json"
        assert include_domains is domains
        assert include_optional is True
        assert account_mode == "keep"
        assert backup is True
        return _writer_result(backup_path=backup_path)

    request = CookieImportRequest(
        payload=_valid_rows(),
        storage_path=tmp_path / "state.json",
        include_domains=domains,
        include_optional=True,
    )
    result = import_cookie_payload(
        request,
        filter_storage_state=filter_state,
        replace_profile_from_login=writer,
    )

    assert isinstance(result, CookieImportSuccess)
    assert result.backup_path is backup_path
    assert result.storage_state is events[1][1]
    assert result.storage_state["origins"] == []
    assert [event[0] for event in events] == ["filter", "writer"]


def test_import_failure_precedence_and_no_extra_write(tmp_path: Path) -> None:
    writer = MagicMock()
    request = CookieImportRequest(
        payload=[_row("SID", ""), _row("__Secure-1PSIDTS")],
        storage_path=tmp_path / "state.json",
        include_domains=set(),
        include_optional=False,
    )
    result = import_cookie_payload(
        request,
        filter_storage_state=lambda state, **_: state,
        replace_profile_from_login=writer,
    )
    assert isinstance(result, CookieImportFailure)
    assert result.code == "EMPTY_REQUIRED"
    writer.assert_not_called()


def test_import_writer_required_drop_precedes_lock(tmp_path: Path) -> None:
    result = import_cookie_payload(
        CookieImportRequest(
            payload=_valid_rows(),
            storage_path=tmp_path / "state.json",
            include_domains=set(),
            include_optional=False,
        ),
        filter_storage_state=lambda state, **_: state,
        replace_profile_from_login=lambda *_, **__: _writer_result(
            ReplaceStatus.REQUIRED_COOKIES_DROPPED,
            missing_required=("SID",),
        ),
    )
    assert isinstance(result, CookieImportFailure)
    assert result.code == "REQUIRED_DROPPED"


def test_import_unexpected_filter_error_is_same_object(tmp_path: Path) -> None:
    error = asyncio.CancelledError("stop")

    def fail(*_: object, **__: object) -> dict[str, Any]:
        raise error

    with pytest.raises(asyncio.CancelledError) as caught:
        import_cookie_payload(
            CookieImportRequest(_valid_rows(), tmp_path / "state.json", set(), False),
            filter_storage_state=fail,
            replace_profile_from_login=MagicMock(),
        )
    assert caught.value is error
    assert caught.value.__cause__ is None
    current = caught.value.__traceback__
    while current is not None:
        if current.tb_frame.f_globals.get("__name__") == "notebooklm._app.login_cookie":
            for name in ("request", "payload", "normalized_state", "filter_storage_state"):
                assert name not in current.tb_frame.f_locals
        current = current.tb_next


def test_payload_and_raw_cookie_canaries_collect_after_licensed_traceback_release(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    payload: _WeakList | None = _WeakList(_valid_rows())
    payload_ref = weakref.ref(payload)
    filter_failure = _DirectBaseException("filter")

    def fail_filter(*_: object, **__: object) -> dict[str, Any]:
        raise filter_failure

    with pytest.raises(_DirectBaseException):
        import_cookie_payload(
            CookieImportRequest(payload, tmp_path / "state.json", set(), False),
            filter_storage_state=fail_filter,
            replace_profile_from_login=MagicMock(),
        )
    _release_exception_traceback(filter_failure)
    payload = None
    gc.collect()
    assert payload_ref() is None

    _patch_probe_facade(monkeypatch, [])
    raw_cookies: _WeakList | None = _WeakList(_valid_rows())
    raw_ref = weakref.ref(raw_cookies)
    runner_failure = _DirectBaseException("runner")

    def reject(_: object) -> list[auth.Account]:
        raise runner_failure

    with pytest.raises(_DirectBaseException):
        probe_browser_cookie_jar(
            BrowserCookieProbeRequest(raw_cookies, "chrome", None, False, True),
            run_probe=reject,
            validate_with_recovery=lambda _: ({"cookies": _valid_rows()}, None),
        )
    _release_exception_traceback(runner_failure)
    raw_cookies = None
    gc.collect()
    assert raw_ref() is None


def test_projection_uses_late_facade_constructor_once(monkeypatch: pytest.MonkeyPatch) -> None:
    source = auth.Account(7, "one@example.com", False, "old")
    sentinel = object()
    constructor = MagicMock(return_value=sentinel)
    monkeypatch.setattr(auth, "Account", constructor)

    result = project_browser_account(source, browser_profile="Profile 2", is_default=True)

    assert result is sentinel
    constructor.assert_called_once_with(
        authuser=7,
        email="one@example.com",
        is_default=True,
        browser_profile="Profile 2",
    )


def _patch_probe_facade(monkeypatch: pytest.MonkeyPatch, accounts: list[auth.Account]) -> None:
    monkeypatch.setattr(auth, "extract_cookies_with_domains", lambda state, **_: state)
    monkeypatch.setattr(auth, "build_cookie_jar", lambda **_: httpx.Cookies())

    async def enumerate_accounts(_: httpx.Cookies) -> list[auth.Account]:
        return accounts

    monkeypatch.setattr(auth, "enumerate_accounts", enumerate_accounts)


def test_probe_validates_once_and_preserves_untagged_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [auth.Account(0, "one@example.com", True)]
    _patch_probe_facade(monkeypatch, accounts)
    validator = MagicMock(return_value=({"cookies": _valid_rows()}, None))
    result = probe_browser_cookie_jar(
        BrowserCookieProbeRequest(_valid_rows(), "chrome", None, False, True),
        run_probe=lambda awaitable: asyncio.run(awaitable),
        validate_with_recovery=validator,
    )
    assert isinstance(result, BrowserCookieProbeSuccess)
    assert result.accounts == (accounts[0],)
    assert result.accounts[0] is accounts[0]
    validator.assert_called_once()


def test_probe_after_mode_converts_then_validates_one_sample(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    accounts = [auth.Account(0, "one@example.com", True)]
    _patch_probe_facade(monkeypatch, accounts)
    converted = {"cookies": _valid_rows()}
    converter = MagicMock(return_value=converted)
    monkeypatch.setattr(auth, "convert_rookiepy_cookies_to_storage_state", converter)
    validator = MagicMock(return_value=({"cookies": []}, ValueError("missing SID")))

    result = probe_browser_cookie_jar(
        BrowserCookieProbeRequest(_valid_rows(), "chrome", None, False, False),
        run_probe=lambda awaitable: asyncio.run(awaitable),
        validate_with_recovery=validator,
    )
    assert isinstance(result, BrowserCookieProbeFailure)
    assert result.code == "COOKIE_VALIDATION"
    converter.assert_called_once()
    validator.assert_called_once()


def test_failed_runner_closes_created_native_coroutine(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe_facade(monkeypatch, [])
    captured: list[object] = []
    error = RuntimeError("bridge failed")

    def fail(awaitable: object) -> list[auth.Account]:
        captured.append(awaitable)
        raise error

    with warnings.catch_warnings(record=True) as caught_warnings:
        warnings.simplefilter("always")
        with pytest.raises(RuntimeError) as caught:
            probe_browser_cookie_jar(
                BrowserCookieProbeRequest(_valid_rows(), "chrome", None, False, True),
                run_probe=fail,
                validate_with_recovery=lambda _: ({"cookies": _valid_rows()}, None),
            )
        assert caught.value is error
        assert inspect.getcoroutinestate(captured[0]) is inspect.CORO_CLOSED
        current = caught.value.__traceback__
        while current is not None:
            if current.tb_frame.f_globals.get("__name__") == "notebooklm._app.login_cookie":
                for name in (
                    "request",
                    "raw_cookies",
                    "jar",
                    "probe_coroutine",
                    "run_probe",
                    "validate_with_recovery",
                ):
                    assert name not in current.tb_frame.f_locals
            current = current.tb_next
        captured.clear()
        gc.collect()
    assert not [item for item in caught_warnings if issubclass(item.category, RuntimeWarning)]


@pytest.mark.parametrize("settlement", ["suspended", "completed"])
def test_runner_started_coroutine_is_never_closed_twice(
    monkeypatch: pytest.MonkeyPatch, settlement: str
) -> None:
    class SuspendOnce:
        def __await__(self):
            yield

    async def enumerate_accounts(_: httpx.Cookies) -> list[auth.Account]:
        if settlement == "suspended":
            await SuspendOnce()
        return []

    monkeypatch.setattr(auth, "extract_cookies_with_domains", lambda state, **_: state)
    monkeypatch.setattr(auth, "build_cookie_jar", lambda **_: httpx.Cookies())
    monkeypatch.setattr(auth, "enumerate_accounts", enumerate_accounts)
    captured: list[object] = []
    error = RuntimeError("runner failed after consumption began")

    def consume_then_fail(awaitable: object) -> list[auth.Account]:
        captured.append(awaitable)
        if settlement == "suspended":
            awaitable.send(None)
            assert inspect.getcoroutinestate(awaitable) is inspect.CORO_SUSPENDED
        else:
            asyncio.run(awaitable)
            assert inspect.getcoroutinestate(awaitable) is inspect.CORO_CLOSED
        raise error

    with pytest.raises(RuntimeError) as caught:
        probe_browser_cookie_jar(
            BrowserCookieProbeRequest(_valid_rows(), "chrome", None, False, True),
            run_probe=consume_then_fail,
            validate_with_recovery=lambda _: ({"cookies": _valid_rows()}, None),
        )
    assert caught.value is error
    expected = inspect.CORO_SUSPENDED if settlement == "suspended" else inspect.CORO_CLOSED
    assert inspect.getcoroutinestate(captured[0]) is expected
    if settlement == "suspended":
        captured[0].close()


def test_quiet_network_error_is_reraised_same_object(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_probe_facade(monkeypatch, [])
    error = httpx.ConnectError("offline")

    def fail(_: object) -> list[auth.Account]:
        raise error

    with pytest.raises(httpx.ConnectError) as caught:
        probe_browser_cookie_jar(
            BrowserCookieProbeRequest(_valid_rows(), "chrome", None, True, True),
            run_probe=fail,
            validate_with_recovery=lambda _: ({"cookies": _valid_rows()}, None),
        )
    assert caught.value is error


#: ``cookie_import`` and ``cookie_jar`` were re-pinned when their canonical
#: cookie-policy calls were routed through identity-equal ``auth`` facade
#: aliases, preserving the CLI boundary without changing their behavior.
_SEMANTIC_HASHES = {
    "login_cookie": "ba6c11e67a09365d017b9ec46aed5e37eb7173f864ba45746ca289fc49bed2dd",
    "cookie_import": "eae4f81f14a18dadfb980155145f9b77b00fd856f9f04fb4e21c654dbb466021",
    "browser_accounts": "0b4278d9971e3522be70481aa98d0dde869455be703aa8f24e61840fb16e79ed",
    "chromium_accounts": "3f7241d07681f66823c212bb3b8292caa59b65a402d022537b134ca3e6995c92",
    "cookie_domains": "a09898adcc9d4e9cba6308fd4bd8a13d36ca9a17bc462834eb27f5854d1e65c3",
    "cookie_jar": "7fc28ec2055d19cfdb708659b22050a7752c97c873f82a231d47c9a7be2b5b83",
}


def _semantic_hash(source: str) -> str:
    return _portable_semantic_hash(ast.parse(source))


def _module_sources() -> dict[str, str]:
    return {
        "login_cookie": inspect.getsource(login_cookie_app),
        "cookie_import": inspect.getsource(cookie_import_module),
        "browser_accounts": inspect.getsource(browser_accounts_module),
        "chromium_accounts": inspect.getsource(chromium_accounts_module),
        "cookie_domains": inspect.getsource(cookie_domains_module),
        "cookie_jar": inspect.getsource(cookie_jar_module),
    }


def _has_outer_finally(tree: ast.Module, function_name: str) -> bool:
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == function_name
    )
    return any(isinstance(node, ast.Try) and node.finalbody for node in function.body)


def _guard_violations(app_source: str, chromium_source: str) -> set[str]:
    tree = ast.parse(app_source)
    chromium_tree = ast.parse(chromium_source)
    violations: set[str] = set()
    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            names = [item.name for item in node.names]
            imported = ".".join([module or "", *names]).lower()
            if "_auth" in imported:
                violations.add("private-auth-import")
            if any(
                part in imported for part in ("notebooklm.cli", "click", "rich", "rpc", "server")
            ):
                violations.add("ui-import")
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name):
            if node.value.id == "Callable" and any(
                isinstance(child, ast.Constant) and child.value is Ellipsis
                for child in ast.walk(node.slice)
            ):
                violations.add("callable-ellipsis")
        if isinstance(node, ast.ExceptHandler) and isinstance(node.type, ast.Name):
            if node.type.id == "Exception":
                violations.add("broad-catch")
        if isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in {"Account", "type"}:
                violations.add("account-bypass")
            if (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "dataclasses"
                and node.func.attr == "replace"
            ):
                violations.add("account-bypass")
        if isinstance(node, ast.BoolOp):
            constants = {
                child.value
                for child in ast.walk(node)
                if isinstance(child, ast.Constant) and isinstance(child.value, str)
            }
            if {"OSID", "APISID", "SAPISID", "LSID"} <= constants:
                violations.add("private-policy-copy")

    for node in tree.body:
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        value = node.value
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        target_names = {target.id for target in targets if isinstance(target, ast.Name)}
        if isinstance(value, (ast.List, ast.Dict, ast.Set)):
            violations.add("module-mutation")
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id in {"list", "dict", "set"}
        ):
            violations.add("module-mutation")
        if (
            target_names != {"Account"}
            and isinstance(value, ast.Attribute)
            and isinstance(value.value, ast.Name)
            and value.value.id == "auth"
        ):
            violations.add("retained-callback")

    sensitive_fields = {"payload", "raw_cookies", "storage_state"}
    for class_node in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        for field_node in class_node.body:
            if not isinstance(field_node, ast.AnnAssign) or not isinstance(
                field_node.target, ast.Name
            ):
                continue
            if field_node.target.id not in sensitive_fields:
                continue
            value = field_node.value
            repr_false = (
                isinstance(value, ast.Call)
                and isinstance(value.func, ast.Name)
                and value.func.id == "field"
                and any(
                    keyword.arg == "repr"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is False
                    for keyword in value.keywords
                )
            )
            if not repr_false:
                violations.add("payload-repr")

    for function in (node for node in tree.body if isinstance(node, ast.FunctionDef)):
        defaults = [*function.args.defaults, *(item for item in function.args.kw_defaults if item)]
        if any(
            isinstance(child, ast.Attribute)
            and isinstance(child.value, ast.Name)
            and child.value.id == "auth"
            for default in defaults
            for child in ast.walk(default)
        ):
            violations.add("eager-default")

    for name in ("import_cookie_payload", "project_browser_account", "probe_browser_cookie_jar"):
        if not _has_outer_finally(tree, name):
            violations.add("missing-finally")
    if not _has_outer_finally(chromium_tree, "_enumerate_chromium_profiles_fanout"):
        violations.add("missing-finally")

    probe = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "probe_browser_cookie_jar"
    )
    probe_calls = [node for node in ast.walk(probe) if isinstance(node, ast.Call)]
    if not any(
        isinstance(call.func, ast.Name) and call.func.id == "project_browser_account"
        for call in probe_calls
    ):
        violations.add("probe-projector-bypass")
    close_calls = [
        call
        for call in probe_calls
        if isinstance(call.func, ast.Attribute) and call.func.attr == "close"
    ]
    if len(close_calls) != 1 or not any(
        isinstance(ancestor, ast.If) and "CORO_CREATED" in ast.dump(ancestor.test)
        for ancestor in (
            parents.get(close_calls[0]),
            parents.get(parents.get(close_calls[0])),
            parents.get(parents.get(parents.get(close_calls[0]))),
        )
        if ancestor is not None
    ):
        violations.add("close-lifecycle")

    fanout = next(
        node
        for node in chromium_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_enumerate_chromium_profiles_fanout"
    )
    fanout_names = {
        node.func.id
        for node in ast.walk(fanout)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    if "project_account" not in fanout_names:
        violations.add("chromium-projector-bypass")
    return violations


def test_full_module_semantic_hashes_are_exact_and_comments_are_ast_clean() -> None:
    sources = _module_sources()
    assert {name: _semantic_hash(source) for name, source in sources.items()} == _SEMANTIC_HASHES
    assert {
        name: _semantic_hash(source + "\n# comment-only change\n")
        for name, source in sources.items()
    } == _SEMANTIC_HASHES


def test_app_semantic_guard_bites_every_frozen_boundary_mutation() -> None:
    app_source = inspect.getsource(login_cookie_app)
    chromium_source = inspect.getsource(chromium_accounts_module)
    assert _guard_violations(app_source, chromium_source) == set()

    mutations = [
        (app_source + "\nfrom notebooklm._auth import cookies\n", "private-auth-import"),
        (app_source + "\nfrom notebooklm.cli import session_cmd\n", "ui-import"),
        (app_source + "\nimport click\n", "ui-import"),
        (
            app_source
            + "\nfrom collections.abc import Callable\ndef bad(cb: Callable[..., int]): ...\n",
            "callable-ellipsis",
        ),
        (
            app_source.replace("payload: object = field(repr=False)", "payload: object"),
            "payload-repr",
        ),
        (app_source + "\ndef eager(cb=auth.enumerate_accounts): ...\n", "eager-default"),
        (app_source + "\n_RETAINED_VALUES = []\n", "module-mutation"),
        (
            app_source
            + "\ndef copy(names): return 'OSID' in names or {'APISID', 'SAPISID', 'LSID'} <= names\n",
            "private-policy-copy",
        ),
        (app_source.replace("except BaseException:", "except Exception:", 1), "broad-catch"),
        (
            app_source.replace(
                "    finally:\n        del request, raw_cookies",
                "    except Exception:\n        del request, raw_cookies",
                1,
            ),
            "missing-finally",
        ),
        (app_source + "\ndef bypass(account): return Account(1, 'x', True)\n", "account-bypass"),
        (
            app_source + "\ndef bypass(account): return type(account)(1, 'x', True)\n",
            "account-bypass",
        ),
        (
            app_source
            + "\nimport dataclasses\ndef bypass(account): return dataclasses.replace(account)\n",
            "account-bypass",
        ),
        (
            app_source.replace(
                "                    project_browser_account(",
                "                    Account(",
                1,
            ),
            "probe-projector-bypass",
        ),
        (app_source + "\n_RETAINED = auth.enumerate_accounts\n", "retained-callback"),
        (
            app_source.replace(
                "                    probe_coroutine.close()",
                "                    probe_coroutine.close()\n                    probe_coroutine.close()",
                1,
            ),
            "close-lifecycle",
        ),
        (
            app_source.replace(
                "if inspect.iscoroutine(probe_coroutine) and (\n"
                "                    inspect.getcoroutinestate(probe_coroutine) is inspect.CORO_CREATED\n"
                "                ):",
                "if True:",
                1,
            ),
            "close-lifecycle",
        ),
    ]
    for mutated, expected in mutations:
        assert expected in _guard_violations(mutated, chromium_source)

    chromium_bypass = chromium_source.replace(
        "projected_account = project_account(",
        "projected_account = Account(",
        1,
    )
    assert "chromium-projector-bypass" in _guard_violations(app_source, chromium_bypass)
