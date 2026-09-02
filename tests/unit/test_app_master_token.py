"""Direct contracts for the coarse master-token app boundary."""

from __future__ import annotations

import ast
import asyncio
import gc
import inspect
import warnings
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import notebooklm._app.master_token as app
import notebooklm.cli.master_token_login as driver
from notebooklm.auth import MasterTokenError
from tests._guardrails._ast_semantics import semantic_hash as _portable_semantic_hash

_MODULE_SEMANTIC_HASH = "144a59913c9ae44daa8e71478d8cc417cfa0714e1d22a2a3af986ec2dfd98eec"


class _DirectBaseException(BaseException):
    pass


class _CoroutineStateProbe:
    def __init__(self, state: str) -> None:
        self.cr_running = False
        self.cr_suspended = state == "started"
        self.cr_frame = None if state == "completed" else SimpleNamespace(f_lasti=0)
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


def _runner(coroutine):
    return asyncio.run(coroutine)


def _traceback_names(error: BaseException) -> list[str]:
    names = []
    current = error.__traceback__
    while current is not None:
        names.append(current.tb_frame.f_code.co_name)
        current = current.tb_next
    return names


def _assert_owned_frame_scrubbed(error: BaseException, frame_name: str, names: set[str]) -> None:
    owned_frames = []
    current = error.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_name == frame_name:
            owned_frames.append(current.tb_frame.f_locals)
        current = current.tb_next
    assert len(owned_frames) == 1
    assert not names & set(owned_frames[0])


def _invoke_runner_route(route, monkeypatch, tmp_path, run_async, *, operation=None):
    storage = tmp_path / "storage_state.json"
    if route == "bootstrap":
        _install_bootstrap_collaborators(monkeypatch, tmp_path)
        if operation is not None:
            monkeypatch.setattr(app.auth, "master_token_bootstrap", Mock(return_value=operation))
        return app.bootstrap_login(
            app.MasterTokenLoginPlan("user@example.com", storage, None, False),
            oauth_token="OAUTH-RUNNER-SENTINEL",
            browser="chromium",
            cdp_url="http://localhost:9222/path",
            capture_oauth_token=Mock(),
            run_async=run_async,
        )

    if operation is None:

        async def remint(_storage_path):
            return {"SID": "JAR-RUNNER-SENTINEL"}

        monkeypatch.setattr(app.auth, "master_token_remint", remint)
    else:
        monkeypatch.setattr(app.auth, "master_token_remint", Mock(return_value=operation))
    return app.remint_login(storage, run_async=run_async)


def _install_bootstrap_collaborators(monkeypatch, tmp_path, *, count=7):
    storage = tmp_path / "profile" / "storage_state.json"
    token_path = storage.with_name("master_token.json")
    calls = []

    def assert_writable(**kwargs):
        calls.append(("writable", kwargs))

    def derive(path):
        calls.append(("derive", path))
        return token_path

    def read(path):
        calls.append(("read", path))
        return {"master_token": "aas_et/RAW-SHOULD-NOT-ESCAPE"}

    async def bootstrap(**kwargs):
        calls.append(("bootstrap", kwargs))
        return count

    monkeypatch.setattr(app.auth, "assert_account_writable", assert_writable)
    monkeypatch.setattr(app, "master_token_path_for", derive)
    monkeypatch.setattr(app.auth, "read_master_token", read)
    monkeypatch.setattr(app.auth, "master_token_bootstrap", bootstrap)
    return storage, token_path, calls


def test_bootstrap_login_preserves_order_arguments_and_secret_free_result(monkeypatch, tmp_path):
    storage, token_path, calls = _install_bootstrap_collaborators(monkeypatch, tmp_path)
    capture = Mock(return_value="CAPTURED-OAUTH-SENTINEL")
    plan = app.MasterTokenLoginPlan("user@example.com", storage, "android", False)

    result = app.bootstrap_login(
        plan,
        oauth_token=None,
        browser="chrome",
        cdp_url="http://localhost:9222/devtools/browser/id",
        timeout_s=420,
        capture_oauth_token=capture,
        run_async=_runner,
    )

    assert result == app.MasterTokenLoginSuccess(7, storage)
    assert "CAPTURED-OAUTH-SENTINEL" not in repr(result)
    assert "aas_et/RAW-SHOULD-NOT-ESCAPE" not in repr(result)
    assert [name for name, _payload in calls] == ["writable", "derive", "read", "bootstrap"]
    assert calls[0][1] == {"email": "user@example.com", "storage_path": storage, "force": False}
    assert calls[1][1] == storage
    assert calls[2][1] == token_path
    assert calls[3][1] == {
        "email": "user@example.com",
        "oauth_token": "CAPTURED-OAUTH-SENTINEL",
        "storage_path": storage,
        "android_id": "android",
        "force": False,
    }
    capture.assert_called_once_with(
        browser="chrome",
        cdp_url="http://localhost:9222/devtools/browser/id",
        timeout_s=420,
    )


@pytest.mark.parametrize("explicit", ["EXPLICIT-OAUTH-SENTINEL", ""])
def test_bootstrap_login_preserves_explicit_token_truthiness(monkeypatch, tmp_path, explicit):
    storage, _token_path, calls = _install_bootstrap_collaborators(monkeypatch, tmp_path, count=2)
    capture = Mock(return_value="CAPTURED")

    app.bootstrap_login(
        app.MasterTokenLoginPlan("user@example.com", storage, None, True),
        oauth_token=explicit,
        browser="chromium",
        cdp_url=None,
        capture_oauth_token=capture,
        run_async=_runner,
    )

    expected = explicit or "CAPTURED"
    assert calls[-1][1]["oauth_token"] == expected
    if explicit:
        capture.assert_not_called()
    else:
        capture.assert_called_once_with(browser="chromium", cdp_url=None, timeout_s=300.0)


@pytest.mark.parametrize("stage", ["writable", "read"])
def test_bootstrap_login_never_captures_after_preflight_failure(monkeypatch, tmp_path, stage):
    storage, _token_path, _calls = _install_bootstrap_collaborators(monkeypatch, tmp_path)
    failure = MasterTokenError(f"{stage} failed")
    if stage == "writable":
        monkeypatch.setattr(app.auth, "assert_account_writable", Mock(side_effect=failure))
    else:
        monkeypatch.setattr(app.auth, "read_master_token", Mock(side_effect=failure))
    capture = Mock()

    with pytest.raises(MasterTokenError) as raised:
        app.bootstrap_login(
            app.MasterTokenLoginPlan("user@example.com", storage, None, False),
            oauth_token=None,
            browser="chromium",
            cdp_url=None,
            capture_oauth_token=capture,
            run_async=_runner,
        )

    assert raised.value is failure
    capture.assert_not_called()


@pytest.mark.parametrize("failure", [RuntimeError("reject"), _DirectBaseException("base")])
def test_bootstrap_runner_rejection_closes_created_coroutine_and_scrubs(
    monkeypatch, tmp_path, failure
):
    storage, _token_path, _calls = _install_bootstrap_collaborators(monkeypatch, tmp_path)
    offered = []

    def reject(coroutine):
        offered.append(coroutine)
        raise failure

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(BaseException) as raised:
            app.bootstrap_login(
                app.MasterTokenLoginPlan("user@example.com", storage, None, False),
                oauth_token="OAUTH-TRACE-SENTINEL",
                browser="chromium",
                cdp_url=None,
                capture_oauth_token=Mock(),
                run_async=reject,
            )
        assert raised.value is failure
        assert len(offered) == 1
        assert inspect.getcoroutinestate(offered[0]) is inspect.CORO_CLOSED
        traceback_names = []
        current = failure.__traceback__
        while current is not None:
            traceback_names.append(current.tb_frame.f_code.co_name)
            if current.tb_frame.f_code.co_name == "bootstrap_login":
                assert "oauth_token" not in current.tb_frame.f_locals
                assert "capture_oauth_token" not in current.tb_frame.f_locals
                assert "run_async" not in current.tb_frame.f_locals
                assert "operation" not in current.tb_frame.f_locals
            current = current.tb_next
        assert traceback_names[-2:] == ["bootstrap_login", "reject"]
        offered.clear()
        gc.collect()
    assert not [item for item in caught if issubclass(item.category, RuntimeWarning)]


def test_bootstrap_transaction_failure_keeps_identity_chain_and_late_facade_patch(
    monkeypatch, tmp_path
):
    storage, _token_path, _calls = _install_bootstrap_collaborators(monkeypatch, tmp_path)
    cause = ValueError("cause")
    failure = MasterTokenError("transaction failed")
    failure.__cause__ = cause
    transaction = AsyncMock(side_effect=failure)
    monkeypatch.setattr(app.auth, "master_token_bootstrap", transaction)

    with pytest.raises(MasterTokenError) as raised:
        app.bootstrap_login(
            app.MasterTokenLoginPlan("user@example.com", storage, None, False),
            oauth_token="TOKEN",
            browser="chromium",
            cdp_url=None,
            capture_oauth_token=Mock(),
            run_async=_runner,
        )

    assert raised.value is failure
    assert raised.value.__cause__ is cause
    transaction.assert_awaited_once()


def test_remint_login_discards_jar_and_returns_path_only(monkeypatch, tmp_path):
    storage = tmp_path / "storage_state.json"
    jar = {"SID": "COOKIE-SENTINEL"}
    operation = AsyncMock(return_value=jar)
    monkeypatch.setattr(app.auth, "master_token_remint", operation)

    result = app.remint_login(storage, run_async=_runner)

    assert result == app.MasterTokenRemintSuccess(storage)
    assert "COOKIE-SENTINEL" not in repr(result)
    operation.assert_awaited_once_with(storage)


def test_remint_runner_rejection_closes_created_coroutine(monkeypatch, tmp_path):
    storage = tmp_path / "storage_state.json"

    async def remint(_path):
        return object()

    monkeypatch.setattr(app.auth, "master_token_remint", remint)
    failure = RuntimeError("runner rejected")
    offered = []

    def reject(coroutine):
        offered.append(coroutine)
        raise failure

    with warnings.catch_warnings(record=True) as caught, pytest.raises(RuntimeError) as raised:
        warnings.simplefilter("always")
        app.remint_login(storage, run_async=reject)

    assert raised.value is failure
    assert inspect.getcoroutinestate(offered.pop()) is inspect.CORO_CLOSED
    gc.collect()
    assert not [item for item in caught if issubclass(item.category, RuntimeWarning)]


@pytest.mark.parametrize("route", ["bootstrap", "remint"])
def test_runner_failure_preserves_active_outer_exception(monkeypatch, tmp_path, route):
    outer_error = LookupError("unrelated outer error")
    runner_error = RuntimeError("runner rejected")
    offered = []

    def reject(operation):
        offered.append(operation)
        raise runner_error

    try:
        raise outer_error
    except LookupError as active:
        retained_outer = active
        with pytest.raises(RuntimeError) as raised:
            _invoke_runner_route(route, monkeypatch, tmp_path, reject)

    assert raised.value is runner_error
    assert runner_error.__cause__ is None
    assert runner_error.__context__ is retained_outer
    assert runner_error.__suppress_context__ is False
    assert inspect.getcoroutinestate(offered.pop()) is inspect.CORO_CLOSED


@pytest.mark.parametrize("route", ["bootstrap", "remint"])
def test_runner_cancellation_closes_created_coroutine_and_scrubs_owned_frame(
    monkeypatch, tmp_path, route
):
    cancellation = asyncio.CancelledError("runner cancelled")
    offered = []

    def reject(operation):
        offered.append(operation)
        raise cancellation

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        with pytest.raises(asyncio.CancelledError) as raised:
            _invoke_runner_route(route, monkeypatch, tmp_path, reject)

        assert raised.value is cancellation
        assert inspect.getcoroutinestate(offered[0]) is inspect.CORO_CLOSED
        _assert_owned_frame_scrubbed(
            cancellation,
            f"{route}_login",
            {
                "oauth_token",
                "cdp_url",
                "capture_oauth_token",
                "run_async",
                "operation",
                "jar",
                "result",
            },
        )
        offered.clear()
        gc.collect()
    assert not [item for item in caught if issubclass(item.category, RuntimeWarning)]


@pytest.mark.parametrize("route", ["bootstrap", "remint"])
@pytest.mark.parametrize("state", ["started", "completed"])
def test_runner_failure_never_closes_started_or_completed_coroutine(
    monkeypatch, tmp_path, route, state
):
    operation = _CoroutineStateProbe(state)
    failure = RuntimeError(f"runner failed after operation {state}")

    def reject(offered):
        assert offered is operation
        raise failure

    with pytest.raises(RuntimeError) as raised:
        _invoke_runner_route(route, monkeypatch, tmp_path, reject, operation=operation)

    assert raised.value is failure
    assert operation.close_calls == 0


@pytest.mark.parametrize("error_type", [RuntimeError, asyncio.CancelledError])
def test_cold_bootstrap_adds_exactly_one_app_frame_and_preserves_chain(
    monkeypatch, tmp_path, error_type
):
    storage = tmp_path / "storage_state.json"

    async def capture_error(operation):
        try:
            await operation
        except BaseException as error:
            return error, _traceback_names(error)
        raise AssertionError("operation did not fail")

    def lower_failure(label):
        failure = error_type(label)
        cause = ValueError(f"{label} cause")
        contexts = []

        async def lower(_storage_path):
            try:
                raise LookupError(f"{label} context")
            except LookupError as context:
                contexts.append(context)
                raise failure from cause

        return failure, cause, contexts, lower

    direct_error, direct_cause, direct_contexts, direct_lower = lower_failure("direct")
    monkeypatch.setattr(
        app.auth,
        "bootstrap_missing_storage_from_master_token",
        direct_lower,
    )
    captured_direct, direct_names = asyncio.run(capture_error(direct_lower(storage)))

    wrapped_error, wrapped_cause, wrapped_contexts, wrapped_lower = lower_failure("wrapped")
    monkeypatch.setattr(
        app.auth,
        "bootstrap_missing_storage_from_master_token",
        wrapped_lower,
    )
    from notebooklm.cli.services import auth_refresh

    captured_wrapped, wrapped_names = asyncio.run(
        capture_error(auth_refresh.bootstrap_missing_storage_from_master_token(storage))
    )

    assert captured_direct is direct_error
    assert direct_error.__cause__ is direct_cause
    assert direct_error.__context__ is direct_contexts[0]
    assert direct_error.__suppress_context__ is True
    assert captured_wrapped is wrapped_error
    assert wrapped_error.__cause__ is wrapped_cause
    assert wrapped_error.__context__ is wrapped_contexts[0]
    assert wrapped_error.__suppress_context__ is True
    assert wrapped_names == [
        direct_names[0],
        "bootstrap_missing_storage_from_master_token",
        *direct_names[1:],
    ]


def test_cold_bootstrap_is_one_await_with_exact_identity(monkeypatch, tmp_path):
    storage = tmp_path / "storage_state.json"
    operation = AsyncMock(return_value=True)
    monkeypatch.setattr(app.auth, "bootstrap_missing_storage_from_master_token", operation)

    assert asyncio.run(app.bootstrap_missing_storage_from_master_token(storage)) is True
    operation.assert_awaited_once_with(storage)


def test_status_env_auth_short_circuits_path_and_read(monkeypatch, tmp_path):
    derive = Mock()
    read = Mock()
    monkeypatch.setattr(app, "master_token_path_for", derive)
    monkeypatch.setattr(app.auth, "read_master_token", read)

    result = app.inspect_master_token_status(tmp_path / "storage.json", has_env_auth=True)

    assert result == app.MasterTokenStatus(False, None, None, None)
    derive.assert_not_called()
    read.assert_not_called()


def test_status_absent_and_present_projections(monkeypatch, tmp_path):
    storage = tmp_path / "storage_state.json"
    token_path = tmp_path / "master_token.json"
    monkeypatch.setattr(app, "master_token_path_for", Mock(return_value=token_path))
    read = Mock(return_value={"email": 17, "master_token": "SECRET"})
    monkeypatch.setattr(app.auth, "read_master_token", read)

    absent = app.inspect_master_token_status(storage, has_env_auth=False)
    assert absent == app.MasterTokenStatus(False, token_path, None, None)
    read.assert_not_called()

    token_path.write_text("present", encoding="utf-8")
    present = app.inspect_master_token_status(storage, has_env_auth=False)
    assert present == app.MasterTokenStatus(True, token_path, 17, None)
    assert "SECRET" not in repr(present)
    read.assert_called_once_with(token_path)


def test_status_catches_only_the_token_read(monkeypatch, tmp_path):
    storage = tmp_path / "storage_state.json"
    token_path = tmp_path / "master_token.json"
    token_path.write_text("present", encoding="utf-8")
    monkeypatch.setattr(app, "master_token_path_for", Mock(return_value=token_path))
    read_failure = OSError("READ-DETAIL-SENTINEL")
    monkeypatch.setattr(app.auth, "read_master_token", Mock(side_effect=read_failure))

    result = app.inspect_master_token_status(storage, has_env_auth=False)

    assert result == app.MasterTokenStatus(True, token_path, None, "OSError")
    assert "READ-DETAIL-SENTINEL" not in repr(result)

    derive_failure = RuntimeError("derive")
    monkeypatch.setattr(app, "master_token_path_for", Mock(side_effect=derive_failure))
    with pytest.raises(RuntimeError) as raised:
        app.inspect_master_token_status(storage, has_env_auth=False)
    assert raised.value is derive_failure

    exists_failure = OSError("exists")

    class ExplodingPath:
        def exists(self):
            raise exists_failure

    monkeypatch.setattr(app, "master_token_path_for", Mock(return_value=ExplodingPath()))
    with pytest.raises(OSError) as raised_exists:
        app.inspect_master_token_status(storage, has_env_auth=False)
    assert raised_exists.value is exists_failure


def test_status_projection_and_base_exception_escape_exactly(monkeypatch, tmp_path):
    storage = tmp_path / "storage_state.json"
    token_path = tmp_path / "master_token.json"
    token_path.write_text("present", encoding="utf-8")
    monkeypatch.setattr(app, "master_token_path_for", Mock(return_value=token_path))

    class ExplodingRecord(dict):
        def get(self, key, default=None):
            del key, default
            raise projection_failure

    projection_failure = RuntimeError("projection")
    monkeypatch.setattr(app.auth, "read_master_token", Mock(return_value=ExplodingRecord(x=1)))
    with pytest.raises(RuntimeError) as raised:
        app.inspect_master_token_status(storage, has_env_auth=False)
    assert raised.value is projection_failure

    base_failure = _DirectBaseException("base")
    monkeypatch.setattr(app.auth, "read_master_token", Mock(side_effect=base_failure))
    with pytest.raises(_DirectBaseException) as raised_base:
        app.inspect_master_token_status(storage, has_env_auth=False)
    assert raised_base.value is base_failure


def test_driver_drops_ctx_and_secret_capabilities_before_propagation(monkeypatch, tmp_path):
    class Context:
        obj = {"profile": "profile"}

    failure = RuntimeError("driver failure")
    monkeypatch.setattr(driver, "get_storage_path", Mock(return_value=tmp_path / "storage.json"))
    monkeypatch.setattr(driver, "bootstrap_login", Mock(side_effect=failure))

    with pytest.raises(RuntimeError) as raised:
        driver.run_master_token_login(
            Context(),
            storage=None,
            browser="chromium",
            account_email="user@example.com",
            oauth_token="DRIVER-OAUTH-SENTINEL",
            android_id=None,
            cdp_url="http://localhost:9222/path",
            refresh=False,
        )

    assert raised.value is failure
    current = failure.__traceback__
    while current is not None:
        if current.tb_frame.f_code.co_name == "run_master_token_login":
            for name in (
                "ctx",
                "oauth_token",
                "cdp_url",
                "capture_oauth_token",
                "run_async",
                "plan",
                "login_result",
            ):
                assert name not in current.tb_frame.f_locals
        current = current.tb_next


def _boundary_violations(source: str) -> set[str]:
    tree = ast.parse(source)
    violations = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            names = [alias.name for alias in node.names]
            module = node.module if isinstance(node, ast.ImportFrom) else ""
            if any("_auth" in name or ".cli" in name for name in [module or "", *names]):
                violations.add("private-import")
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"getattr", "globals", "locals", "vars", "eval", "exec"}:
                violations.add("dynamic-access")
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            value = node.value
            if isinstance(value, ast.Attribute) and isinstance(value.value, ast.Name):
                if value.value.id == "auth" and value.attr != "MasterTokenError":
                    violations.add("captured-facade")
    semantic_hash = _portable_semantic_hash(tree)
    if semantic_hash != _MODULE_SEMANTIC_HASH:
        violations.add("module-semantic-drift")
    return violations


def test_app_boundary_live_source_and_biting_mutations_are_fail_closed():
    source = inspect.getsource(app)
    assert _boundary_violations(source) == set()
    assert _boundary_violations(source + "\nfrom notebooklm._auth import master_token\n") == {
        "module-semantic-drift",
        "private-import",
    }
    assert _boundary_violations(source + '\nwriter = getattr(auth, "master_token_remint")\n') == {
        "dynamic-access",
        "module-semantic-drift",
    }
    assert _boundary_violations(source + "\nwriter = auth.master_token_remint\n") == {
        "captured-facade",
        "module-semantic-drift",
    }


def test_app_boundary_semantic_backstop_bites_every_frozen_mutation():
    source = inspect.getsource(app)
    mutations = {
        "copied-public-import": source + "\nfrom notebooklm.auth import master_token_remint\n",
        "default-bound-operation": source
        + "\ndef bad(operation=auth.master_token_remint):\n    return operation\n",
        "facade-alias": source + "\nfacade = auth\nwriter = facade.master_token_remint\n",
        "facade-mapping": source + '\nwriter = auth.__dict__["master_token_remint"]\n',
        "retained-callback": source.replace("        del capture_oauth_token\n", "", 1),
        "token-bearing-result": source.replace(
            "    notebook_count: int\n    storage_path: Path\n",
            "    notebook_count: int\n    oauth_token: str\n    storage_path: Path\n",
            1,
        ),
        "broad-catch": source.replace(
            "        except BaseException:\n",
            "        except Exception:\n",
            1,
        ),
        "raise-exception-object": source.replace(
            "        except BaseException:\n",
            "        except BaseException as exc:\n",
            1,
        ).replace("            raise\n", "            raise exc\n", 1),
        "second-runner-call": source.replace(
            "            count = run_async(operation)\n",
            "            count = run_async(operation)\n            run_async(operation)\n",
            1,
        ),
    }

    assert all(mutated != source for mutated in mutations.values())
    assert {name: _boundary_violations(mutated) for name, mutated in mutations.items()} == {
        name: {"module-semantic-drift"} for name in mutations
    }
    assert (
        _boundary_violations(source + "\n# Comment-only changes stay outside the AST.\n") == set()
    )


def test_public_error_alias_and_service_identity_are_exact():
    from notebooklm import auth
    from notebooklm.cli.services import auth_refresh

    assert app.MasterTokenError is auth.MasterTokenError
    assert app.MasterTokenError.__module__ == "notebooklm._auth.master_token"
    assert (
        auth_refresh.bootstrap_missing_storage_from_master_token
        is app.bootstrap_missing_storage_from_master_token
    )
