"""Focused regression coverage for issue #2061 contracts."""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from pytest_httpx import HTTPXMock

from notebooklm import auth
from notebooklm._app.auth_check import AuthCheckPlan, run_auth_check
from notebooklm._auth import (
    cookie_semantics,
    cookies,
    psidts_recovery,
    storage,
    tokens,
)
from notebooklm._auth.cookie_policy import RequiredCookieValidationError
from notebooklm._browser import browser_capture
from notebooklm.cli.services import auth_source
from notebooklm.cli.services.auth_source import AuthSource, has_env_auth_json

_ROTATE_URL = "https://accounts.google.com/RotateCookies"
_ROTATE_URL_RE = re.compile(r"^https://accounts\.google\.com/RotateCookies$")


def _required_state(
    psidts: dict | None = None,
    *,
    secondary: bool = True,
    extra: list[dict] | None = None,
) -> dict:
    rows = [
        {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
    ]
    if secondary:
        rows.extend(
            [
                {"name": "APISID", "value": "apisid", "domain": ".google.com", "path": "/"},
                {
                    "name": "SAPISID",
                    "value": "sapisid",
                    "domain": ".google.com",
                    "path": "/",
                },
            ]
        )
    if psidts is not None:
        rows.append(
            {
                "name": "__Secure-1PSIDTS",
                "value": "psidts",
                "domain": ".google.com",
                "path": "/",
                **psidts,
            }
        )
    rows.extend(extra or [])
    return {"cookies": rows, "origins": []}


def _rotate_response(*, include_psidts: bool = True) -> dict:
    headers: list[tuple[str, str]] = []
    if include_psidts:
        headers.extend(
            [
                (
                    "Set-Cookie",
                    "__Secure-1PSIDTS=fresh-psidts; Domain=.google.com; Path=/; Secure; HttpOnly",
                ),
                (
                    "Set-Cookie",
                    "__Secure-3PSIDTS=fresh-3psidts; Domain=.google.com; Path=/; Secure; HttpOnly",
                ),
            ]
        )
    return {"status_code": 200, "headers": headers, "content": b'["identity.hfcr",600]'}


def _rotate_requests(httpx_mock: HTTPXMock) -> list[httpx.Request]:
    return [
        request for request in httpx_mock.get_requests() if _ROTATE_URL_RE.match(str(request.url))
    ]


@pytest.mark.parametrize(
    ("raw", "value", "session"),
    [
        (None, None, True),
        (-1, None, True),
        (-1.0, -1.0, False),
        ("-1", -1.0, False),
        (0, 0, False),
        (12.9, 12, False),
        ("12.9", 12, False),
        # Firefox 142+ (schema >= 16) expiry read through the unscoped
        # ``rookie_cookies.firefox()``/``any_browser()`` path arrives in
        # milliseconds with no unit tag. Anything past year 3000 as seconds
        # is implausible for a browser cookie, so it is rescaled.
        (1_755_000_000_000, 1_755_000_000, False),
        (1_755_000_000_000_000, 1_755_000_000, False),  # raw microseconds: rescaled twice
        (32_503_680_000, 32_503_680_000, False),  # exactly year 3000: not rescaled
        (32_503_680_001, 32_503_680, False),  # one second past: rescaled
    ],
)
def test_cookie_expiry_normalization_preserves_session_and_epoch(
    raw: object, value: int | float | None, session: bool
) -> None:
    normalized = cookie_semantics.normalize_cookie_expiry(raw)
    assert normalized.value == value
    assert normalized.session is session


@pytest.mark.parametrize(
    "raw",
    [True, False, "", "never", float("nan"), float("inf"), float("-inf"), [], {}, 10**1000],
)
def test_cookie_expiry_normalization_rejects_malformed_values(raw: object) -> None:
    with pytest.raises(cookie_semantics.CookieRowError):
        cookie_semantics.normalize_cookie_expiry(raw)


@pytest.mark.parametrize(
    ("raw", "expires", "discard"),
    [(None, None, True), (-1, None, True), (-1.0, -1, False), ("-1", -1, False), (0, 0, False)],
)
def test_storage_converter_derives_discard_from_normalized_expiry(
    raw: object, expires: int | None, discard: bool
) -> None:
    cookie = cookies._storage_entry_to_cookie(
        {"name": "SID", "value": "v", "domain": ".google.com", "path": "/", "expires": raw}
    )
    assert cookie.expires == expires
    assert cookie.discard is discard


def test_rookiepy_session_expiry_keeps_playwright_minus_one() -> None:
    state = cookies.convert_rookiepy_cookies_to_storage_state(
        [{"name": "SID", "value": "v", "domain": ".google.com", "expires": None}]
    )
    assert state["cookies"][0]["expires"] == -1


def test_rookiepy_conversion_preserves_playwright_same_site_attributes() -> None:
    state = cookies.convert_rookiepy_cookies_to_storage_state(
        [
            {
                "name": "SID",
                "value": "sid",
                "domain": ".google.com",
                "sameSite": "Lax",
            },
            {
                "name": "APISID",
                "value": "apisid",
                "domain": ".google.com",
                "sameSite": "Strict",
            },
        ]
    )
    assert [row["sameSite"] for row in state["cookies"]] == ["Lax", "Strict"]


def test_captured_state_validation_preserves_same_site_attributes() -> None:
    state = _required_state({"expires": None})
    state["cookies"][-1]["sameSite"] = "Lax"
    state["cookies"][0]["sameSite"] = "Strict"

    validated, error = browser_capture.heal_captured_state(state)
    assert error is None

    same_site = {row["name"]: row["sameSite"] for row in validated["cookies"]}
    assert same_site["SID"] == "Strict"
    assert same_site["__Secure-1PSIDTS"] == "Lax"


@pytest.mark.parametrize(
    ("domain", "path", "expires", "expected"),
    [
        (".google.com", "/", None, True),
        ("google.com", "/", None, True),
        ("accounts.google.com", "/", None, True),
        (".accounts.google.com", "/", None, True),
        (".notebooklm.google.com", "/", None, False),
        (".google.co.uk", "/", None, False),
        (".googleusercontent.com", "/", None, False),
        (".google.com", "/wrong", None, False),
        (".google.com", "/", 1, False),
    ],
)
def test_route_preflight_accepts_only_rotatable_psidts(
    tmp_path: Path,
    domain: str,
    path: str,
    expires: int | None,
    expected: bool,
) -> None:
    """The routing preflight itself, exercised where it is legitimately applied.

    Deliberately calls the shared builder rather than a public loader: the
    ``require_routable=True`` arm belongs only to loaders that follow a failure
    with a recovery attempt, so no public loader is an honest vehicle for it.
    The two below pin the other half — that the loaders *without* a recovery arm
    stay name-only.
    """
    state = _required_state(
        {"domain": domain, "path": path, "expires": expires}
        if domain != ".google.com" or path != "/" or expires is not None
        else {"expires": None}
    )
    state["cookies"][-1]["domain"] = domain
    state["cookies"][-1]["path"] = path

    if expected:
        jar = cookies._build_httpx_cookies_from_storage_state(state, require_routable=True)
        assert any(
            cookie.name == "__Secure-1PSIDTS" and cookie.value == "psidts" for cookie in jar.jar
        )
    else:
        with pytest.raises(RequiredCookieValidationError):
            cookies._build_httpx_cookies_from_storage_state(state, require_routable=True)


def test_download_loader_accepts_an_unrotatable_psidts(tmp_path: Path) -> None:
    """A PSIDTS the rotate URL never sees is still a cookie the app host gets.

    ``load_httpx_cookies`` backs artifact downloads and has no recovery arm, so
    hardening it would convert a working session into a permanent failure with
    no repair path. Regression guard for the first cut of #2061, which did.
    """
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(_required_state({"domain": ".notebook.google.com", "expires": None})),
        encoding="utf-8",
    )

    jar = cookies.load_httpx_cookies(path)

    assert {cookie.name for cookie in jar.jar} >= {"SID", "__Secure-1PSIDTS"}


def test_download_loader_still_rejects_a_missing_required_cookie(tmp_path: Path) -> None:
    """Name-only is not no-validation — the required-cookie contract still holds."""
    state = _required_state()
    state["cookies"] = [row for row in state["cookies"] if row["name"] != "__Secure-1PSIDTS"]
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    with pytest.raises(RequiredCookieValidationError, match="__Secure-1PSIDTS"):
        cookies.load_httpx_cookies(path)


def test_env_auth_state_is_not_hardened_when_recovery_cannot_run(
    monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    """Inline ``NOTEBOOKLM_AUTH_JSON`` has no writable store, so no heal exists.

    ``_resolve_recovery_path`` returns ``None`` for env auth and recovery
    declines. Raising the routing condition there would be a pure loss: a hard
    failure the caller has no way to repair. It must load, and fire no POST.
    """
    monkeypatch.setenv(
        "NOTEBOOKLM_AUTH_JSON",
        json.dumps(_required_state({"domain": ".notebook.google.com", "expires": None})),
    )

    jar = cookies.build_httpx_cookies_from_storage(None)

    assert {cookie.name for cookie in jar.jar} >= {"SID", "__Secure-1PSIDTS"}
    assert _rotate_requests(httpx_mock) == []


def test_declined_recovery_falls_back_instead_of_hardening(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A declined heal (throttle, contention, no rotatable binding) must not fail the load.

    Declines through a real precondition rather than a patched seam: with no
    ``OSID`` and no ``APISID``/``SAPISID`` pair the set has no rotatable
    secondary binding, so ``_recover_psidts_inline`` returns ``False`` on its
    own. That keeps the test honest about *why* recovery declined and stays
    clear of the ADR-0007 private-monkeypatch rule.
    """
    state = _required_state({"domain": ".notebook.google.com", "expires": None})
    state["cookies"] = [
        row for row in state["cookies"] if row["name"] not in {"OSID", "APISID", "SAPISID"}
    ]
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")
    assert not psidts_recovery._recover_psidts_inline(path), "precondition: recovery must decline"

    jar = cookies.build_httpx_cookies_from_storage(path)
    flat = tokens.load_auth_from_storage(path)

    assert {cookie.name for cookie in jar.jar} >= {"SID", "__Secure-1PSIDTS"}
    assert flat["__Secure-1PSIDTS"] == "psidts"


def test_validation_only_malformed_required_row_is_typed_and_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "sentinel-cookie-value"
    raw_expiry = "sentinel-raw-expiry"
    state = _required_state(
        {"value": secret, "expires": raw_expiry},
    )
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps(state), encoding="utf-8")

    def recover(_path: Path) -> bool:
        pytest.fail("validation-only loader attempted recovery")

    monkeypatch.setattr(psidts_recovery, "_recover_psidts_inline", recover)

    with (
        caplog.at_level("DEBUG", logger="notebooklm.auth"),
        pytest.raises(RequiredCookieValidationError) as caught,
    ):
        cookies.load_httpx_cookies(path)

    # Without this the redaction assertions below would pass vacuously on an
    # empty record list — which is what an accidentally-silenced logger looks
    # like. ``_auth.cookies`` logs to the "notebooklm.auth" logger by name, not
    # via ``__name__``, so that is the level to raise.
    assert caplog.records, "expected a redacted malformed-row diagnostic"
    combined = " ".join(record.getMessage() for record in caplog.records)
    assert secret not in combined
    assert raw_expiry not in combined
    assert secret not in str(caught.value)


@pytest.mark.parametrize(
    "psidts_overrides",
    [
        {"domain": ".notebooklm.google.com"},
        {"expires": 1},
        {"expires": "bad-import-expiry"},
    ],
)
def test_import_cookies_rejects_non_routable_or_malformed_required_psidts(
    tmp_path: Path, psidts_overrides: dict[str, object]
) -> None:
    from click import ClickException

    from notebooklm.cli._cookie_import import _import_cookie_json

    state = _required_state(psidts_overrides)
    storage_path = tmp_path / "storage_state.json"
    with pytest.raises(ClickException) as caught:
        _import_cookie_json(
            payload=state,
            storage_path=storage_path,
            include_domains=set(),
            include_optional=False,
        )
    assert not storage_path.exists()
    assert "bad-import-expiry" not in str(caught.value)


@pytest.mark.no_default_keepalive_mock
@pytest.mark.parametrize("initial_expires", [1, "bad-expiry"])
def test_recovery_wrapped_loader_posts_once_and_retries_malformed_or_expired(
    tmp_path: Path, httpx_mock: HTTPXMock, initial_expires: int | str
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(_required_state({"expires": initial_expires})),
        encoding="utf-8",
    )
    httpx_mock.add_response(url=_ROTATE_URL, **_rotate_response())

    assert auth.load_auth_from_storage(path)["SID"] == "sid"
    assert len(_rotate_requests(httpx_mock)) == 1
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert [row for row in saved["cookies"] if row["name"] == "__Secure-1PSIDTS"] == [
        {
            "name": "__Secure-1PSIDTS",
            "value": "fresh-psidts",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]


@pytest.mark.no_default_keepalive_mock
@pytest.mark.parametrize("loader", ["flat", "strict"])
def test_route_unusable_file_state_recovers_through_both_loaders(
    tmp_path: Path, httpx_mock: HTTPXMock, loader: str
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(_required_state({"domain": ".notebooklm.google.com"})),
        encoding="utf-8",
    )
    httpx_mock.add_response(url=_ROTATE_URL, **_rotate_response())

    if loader == "flat":
        assert auth.load_auth_from_storage(path)["SID"] == "sid"
    else:
        jar = cookies.build_httpx_cookies_from_storage(path)
        assert any(cookie.name == "SID" for cookie in jar.jar)
    assert len(_rotate_requests(httpx_mock)) == 1


@pytest.mark.no_default_keepalive_mock
@pytest.mark.parametrize("bad_value", ["", None, 17])
def test_recovery_replaces_existing_unusable_psidts_row(
    tmp_path: Path, httpx_mock: HTTPXMock, bad_value: object
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            _required_state(
                extra=[
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": bad_value,
                        "domain": ".google.com",
                        "path": "/",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    httpx_mock.add_response(url=_ROTATE_URL, **_rotate_response())

    assert auth.load_auth_from_storage(path)["SID"] == "sid"
    saved = json.loads(path.read_text(encoding="utf-8"))
    target = [
        row
        for row in saved["cookies"]
        if isinstance(row, dict) and row.get("name") == "__Secure-1PSIDTS"
    ]
    assert target == [
        {
            "name": "__Secure-1PSIDTS",
            "value": "fresh-psidts",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": True,
            "secure": True,
            "sameSite": "None",
        }
    ]
    assert len(_rotate_requests(httpx_mock)) == 1


@pytest.mark.no_default_keepalive_mock
def test_fresh_routed_loader_does_not_post(tmp_path: Path, httpx_mock: HTTPXMock) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(_required_state({"expires": time.time() + 3600})),
        encoding="utf-8",
    )
    assert auth.load_auth_from_storage(path)["SID"] == "sid"
    assert _rotate_requests(httpx_mock) == []


@pytest.mark.no_default_keepalive_mock
@pytest.mark.parametrize(
    "psidts_overrides",
    [{"expires": "bad-expiry"}, {"domain": ".notebooklm.google.com"}],
)
def test_route_unusable_rookiepy_row_recovers_in_memory_without_file_save(
    httpx_mock: HTTPXMock,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    psidts_overrides: dict[str, object],
) -> None:
    rookiepy_rows = [
        {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
        {"name": "APISID", "value": "apisid", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": "sapisid", "domain": ".google.com", "path": "/"},
        {
            "name": "__Secure-1PSIDTS",
            "value": "old-secret",
            "domain": ".google.com",
            "path": "/",
            **psidts_overrides,
        },
    ]
    monkeypatch.setenv("NOTEBOOKLM_HOME", str(tmp_path))
    httpx_mock.add_response(url=_ROTATE_URL, **_rotate_response())

    state, error = psidts_recovery.validate_with_recovery(rookiepy_rows)

    assert error is None
    assert any(
        row["name"] == "__Secure-1PSIDTS" and row["value"] == "fresh-psidts"
        for row in state["cookies"]
    )
    assert len(_rotate_requests(httpx_mock)) == 1
    assert not list(tmp_path.rglob("*")), "in-memory recovery must not create profile state"


def test_contended_recovery_requires_routed_disk_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, httpx_mock: HTTPXMock
) -> None:
    path = tmp_path / "storage_state.json"
    initial = _required_state()
    app_only = _required_state({"domain": ".notebooklm.google.com", "expires": time.time() + 3600})
    path.write_text(json.dumps(initial), encoding="utf-8")

    from contextlib import contextmanager

    @contextmanager
    def contended(_lock_path: Path):
        path.write_text(json.dumps(app_only), encoding="utf-8")
        yield False

    monkeypatch.setattr(psidts_recovery, "_file_lock_try_exclusive", contended)
    assert psidts_recovery._recover_psidts_inline(path) is False
    assert _rotate_requests(httpx_mock) == []


def test_recovery_observation_collapses_malformed_and_duplicate_targets(tmp_path: Path) -> None:
    path = tmp_path / "storage_state.json"
    state = _required_state(
        extra=[
            {
                "name": "__Secure-1PSIDTS",
                "value": "old-one",
                "domain": "google.com",
                "path": "/",
                "expires": "bad-expiry",
            },
            {
                "name": "__Secure-1PSIDTS",
                "value": "old-two",
                "domain": ".google.com",
                "path": "/",
                "expires": 1,
            },
            {"name": "UNRELATED", "value": "keep", "domain": ".google.com", "path": "/"},
            "preserve-this-row",
        ]
    )
    path.write_text(json.dumps(state), encoding="utf-8")
    jar = httpx.Cookies()
    for row in [
        {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
        {"name": "APISID", "value": "apisid", "domain": ".google.com", "path": "/"},
        {"name": "SAPISID", "value": "sapisid", "domain": ".google.com", "path": "/"},
        {
            "name": "__Secure-1PSIDTS",
            "value": "fresh",
            "domain": ".google.com",
            "path": "/",
            "expires": time.time() + 3600,
        },
    ]:
        jar.jar.set_cookie(cookies._storage_entry_to_cookie(row))
    observation = {
        storage.CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/"): frozenset(
            {"old-one", "old-two"}
        )
    }

    result = storage.save_cookies_to_storage(
        jar,
        path,
        original_snapshot={},
        recovery_observation=observation,
    )

    assert result is True
    saved = json.loads(path.read_text(encoding="utf-8"))
    rows = [row for row in saved["cookies"] if isinstance(row, dict)]
    target = [row for row in rows if row["name"] == "__Secure-1PSIDTS"]
    assert len(target) == 1
    assert target[0]["value"] == "fresh"
    assert any(row.get("name") == "UNRELATED" for row in rows)
    assert "preserve-this-row" in saved["cookies"]


def test_recovery_observation_preserves_unobserved_sibling_and_reports_cas(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            _required_state(
                extra=[
                    {
                        "name": "__Secure-1PSIDTS",
                        "value": "sibling",
                        "domain": ".google.com",
                        "path": "/",
                    }
                ]
            )
        ),
        encoding="utf-8",
    )
    jar = httpx.Cookies()
    jar.jar.set_cookie(
        cookies._storage_entry_to_cookie(
            {
                "name": "__Secure-1PSIDTS",
                "value": "fresh",
                "domain": ".google.com",
                "path": "/",
                "expires": time.time() + 3600,
            }
        )
    )
    observation = {
        storage.CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/"): frozenset({"old"})
    }
    result = storage.save_cookies_to_storage(
        jar,
        path,
        original_snapshot={},
        recovery_observation=observation,
        return_result=True,
    )
    assert isinstance(result, storage.CookieSaveResult)
    assert result.ok is False
    assert result.cas_rejected_keys
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert any(row.get("value") == "sibling" for row in saved["cookies"] if isinstance(row, dict))


def test_auth_source_env_presence_beats_profile_but_explicit_path_wins(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    profile_path = tmp_path / "profiles" / "work" / "storage_state.json"
    monkeypatch.setattr(auth_source, "get_storage_path", lambda profile=None: profile_path)
    explicit = tmp_path / "explicit.json"

    monkeypatch.delenv("NOTEBOOKLM_AUTH_JSON", raising=False)
    assert has_env_auth_json() is False
    assert (
        AuthSource.from_components(storage_override=None, profile="work").resolve() == profile_path
    )

    for value in ("", "   \n", "{}"):
        monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", value)
        assert has_env_auth_json() is True
        source = AuthSource.from_components(storage_override=None, profile="work")
        assert source.has_env_auth is True
        assert source.resolve() is None
        explicit_source = AuthSource.from_components(storage_override=explicit, profile="work")
        assert explicit_source.has_env_auth is False
        assert explicit_source.resolve() == explicit


def test_cli_auth_tokens_env_metadata_does_not_read_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from notebooklm.cli import auth_runtime, helpers

    state = _required_state({"expires": None})
    state["notebooklm"] = {
        "version": 1,
        "account": {"authuser": 3, "email": "env@example.com"},
    }
    monkeypatch.setenv("NOTEBOOKLM_AUTH_JSON", json.dumps(state))
    ctx = type("Context", (), {"obj": None})()
    with (
        patch.object(
            helpers,
            "get_client",
            return_value=(
                {"SID": "sid", "__Secure-1PSIDTS": "ts"},
                "csrf",
                "session",
            ),
        ),
        patch.object(
            auth,
            "build_httpx_cookies_from_storage",
            return_value=httpx.Cookies(),
        ),
        patch.object(auth_runtime, "read_env_auth_json", return_value=json.dumps(state)),
        patch.object(
            auth,
            "get_authuser_for_storage",
            side_effect=AssertionError("env auth must not read profile metadata"),
        ),
        patch.object(
            auth,
            "get_account_email_for_storage",
            side_effect=AssertionError("env auth must not read profile metadata"),
        ),
    ):
        result = auth_runtime.get_auth_tokens(ctx)

    assert result.authuser == 3
    assert result.account_email == "env@example.com"


@pytest.mark.asyncio
async def test_auth_check_test_recomputes_after_normal_recovery(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            _required_state({"domain": ".notebooklm.google.com", "expires": time.time() + 3600})
        ),
        encoding="utf-8",
    )
    plan = AuthCheckPlan(
        storage_path=path,
        profile="default",
        has_env_auth=False,
        has_home_env=False,
        auth_source_label="file",
        test_fetch=True,
        json_output=False,
    )

    async def heal(_path: Path, _profile: str) -> tuple[str, str]:
        path.write_text(
            json.dumps(_required_state({"expires": time.time() + 3600})), encoding="utf-8"
        )
        return "csrf", "session"

    with patch.object(auth, "fetch_tokens_with_domains", new=heal):
        result = await run_auth_check(plan, read_env_auth_json=lambda: pytest.fail("env read"))
    assert result.checks["token_fetch"] is True
    assert result.checks["cookies_present"] is True
    assert result.details["error"] is None


@pytest.mark.asyncio
async def test_auth_check_passive_continues_but_keeps_local_route_failure(
    tmp_path: Path,
) -> None:
    path = tmp_path / "storage_state.json"
    path.write_text(
        json.dumps(
            _required_state({"domain": ".notebooklm.google.com", "expires": time.time() + 3600})
        ),
        encoding="utf-8",
    )
    plan = AuthCheckPlan(
        storage_path=path,
        profile="default",
        has_env_auth=False,
        has_home_env=False,
        auth_source_label="file",
        test_fetch=True,
        json_output=False,
        passive=True,
    )
    passive = AsyncMock(return_value=("csrf", "session"))
    normal = AsyncMock(side_effect=AssertionError("normal path used"))
    with (
        patch.object(auth, "fetch_tokens_passive", passive),
        patch.object(auth, "fetch_tokens_with_domains", normal),
    ):
        result = await run_auth_check(plan, read_env_auth_json=lambda: pytest.fail("env read"))
    assert result.checks["token_fetch"] is True
    assert result.checks["cookies_present"] is False
    assert result.details["error"]
    passive.assert_awaited_once_with(path, "default")


def test_recovery_replacement_preserves_a_stored_same_site(tmp_path: Path) -> None:
    """A rotation refreshes a cookie's value — it must not erase its SameSite.

    ``_cookie_to_storage_state`` can only emit ``"None"`` because
    ``http.cookiejar.Cookie`` carries no SameSite attribute, so the recovery
    replacement path has to preserve the stored attribute the same way the two
    ordinary merge paths do. Without this the very rows recovery heals are the
    ones whose ``Lax``/``Strict`` gets downgraded (#2082 review).
    """
    path = tmp_path / "storage_state.json"
    state = _required_state({"expires": 1, "sameSite": "Lax"})
    path.write_text(json.dumps(state), encoding="utf-8")

    jar = httpx.Cookies()
    for row in [
        {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
        {
            "name": "__Secure-1PSIDTS",
            "value": "fresh",
            "domain": ".google.com",
            "path": "/",
            "expires": time.time() + 3600,
        },
    ]:
        jar.jar.set_cookie(cookies._storage_entry_to_cookie(row))

    assert storage.save_cookies_to_storage(
        jar,
        path,
        original_snapshot={},
        recovery_observation={
            storage.CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/"): frozenset({"psidts"})
        },
    )

    saved = json.loads(path.read_text(encoding="utf-8"))
    target = [row for row in saved["cookies"] if row["name"] == "__Secure-1PSIDTS"]
    assert len(target) == 1
    assert target[0]["value"] == "fresh"
    assert target[0]["sameSite"] == "Lax"
