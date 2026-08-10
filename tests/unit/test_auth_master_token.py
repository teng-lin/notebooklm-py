"""Unit tests for _auth/master_token.py (headless master-token minting).

No network: gpsoauth is faked via sys.modules; the OAuthLogin/MergeSession GETs
are intercepted with pytest-httpx's httpx_mock.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import types
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from notebooklm._auth import master_token as mt
from notebooklm._auth.master_token import BootstrapOutcome, MasterTokenError
from notebooklm._auth.master_token_bootstrap import MasterTokenBootstrapper

_OAUTHLOGIN_RE = re.compile(r"^https://accounts\.google\.com/OAuthLogin")
_MERGESESSION_RE = re.compile(r"^https://accounts\.google\.com/MergeSession")
_ROTATE_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies")


@pytest.fixture
def fake_gpsoauth(monkeypatch):
    """Install a fake `gpsoauth` module; tests set .exchange/.oauth return values."""
    mod = types.ModuleType("gpsoauth")
    mod.exchange_return = {"Token": "aas_et/MASTER"}
    mod.oauth_return = {"Auth": "ya29.FAKE"}
    mod.exchange_token = lambda email, oauth_token, android_id: mod.exchange_return
    mod.perform_oauth = lambda *a, **k: mod.oauth_return
    monkeypatch.setitem(sys.modules, "gpsoauth", mod)
    return mod


def _merge_session_cookies(*names: str) -> list[tuple[str, str]]:
    return [("set-cookie", f"{n}=val-{n}; Domain=.google.com; Path=/; Secure") for n in names]


# --- exchange_master_token -------------------------------------------------


def test_exchange_master_token_success(fake_gpsoauth):
    assert mt.exchange_master_token("e@x.com", "oauth_token", "abc") == "aas_et/MASTER"


def test_exchange_master_token_rejected(fake_gpsoauth):
    fake_gpsoauth.exchange_return = {"Error": "BadAuthentication"}
    with pytest.raises(MasterTokenError, match="oauth_token"):
        mt.exchange_master_token("e@x.com", "stale", "abc")


def test_exchange_master_token_no_secret_in_error(fake_gpsoauth):
    fake_gpsoauth.exchange_return = {"Error": "BadAuthentication"}
    with pytest.raises(MasterTokenError) as exc:
        mt.exchange_master_token("e@x.com", "SUPERSECRET", "abc")
    assert "SUPERSECRET" not in str(exc.value)


# --- mint_cookies ----------------------------------------------------------


@pytest.mark.no_default_keepalive_mock  # own the RotateCookies response (mint PSIDTS)
@pytest.mark.asyncio
async def test_mint_cookies_success_and_call_order(fake_gpsoauth, httpx_mock):
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(
        url=_MERGESESSION_RE,
        headers=_merge_session_cookies("SID", "APISID", "SAPISID", "HSID"),
    )
    # RotateCookies mints __Secure-1PSIDTS into the same jar so it's complete.
    httpx_mock.add_response(
        url=_ROTATE_RE,
        headers=_merge_session_cookies("__Secure-1PSIDTS"),
    )
    jar = await mt.mint_cookies("e@x.com", "aas_et/MASTER", "abc")
    names = {c.name for c in jar.jar}
    assert {"SID", "APISID", "SAPISID", "__Secure-1PSIDTS"} <= names
    # OAuthLogin -> MergeSession -> RotateCookies (POST), in order.
    reqs = httpx_mock.get_requests()
    assert "OAuthLogin" in str(reqs[0].url)
    assert "MergeSession" in str(reqs[1].url)
    assert "RotateCookies" in str(reqs[2].url) and reqs[2].method == "POST"
    # uberauth was forwarded to MergeSession.
    assert "APh-UBERAUTH" in str(reqs[1].url)


@pytest.mark.asyncio
async def test_mint_cookies_revoked_master_token(fake_gpsoauth, httpx_mock):
    fake_gpsoauth.oauth_return = {"Error": "BadAuthentication"}
    with pytest.raises(MasterTokenError, match="re-bootstrap|master token"):
        await mt.mint_cookies("e@x.com", "revoked", "abc")


@pytest.mark.asyncio
async def test_mint_cookies_no_uberauth(fake_gpsoauth, httpx_mock):
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="")
    with pytest.raises(MasterTokenError, match="uberauth"):
        await mt.mint_cookies("e@x.com", "aas_et/MASTER", "abc")


@pytest.mark.asyncio
async def test_mint_cookies_missing_required_cookie(fake_gpsoauth, httpx_mock):
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-X")
    # MergeSession returns SID but no secondary binding (APISID/SAPISID).
    httpx_mock.add_response(
        url=_MERGESESSION_RE,
        headers=_merge_session_cookies("SID"),
    )
    # RotateCookies still fires after MergeSession; the autouse keepalive mock
    # answers it, so the required-cookie check is what raises.
    with pytest.raises(MasterTokenError, match="missing required cookies"):
        await mt.mint_cookies("e@x.com", "aas_et/MASTER", "abc")


@pytest.mark.no_default_keepalive_mock  # own the RotateCookies response (simulate rejection)
@pytest.mark.parametrize("status_code", [429, 503])
@pytest.mark.asyncio
async def test_mint_cookies_survives_a_rejected_rotation(
    fake_gpsoauth, httpx_mock, caplog, status_code
):
    """The RotateCookies leg is best-effort inside the mint. It now runs through
    ``keepalive._rotate_post``, which adds the ``raise_for_status`` the old
    inline POST lacked, so a 429/5xx becomes an ``httpx.HTTPStatusError``
    instead of a 200-shaped no-op. The contract that pins: it is logged and
    skipped — neither silently ignored (the pre-existing bug) nor escalated
    into a failed mint by the *outer* ``except httpx.HTTPError``. The jar comes
    back without ``__Secure-1PSIDTS``, leaving the standard inline recovery to
    mint it on first load."""
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(
        url=_MERGESESSION_RE,
        headers=_merge_session_cookies("SID", "APISID", "SAPISID", "HSID"),
    )
    httpx_mock.add_response(url=_ROTATE_RE, status_code=status_code)

    with caplog.at_level(logging.DEBUG, logger="notebooklm.auth.master_token"):
        jar = await mt.mint_cookies("e@x.com", "aas_et/MASTER", "abc")

    names = {c.name for c in jar.jar}
    assert {"SID", "APISID", "SAPISID"} <= names  # the mint itself still succeeded
    assert "__Secure-1PSIDTS" not in names  # withheld: inline recovery's job now
    assert any("RotateCookies during mint failed" in r.getMessage() for r in caplog.records), (
        "a rejected rotation must be logged, not silently swallowed"
    )
    # The rotation really was attempted (and really was the rejected response).
    rotate_reqs = [r for r in httpx_mock.get_requests() if "RotateCookies" in str(r.url)]
    assert len(rotate_reqs) == 1 and rotate_reqs[0].method == "POST"


# --- remint_from_stored_token (kernel, #2103 PR-2 D1) ----------------------


@pytest.mark.no_default_keepalive_mock  # own the RotateCookies response (mint PSIDTS)
@pytest.mark.asyncio
async def test_remint_from_stored_token_success(fake_gpsoauth, httpx_mock, tmp_path):
    storage = tmp_path / "storage_state.json"
    # #2103 PR-2 D6: persist_minted_jar refuses an unrecorded/mismatched
    # owner, so the fixture must already record the SAME account the mint is
    # for (see tests/unit/test_storage_writer.py for the guard's own tests).
    storage.write_text(
        json.dumps(
            {
                "cookies": [],
                "notebooklm": {"version": 1, "account": {"authuser": 0, "email": "e@x.com"}},
            }
        ),
        encoding="utf-8",
    )
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="e@x.com",
        master_token="aas_et/MASTER",
        android_id="abc",
    )
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(
        url=_MERGESESSION_RE, headers=_merge_session_cookies("SID", "APISID", "SAPISID")
    )
    httpx_mock.add_response(url=_ROTATE_RE, headers=_merge_session_cookies("__Secure-1PSIDTS"))

    jar = await mt.remint_from_stored_token(storage)

    names = {c.name for c in jar.jar}
    assert "__Secure-1PSIDTS" in names  # the kernel's own reload succeeded
    data = json.loads(storage.read_text(encoding="utf-8"))
    assert {c["name"] for c in data["cookies"]} >= {"SID", "APISID", "SAPISID", "__Secure-1PSIDTS"}


@pytest.mark.asyncio
async def test_remint_from_stored_token_raises_when_no_token(tmp_path):
    storage = tmp_path / "storage_state.json"
    with pytest.raises(MasterTokenError, match="No master token"):
        await mt.remint_from_stored_token(storage)


@pytest.mark.no_default_keepalive_mock  # own the RotateCookies response (simulate withheld)
@pytest.mark.asyncio
async def test_remint_from_stored_token_wraps_reload_failure_when_psidts_withheld(
    fake_gpsoauth, httpx_mock, tmp_path
):
    """``mint_cookies`` is documented best-effort about PSIDTS (Google may
    withhold RotateCookies; the standard inline recovery mints it on first
    load) — but this kernel's own internal reload uses the STRICT loader,
    which enforces the Tier-1 minimum cookie set unconditionally and would
    otherwise raise a raw ``RequiredCookieValidationError`` (a ``ValueError``
    subclass) right after a persist that genuinely succeeded. Every caller of
    this "raising _auth primitive" (#2103 PR-2 D1) handles only
    ``MasterTokenError`` — a raw loader exception escaping here would crash
    ``login --master-token-refresh`` instead of a clean red error message."""
    storage = tmp_path / "storage_state.json"
    # #2103 PR-2 D6: persist_minted_jar refuses an unrecorded/mismatched
    # owner, so the fixture must already record the SAME account the mint is
    # for (see tests/unit/test_storage_writer.py for the guard's own tests).
    storage.write_text(
        json.dumps(
            {
                "cookies": [],
                "notebooklm": {"version": 1, "account": {"authuser": 0, "email": "e@x.com"}},
            }
        ),
        encoding="utf-8",
    )
    mt.write_master_token(
        tmp_path / "master_token.json",
        email="e@x.com",
        master_token="aas_et/MASTER",
        android_id="abc",
    )
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-X")
    httpx_mock.add_response(
        url=_MERGESESSION_RE, headers=_merge_session_cookies("SID", "APISID", "SAPISID")
    )
    httpx_mock.add_response(url=_ROTATE_RE)  # 200, no Set-Cookie: PSIDTS withheld

    with pytest.raises(MasterTokenError, match="reloading it failed"):
        await mt.remint_from_stored_token(storage)
    # The persist already happened before the reload failed: the caller gets
    # a typed error, not silent data loss, and the file is there for a
    # subsequent normal load's inline PSIDTS recovery to complete.
    data = json.loads(storage.read_text(encoding="utf-8"))
    assert {c["name"] for c in data["cookies"]} == {"SID", "APISID", "SAPISID"}


# --- bootstrap_storage_from_master_token / BootstrapOutcome (#2103 PR-2 D7) -


def test_bootstrap_lock_path_does_not_crash_on_a_symlink_loop(tmp_path):
    """CodeRabbit finding on the combined PR: ``_bootstrap_lock_path`` does its
    own separate ``expanduser().resolve()`` (it derives a lock path, not the
    master-token sibling), so it hadn't inherited #2103 PR-1's fix for
    ``Path.resolve()`` raising ``RuntimeError`` on a symlink loop on Python
    3.10-3.12."""
    loop_dir = tmp_path / "loop"
    try:
        loop_dir.symlink_to(loop_dir)
    except (OSError, NotImplementedError):  # pragma: no cover - platform can't self-link
        pytest.skip("platform cannot create a self-referential symlink")
    storage = loop_dir / "sub" / "storage_state.json"

    result = mt._bootstrap_lock_path(storage)  # must not raise

    assert result.name == ".storage_state.json.lock.bootstrap"


def test_bootstrap_lock_path_resolve_runtime_error_falls_back(tmp_path, monkeypatch):
    """Pins the exception-handling contract deterministically via monkeypatch
    (regardless of which Python this suite happens to run under — the real
    symlink-loop test above only exercises the raising branch on 3.10-3.12;
    on 3.13+ ``Path.resolve()`` degrades silently instead of raising)."""
    storage = tmp_path / "storage_state.json"

    def raise_runtime_error(self: Path) -> Path:
        raise RuntimeError("Symlink loop from '...'")

    monkeypatch.setattr(Path, "resolve", raise_runtime_error)

    result = mt._bootstrap_lock_path(storage)

    assert result == storage.with_name(".storage_state.json.lock.bootstrap")


def test_bootstrap_storage_from_master_token_returns_present_on_entry(tmp_path):
    """#2103 PR-2 review: the 4 BootstrapOutcome
    states must each be asserted directly, not only through the CLI's
    boolean collapse (which would not catch two states being swapped)."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    outcome = asyncio.run(mt.bootstrap_storage_from_master_token(storage))

    assert outcome is BootstrapOutcome.PRESENT_ON_ENTRY


def test_bootstrap_storage_from_master_token_returns_no_token(tmp_path):
    storage = tmp_path / "storage_state.json"  # absent, and no sibling token either

    outcome = asyncio.run(mt.bootstrap_storage_from_master_token(storage))

    assert outcome is BootstrapOutcome.NO_TOKEN


@pytest.mark.asyncio
async def test_bootstrap_storage_from_master_token_returns_minted(tmp_path):
    storage = tmp_path / "storage_state.json"
    (tmp_path / "master_token.json").write_text("{}", encoding="utf-8")

    async def mint(bootstrapper, *, strict_loader):
        del strict_loader
        bootstrapper._store.path.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    with patch.object(
        MasterTokenBootstrapper,
        "remint_from_stored_token",
        autospec=True,
        side_effect=mint,
    ):
        outcome = await mt.bootstrap_storage_from_master_token(storage)

    assert outcome is BootstrapOutcome.MINTED


@pytest.mark.asyncio
async def test_bootstrap_storage_from_master_token_returns_present_after_wait(tmp_path):
    """The leader gets MINTED; a concurrent waiter that finds storage already
    written when it re-checks after acquiring the lock must get
    PRESENT_AFTER_WAIT specifically, not just a truthy/generic result --
    these two states are meant to be distinguishable (that's the whole point
    of BootstrapOutcome over a bool)."""
    storage = tmp_path / "storage_state.json"
    (tmp_path / "master_token.json").write_text("{}", encoding="utf-8")
    started = asyncio.Event()
    release = asyncio.Event()

    async def mint(bootstrapper, *, strict_loader):
        del strict_loader
        storage_path = bootstrapper._store.path
        started.set()
        await release.wait()
        storage_path.write_text(json.dumps({"cookies": []}), encoding="utf-8")

    with patch.object(
        MasterTokenBootstrapper,
        "remint_from_stored_token",
        autospec=True,
        side_effect=mint,
    ):
        leader = asyncio.create_task(mt.bootstrap_storage_from_master_token(storage))
        await started.wait()
        follower = asyncio.create_task(mt.bootstrap_storage_from_master_token(storage))
        await asyncio.sleep(0)
        assert not follower.done()
        release.set()

        leader_outcome, follower_outcome = await asyncio.gather(leader, follower)

    assert leader_outcome is BootstrapOutcome.MINTED
    assert follower_outcome is BootstrapOutcome.PRESENT_AFTER_WAIT


# --- bootstrap_from_oauth_token (#2103 PR-2) --------------------------------


@pytest.mark.no_default_keepalive_mock  # own the RotateCookies response (mint PSIDTS)
@pytest.mark.asyncio
async def test_bootstrap_from_oauth_token_success(fake_gpsoauth, httpx_mock, tmp_path):
    storage = tmp_path / "storage_state.json"
    token_path = tmp_path / "master_token.json"
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-UBERAUTH")
    httpx_mock.add_response(
        url=_MERGESESSION_RE, headers=_merge_session_cookies("SID", "APISID", "SAPISID")
    )
    httpx_mock.add_response(url=_ROTATE_RE, headers=_merge_session_cookies("__Secure-1PSIDTS"))

    count = await mt.bootstrap_from_oauth_token(
        email="e@x.com", oauth_token="OAUTH", storage_path=storage, verify=False
    )

    assert count == -1  # verify=False
    rec = mt.read_master_token(token_path)
    assert rec["email"] == "e@x.com" and rec["master_token"] == "aas_et/MASTER"
    data = json.loads(storage.read_text(encoding="utf-8"))
    assert data["notebooklm"]["account"]["email"] == "e@x.com"


@pytest.mark.asyncio
async def test_bootstrap_from_oauth_token_raises_on_corrupted_stored_token(tmp_path):
    """#2103 PR-2 review (MAJOR finding): a corrupted
    master_token.json must not be silently overwritten with a freshly
    generated android_id. Before the fix, ``_resolve_bootstrap_android_id``
    caught ``MasterTokenError`` from ``read_master_token`` the same way it
    handled a plain missing file, discarding evidence of corruption and
    proceeding as if no token had ever existed. The pre-PR CLI driver's
    unwrapped ``read_master_token(master_token_path)`` call raised this same
    corruption loudly before any capture/mint; a direct library caller of
    ``master_token_bootstrap`` (the documented public surface) has no
    equivalent CLI-side pre-capture probe protecting it, so the library
    function itself must not swallow this."""
    storage = tmp_path / "storage_state.json"
    token_path = tmp_path / "master_token.json"
    token_path.write_text(json.dumps({"version": 99}), encoding="utf-8")

    with pytest.raises(MasterTokenError, match="malformed|version"):
        await mt.bootstrap_from_oauth_token(
            email="e@x.com", oauth_token="OAUTH", storage_path=storage, verify=False
        )
    # Must not have been silently overwritten with a fresh token/android_id.
    assert json.loads(token_path.read_text(encoding="utf-8")) == {"version": 99}


@pytest.mark.asyncio
async def test_bootstrap_from_oauth_token_does_not_write_master_token_when_storage_gate_refuses(
    fake_gpsoauth, httpx_mock, tmp_path
):
    """#2103 PR-2 review (MAJOR finding): ``write_master_token``
    must not durably persist the new account's token when the LATER,
    authoritative ``persist_minted_jar`` gate refuses. Repro: existing
    storage with no recorded account (unknown owner) and no
    ``master_token.json`` yet -- ``assert_account_writable``'s advisory
    pre-check sees no conflict (nothing recorded on either file), but
    ``persist_minted_jar``'s default ``refuse_unknown_owner=True`` refuses.
    Before the fix, ``write_master_token`` ran BEFORE ``persist_minted_jar``,
    so this exact refused call left a live, full-account master token for
    the wrong (or at least unintended) account durably on disk despite the
    caller being told the operation failed."""
    storage = tmp_path / "storage_state.json"
    storage.write_text(json.dumps({"cookies": [{"name": "OLD", "value": "x"}]}), encoding="utf-8")
    token_path = tmp_path / "master_token.json"
    httpx_mock.add_response(url=_OAUTHLOGIN_RE, text="APh-X")
    httpx_mock.add_response(
        url=_MERGESESSION_RE, headers=_merge_session_cookies("SID", "APISID", "SAPISID")
    )

    with pytest.raises(MasterTokenError, match="no recorded account owner"):
        await mt.bootstrap_from_oauth_token(
            email="someone@else.com", oauth_token="OAUTH", storage_path=storage, verify=False
        )

    assert not token_path.exists()


# --- assert_account_writable ------------------------------------------------


def test_assert_account_writable_rejects_empty_email(tmp_path):
    """#2103 PR-2 review (MINOR finding): a public-boundary
    function must raise a typed MasterTokenError, not a raw AttributeError
    from ``email.casefold()``, when called with a falsy email."""
    with pytest.raises(MasterTokenError, match="non-empty email"):
        mt.assert_account_writable(email="", storage_path=tmp_path / "storage_state.json")


# --- storage_state_from_jar -----------------------------------------------


def test_storage_state_shape_and_namespace():
    jar = httpx.Cookies()
    jar.set("SID", "v", domain=".google.com", path="/")
    state = mt.storage_state_from_jar(jar, email="e@x.com")
    assert state["origins"] == []
    sid = next(c for c in state["cookies"] if c["name"] == "SID")
    assert sid["domain"] == ".google.com" and "secure" in sid and "httpOnly" in sid
    # notebooklm namespace carries version + account (read by account.py).
    assert state["notebooklm"] == {"version": 1, "account": {"authuser": 0, "email": "e@x.com"}}


# --- master_token.json persistence ----------------------------------------


def test_write_read_master_token_roundtrip_0600(tmp_path):
    path = tmp_path / "master_token.json"
    mt.write_master_token(path, email="e@x.com", master_token="aas_et/M", android_id="abc")
    if sys.platform != "win32":  # Windows ignores POSIX mode bits
        assert (path.stat().st_mode & 0o777) == 0o600
    rec = mt.read_master_token(path)
    assert rec["master_token"] == "aas_et/M" and rec["android_id"] == "abc"


def test_read_master_token_absent(tmp_path):
    assert mt.read_master_token(tmp_path / "nope.json") is None


def test_read_master_token_malformed(tmp_path):
    path = tmp_path / "master_token.json"
    path.write_text(json.dumps({"version": 99}))
    with pytest.raises(MasterTokenError, match="malformed|version"):
        mt.read_master_token(path)


def test_read_master_token_non_dict_json(tmp_path):
    # A bare JSON array must raise MasterTokenError, not AttributeError on .get.
    path = tmp_path / "master_token.json"
    path.write_text(json.dumps([]))
    with pytest.raises(MasterTokenError, match="malformed|version"):
        mt.read_master_token(path)


def test_generate_android_id_is_16_hex():
    aid = mt.generate_android_id()
    assert len(aid) == 16 and int(aid, 16) >= 0 and aid != mt.generate_android_id()


# --- missing optional dependency ------------------------------------------


def test_missing_gpsoauth_raises_actionable(monkeypatch):
    monkeypatch.setitem(sys.modules, "gpsoauth", None)  # `import gpsoauth` -> ImportError
    with pytest.raises(MasterTokenError, match=r"notebooklm-py\[headless\]"):
        mt.exchange_master_token("e@x.com", "tok", "abc")
