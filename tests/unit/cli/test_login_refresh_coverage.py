"""Coverage-focused unit tests for ``cli/services/login/refresh.py``.
These tests target failure / edge branches not exercised by the broader
``test_login.py`` / ``test_login_multi_account.py`` suites:
* ``_login_browser_cookies_single`` — targeted-extraction write-outcome exit.
* ``_login_all_accounts_from_browser`` — enumeration-outcome exit, the
  no-accounts early return, and the per-account write-outcome exit (lines
  231, 234-235, 275).
* ``_refresh_from_browser_cookies`` — enumeration-outcome exit, the
  no-accounts exit, and the write-outcome exit.
* ``_login_with_browser_cookies`` — the OSError save path, the
  account-metadata clear/write branches, and each cookie-verification
  failure branch.
Collaborators are patched at their ``refresh`` module import sites so each
driver runs in isolation without a real browser / network.
"""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest

import notebooklm.cli.playwright_login_io as playwright_login_io_module
from notebooklm._auth.storage import LoginWriteOutcome, LoginWriteStatus
from notebooklm.cli.services.login import refresh
from notebooklm.cli.services.login.outcomes import BrowserCookieOutcome

REFRESH = "notebooklm.cli.services.login.refresh"
# The async bridge is no longer ``refresh.run_async`` (#1393 inverted it behind
# the injected ``LoginIO`` sink). With no ``io`` injected these drivers resolve
# the command-layer default sink (``PlaywrightLoginIO``), whose ``run_async``
# binds ``cli.playwright_login_io.run_async``. Patching it here intercepts the
# async probe while leaving ``emit`` → ``console.print`` intact so ``capsys``
# still captures the rendered warning lines.


def _account(email: str, *, authuser: int = 0, browser_profile: str = "Default") -> Any:
    return SimpleNamespace(email=email, authuser=authuser, browser_profile=browser_profile)


def _outcome(message: str = "[red]boom[/red]") -> BrowserCookieOutcome:
    """Build a concrete failure outcome instance."""
    obj = BrowserCookieOutcome.__new__(BrowserCookieOutcome)
    object.__setattr__(obj, "code", "TEST_FAILURE")
    object.__setattr__(obj, "message", message)
    return obj


def _deps(**overrides: Any) -> refresh.RefreshDeps:
    return replace(refresh.default_refresh_deps(), **overrides)


def _login_base_deps(**overrides: Any) -> refresh.RefreshDeps:
    # Required cookies on an allowlisted domain: the write-time filter and
    # the post-filter Tier-1 recheck both run on this state, so the success
    # paths need SID + __Secure-1PSIDTS to survive filtering.
    storage_state = {
        "cookies": [
            {"name": "SID", "domain": ".google.com", "path": "/"},
            {"name": "__Secure-1PSIDTS", "domain": ".google.com", "path": "/"},
        ],
        "origins": [],
    }
    return _deps(
        read_browser_cookies=MagicMock(return_value=["raw"]),
        validate_with_recovery=MagicMock(return_value=(storage_state, None)),
        cookie_names_from_storage=MagicMock(return_value={"SID", "__Secure-1PSIDTS"}),
        missing_cookies_hint=MagicMock(return_value="hint"),
        sync_server_language_to_config=MagicMock(),
        fetch_tokens_with_domains=MagicMock(return_value=None),
        **overrides,
    )


# ---------------------------------------------------------------------------
# _login_browser_cookies_single — targeted write-outcome exit
# ---------------------------------------------------------------------------
def test_login_single_enum_outcome_exits(tmp_path) -> None:
    """An enumeration outcome in the targeted path exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_browser_cookies_single(
            "chrome",
            storage=None,
            account_email="bob@example.com",
            profile_name=None,
            active_profile="work",
            deps=deps,
        )
    assert exc_info.value.code == 1


def test_login_single_targeted_write_outcome_exits(tmp_path) -> None:
    """A write-outcome from the targeted extraction path exits 1."""
    account = _account("bob@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        select_account=MagicMock(return_value=account),
        confirm_profile_account_overwrite=MagicMock(),
        write_extracted_cookies=MagicMock(return_value=_outcome()),
        get_storage_path=MagicMock(return_value=tmp_path / "s.json"),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_browser_cookies_single(
            "chrome",
            storage=None,
            account_email="bob@example.com",
            profile_name=None,
            active_profile="work",
            deps=deps,
        )
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _login_all_accounts_from_browser — enum, no-accounts, and write-outcome paths
# ---------------------------------------------------------------------------
def test_login_all_accounts_enum_outcome_exits() -> None:
    """An enumeration outcome exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_all_accounts_from_browser("chrome", deps=deps)
    assert exc_info.value.code == 1


def test_login_all_accounts_no_accounts_returns(capsys) -> None:
    """No discovered accounts returns early with a notice."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=({}, [])))
    refresh._login_all_accounts_from_browser("chrome", deps=deps)
    out = capsys.readouterr().out
    assert "No accounts discovered" in out


def test_login_all_accounts_write_outcome_exits(tmp_path) -> None:
    """A per-account write outcome exits 1."""
    account = _account("alice@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        list_profiles=MagicMock(return_value=[]),
        profiles_by_account_email=MagicMock(return_value={}),
        resolve_all_accounts_target=MagicMock(return_value="alice"),
        email_to_profile_name=MagicMock(return_value="alice"),
        get_storage_path=MagicMock(return_value=tmp_path / "alice.json"),
        write_extracted_cookies=MagicMock(return_value=_outcome()),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_all_accounts_from_browser("chrome", deps=deps)
    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# _refresh_from_browser_cookies — enum, no-accounts, and write-outcome paths
# ---------------------------------------------------------------------------
def test_refresh_enum_outcome_exits(tmp_path) -> None:
    """An enumeration outcome exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1


def test_refresh_no_accounts_exits(tmp_path, capsys) -> None:
    """No signed-in accounts exits 1."""
    deps = _deps(enumerate_browser_accounts=MagicMock(return_value=({}, [])))
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1
    assert "No signed-in Google accounts" in capsys.readouterr().out


def test_refresh_select_outcome_exits(tmp_path) -> None:
    """A select-refresh-account outcome exits 1."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        read_account_metadata=MagicMock(return_value={}),
        select_refresh_account=MagicMock(return_value=_outcome()),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1


def test_refresh_success_prints_summary(tmp_path, capsys) -> None:
    """A successful non-quiet refresh prints the ok/account summary."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        read_account_metadata=MagicMock(return_value={}),
        select_refresh_account=MagicMock(return_value=account),
        write_extracted_cookies=MagicMock(return_value=None),
        sync_server_language_to_config=MagicMock(),
    )
    refresh._refresh_from_browser_cookies(
        "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=False, deps=deps
    )
    out = capsys.readouterr().out
    assert "refreshed from chrome" in out
    assert "carol@example.com" in out


def test_refresh_write_outcome_exits(tmp_path) -> None:
    """A write outcome exits 1."""
    account = _account("carol@example.com", browser_profile="Default")
    per_profile = {"Default": ["cookie"]}
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=(per_profile, [account])),
        read_account_metadata=MagicMock(return_value={}),
        select_refresh_account=MagicMock(return_value=account),
        write_extracted_cookies=MagicMock(return_value=_outcome()),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._refresh_from_browser_cookies(
            "chrome", storage_path=tmp_path / "s.json", profile="work", quiet=True, deps=deps
        )
    assert exc_info.value.code == 1


def test_login_with_cookies_read_outcome_exits(tmp_path) -> None:
    """An outcome from ``_read_browser_cookies`` exits 1."""
    deps = _deps(read_browser_cookies=MagicMock(return_value=_outcome()))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    assert exc_info.value.code == 1


def test_login_with_cookies_validation_error_exits(tmp_path, capsys) -> None:
    """A validation error from ``validate_with_recovery`` exits 1."""
    deps = _deps(
        read_browser_cookies=MagicMock(return_value=["raw"]),
        validate_with_recovery=MagicMock(
            return_value=({"cookies": []}, "missing required cookies")
        ),
        cookie_names_from_storage=MagicMock(return_value=[]),
        missing_cookies_hint=MagicMock(return_value="install hint"),
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    assert exc_info.value.code == 1
    assert "No valid Google authentication cookies" in capsys.readouterr().out


def test_login_with_cookies_save_oserror_exits(tmp_path) -> None:
    """An OSError while writing storage exits 1."""
    deps = _login_base_deps(replace_from_login=MagicMock(side_effect=OSError("disk full")))
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_with_browser_cookies(tmp_path / "out" / "storage.json", "chrome", deps=deps)
    assert exc_info.value.code == 1


def test_login_with_cookies_lock_unavailable_exits(tmp_path) -> None:
    """A fail-closed ``lock_unavailable`` writer outcome exits 1 (nothing saved).

    Since b-PR3 the account metadata is embedded in the same atomic write, so a
    lock failure is a whole-write failure — no separate best-effort metadata step
    survives it."""
    deps = _login_base_deps(
        replace_from_login=MagicMock(
            return_value=LoginWriteOutcome(LoginWriteStatus.LOCK_UNAVAILABLE)
        )
    )
    with pytest.raises(SystemExit) as exc_info:
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    assert exc_info.value.code == 1


def test_login_with_cookies_account_line_printed(tmp_path, capsys) -> None:
    """When an email is provided the Account: line is printed.

    The account binding is now embedded in the writer's single atomic write
    (``AccountRecord``); the real writer runs here."""
    deps = _login_base_deps()
    with patch.object(playwright_login_io_module, "run_async"):
        refresh._login_with_browser_cookies(
            tmp_path / "storage.json",
            "chrome",
            authuser=2,
            email="dave@example.com",
            deps=deps,
        )
    out = capsys.readouterr().out
    assert "dave@example.com" in out


def test_login_with_cookies_verify_valueerror_warns(tmp_path, capsys) -> None:
    """A ValueError from verification warns but does not exit."""
    deps = _login_base_deps()
    with patch.object(
        playwright_login_io_module,
        "run_async",
        side_effect=ValueError("invalid cookies"),
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    out = capsys.readouterr().out
    assert "failed validation" in out


def test_login_with_cookies_verify_network_error_warns(tmp_path, capsys) -> None:
    """A network RequestError warns but does not exit."""
    deps = _login_base_deps()
    with patch.object(
        playwright_login_io_module,
        "run_async",
        side_effect=httpx.RequestError("connect failed"),
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    out = capsys.readouterr().out
    assert "network issue" in out


def test_login_with_cookies_verify_unexpected_error_warns(tmp_path, capsys) -> None:
    """An unexpected error warns but does not exit."""
    deps = _login_base_deps()
    with patch.object(
        playwright_login_io_module,
        "run_async",
        side_effect=RuntimeError("boom"),
    ):
        refresh._login_with_browser_cookies(tmp_path / "storage.json", "chrome", deps=deps)
    out = capsys.readouterr().out
    assert "Unexpected error during verification" in out


# ---------------------------------------------------------------------------
# _login_with_browser_cookies — write-time cookie-domain filtering
# ---------------------------------------------------------------------------
def _rookiepy_cookie(domain: str, name: str) -> dict:
    """A rookiepy-shaped raw cookie row (matches the extractor output)."""
    return {
        "domain": domain,
        "name": name,
        "value": "v",
        "path": "/",
        "secure": True,
        "expires": None,
        "http_only": False,
    }


def _sibling_heavy_jar() -> list[dict]:
    """Required auth cookies plus sibling-product rows.

    Mirrors what the Firefox container extractor can hand the writer: its
    suffix-based domain match (rookiepy semantics: dot-prefixed = suffix
    match) returns cookies for every ``*.google.com`` subdomain even when
    the extraction request contained only ``REQUIRED_COOKIE_DOMAINS``.
    """
    return [
        _rookiepy_cookie(".google.com", "SID"),
        _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
        _rookiepy_cookie("mail.google.com", "MAIL_OSID"),
        _rookiepy_cookie("docs.google.com", "DOCS_OSID"),
        _rookiepy_cookie("myaccount.google.com", "ACCOUNT_OSID"),
        _rookiepy_cookie(".youtube.com", "YOUTUBE_SID"),
    ]


def _login_and_read_storage(tmp_path, jar: list[dict] | None = None, **login_kwargs) -> dict:
    """Drive ``_login_with_browser_cookies`` with the real validate → filter →
    write pipeline (only the network/verification seams stubbed) and return
    the persisted ``storage_state.json``."""
    import json

    storage_path = tmp_path / "storage_state.json"
    deps = _deps(
        read_browser_cookies=MagicMock(
            return_value=jar if jar is not None else _sibling_heavy_jar()
        ),
        sync_server_language_to_config=MagicMock(),
        fetch_tokens_with_domains=MagicMock(return_value=None),
    )
    with patch.object(playwright_login_io_module, "run_async"):
        refresh._login_with_browser_cookies(storage_path, "chrome", deps=deps, **login_kwargs)
    return json.loads(storage_path.read_text())


def test_login_with_cookies_keeps_google_subdomains_but_drops_youtube(tmp_path) -> None:
    """Default login uses trusted-root suffix matching for compatibility."""
    persisted = _login_and_read_storage(tmp_path)
    names = {c["name"] for c in persisted["cookies"]}
    assert {
        "SID",
        "__Secure-1PSIDTS",
        "MAIL_OSID",
        "DOCS_OSID",
        "ACCOUNT_OSID",
    } <= names
    assert "YOUTUBE_SID" not in names


def test_login_with_cookies_opt_in_persists_youtube(tmp_path) -> None:
    """``--include-domains=youtube`` adds the distinct YouTube root."""
    persisted = _login_and_read_storage(tmp_path, include_domains={"youtube"})
    names = {c["name"] for c in persisted["cookies"]}
    assert {"SID", "__Secure-1PSIDTS", "YOUTUBE_SID"} <= names


def test_login_with_cookies_functional_domains_survive(tmp_path) -> None:
    """Drive-ingest and media-download domains pass the write-time filter.

    ``drive.google.com`` (both dotted variants) and
    ``.googleusercontent.com`` are in ``REQUIRED_COOKIE_DOMAINS`` (Drive
    ingest redirects, artifact media downloads), so a default browser-cookie
    login must persist them unchanged. Compatibility suffix matching also
    keeps the actual Drive download host and host-scoped media domains.
    """
    jar = [
        _rookiepy_cookie(".google.com", "SID"),
        _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
        _rookiepy_cookie("drive.google.com", "DRIVE_HOST"),
        _rookiepy_cookie(".drive.google.com", "DRIVE_DOT"),
        _rookiepy_cookie("drive.usercontent.google.com", "DRIVE_DOWNLOAD"),
        _rookiepy_cookie(".googleusercontent.com", "GUC_DOMAIN"),
        _rookiepy_cookie("lh3.googleusercontent.com", "GUC_HOST"),
    ]
    persisted = _login_and_read_storage(tmp_path, jar=jar)
    names = {c["name"] for c in persisted["cookies"]}
    assert {
        "SID",
        "__Secure-1PSIDTS",
        "DRIVE_HOST",
        "DRIVE_DOT",
        "DRIVE_DOWNLOAD",
        "GUC_DOMAIN",
        "GUC_HOST",
    } <= names


def test_login_with_cookies_required_only_on_sibling_domain_exits(tmp_path, capsys) -> None:
    """Filtering away a required cookie exits 1 instead of writing.

    The converter accepts opted-in runtime domains, so a jar whose only
    ``SID`` sits on ``youtube.com`` passes ``validate_with_recovery``;
    the write-time filter then drops that row. The post-filter Tier-1
    recheck must exit 1 (writing nothing) rather than persisting unusable
    auth with a success exit.
    """
    storage_path = tmp_path / "storage_state.json"
    jar = [
        _rookiepy_cookie(".youtube.com", "SID"),
        _rookiepy_cookie(".google.com", "__Secure-1PSIDTS"),
    ]
    deps = _deps(
        read_browser_cookies=MagicMock(return_value=jar),
        sync_server_language_to_config=MagicMock(),
        fetch_tokens_with_domains=MagicMock(return_value=None),
    )
    with (
        patch.object(playwright_login_io_module, "run_async"),
        pytest.raises(SystemExit) as exc_info,
    ):
        refresh._login_with_browser_cookies(storage_path, "chrome", deps=deps)
    assert exc_info.value.code == 1
    assert "SID" in capsys.readouterr().out
    assert not storage_path.exists()


def test_refresh_forwards_include_domains_to_writer(tmp_path) -> None:
    """``_refresh_from_browser_cookies`` threads the opt-in set to the writer."""
    account = _account("bob@example.com", browser_profile="Default")
    writer = MagicMock(return_value=None)
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=({"Default": ["raw"]}, [account])),
        read_account_metadata=MagicMock(return_value={"email": "bob@example.com"}),
        select_refresh_account=MagicMock(return_value=account),
        write_extracted_cookies=writer,
        sync_server_language_to_config=MagicMock(),
    )
    refresh._refresh_from_browser_cookies(
        "chrome",
        storage_path=tmp_path / "storage_state.json",
        profile=None,
        quiet=True,
        include_domains={"youtube"},
        deps=deps,
    )
    writer.assert_called_once()
    assert writer.call_args.kwargs["include_domains"] == {"youtube"}


def test_login_single_targeted_forwards_include_domains_to_writer(tmp_path) -> None:
    """``_login_browser_cookies_single`` threads the opt-in set to the writer."""
    account = _account("bob@example.com", browser_profile="Default")
    writer = MagicMock(return_value=None)
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=({"Default": ["raw"]}, [account])),
        select_account=MagicMock(return_value=account),
        confirm_profile_account_overwrite=MagicMock(),
        get_storage_path=MagicMock(return_value=tmp_path / "storage_state.json"),
        write_extracted_cookies=writer,
        sync_server_language_to_config=MagicMock(),
    )
    refresh._login_browser_cookies_single(
        "chrome",
        storage=None,
        account_email="bob@example.com",
        profile_name=None,
        active_profile="work",
        include_domains={"youtube"},
        deps=deps,
    )
    writer.assert_called_once()
    assert writer.call_args.kwargs["include_domains"] == {"youtube"}


def test_login_all_accounts_forwards_include_domains_to_writer(tmp_path) -> None:
    """``_login_all_accounts_from_browser`` threads the opt-in set to the writer."""
    account = _account("alice@example.com", browser_profile="Default")
    writer = MagicMock(return_value=None)
    deps = _deps(
        enumerate_browser_accounts=MagicMock(return_value=({"Default": ["raw"]}, [account])),
        list_profiles=MagicMock(return_value=[]),
        profiles_by_account_email=MagicMock(return_value={}),
        email_to_profile_name=MagicMock(return_value="alice"),
        resolve_all_accounts_target=MagicMock(return_value="alice"),
        get_storage_path=MagicMock(return_value=tmp_path / "alice.json"),
        write_extracted_cookies=writer,
        sync_server_language_to_config=MagicMock(),
    )
    refresh._login_all_accounts_from_browser(
        "chrome",
        include_domains={"youtube"},
        deps=deps,
    )
    writer.assert_called_once()
    assert writer.call_args.kwargs["include_domains"] == {"youtube"}
