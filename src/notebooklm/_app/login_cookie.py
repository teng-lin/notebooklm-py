"""Transport-neutral cookie import and browser-account probing."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

from .. import auth

Account = auth.Account


@dataclass(frozen=True, slots=True)
class CookieDomainSelection:
    """Validated domain labels and their resolved extraction domains."""

    labels: frozenset[str]
    supported_labels: tuple[str, ...]
    optional_domains: frozenset[str]
    extraction_domains: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CookieDomainFailure:
    """Unknown domain labels and the labels accepted by the application."""

    invalid_labels: tuple[str, ...]
    supported_labels: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CookieImportRequest:
    """One already-read cookie payload to validate and persist."""

    payload: object = field(repr=False)
    storage_path: Path
    include_domains: set[str]
    include_optional: bool


@dataclass(frozen=True, slots=True)
class CookieImportSuccess:
    """Sanitized state and the optional backup made by the writer."""

    storage_state: dict[str, Any] = field(repr=False)
    backup_path: Path | None


@dataclass(frozen=True, slots=True)
class CookieImportFailure:
    """Value-free cookie import failure for presentation adapters."""

    code: Literal[
        "INVALID_SHAPE",
        "INVALID_ENTRY",
        "EMPTY_REQUIRED",
        "INVALID_REQUIRED",
        "INVALID_BINDING",
        "REQUIRED_DROPPED",
        "LOCK_UNAVAILABLE",
    ]
    message: str


@dataclass(frozen=True, slots=True)
class BrowserCookieProbeRequest:
    """One already-read browser-extractor cookie set to probe."""

    raw_cookies: list[dict[str, Any]] = field(repr=False)
    browser_name: str
    browser_profile: str | None
    quiet: bool
    validate_before_probe: bool


@dataclass(frozen=True, slots=True)
class BrowserCookieProbeSuccess:
    """Accounts visible through the supplied cookie set."""

    accounts: tuple[Account, ...]


@dataclass(frozen=True, slots=True)
class BrowserCookieProbeFailure:
    """Cookie-probe failure without raw credential-bearing values."""

    code: Literal["COOKIE_VALIDATION", "STALE_COOKIES", "NETWORK_ERROR"]
    detail: str
    cookie_names: frozenset[str] = frozenset()
    hint: str | None = None


class StorageStateFilter(Protocol):
    """Domain-policy projection used by cookie imports."""

    def __call__(
        self,
        state: dict[str, Any],
        *,
        include_domains: set[str] | None,
        include_optional: bool = False,
    ) -> dict[str, Any]: ...


class LoginWriter(Protocol):
    """Native path-shaped login/import profile writer."""

    def __call__(
        self,
        path: Path,
        state: Mapping[str, Any],
        *,
        include_domains: set[str] | None,
        include_optional: bool = False,
        account_mode: Literal["keep", "clear", "set"] = "keep",
        account_authuser: int | None = None,
        account_email: str | None = None,
        backup: bool = False,
    ) -> auth.ReplaceResult: ...


class ValidateWithRecovery(Protocol):
    """Route-aware browser-cookie validator."""

    def __call__(
        self, rookiepy_cookies: list[dict[str, Any]]
    ) -> tuple[dict[str, Any], ValueError | None]: ...


class ProbeRunner(Protocol):
    """Synchronous bridge for the account-probe coroutine."""

    def __call__(self, awaitable: Awaitable[list[Account]]) -> list[Account]: ...


def _supported_domain_labels() -> tuple[str, ...]:
    return tuple(sorted((*auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL, "all")))


def parse_cookie_domain_labels(
    values: tuple[str, ...],
) -> CookieDomainSelection | CookieDomainFailure:
    """Parse comma-separated domain labels and resolve their domain set."""
    labels = {
        label
        for raw_value in values
        for part in raw_value.split(",")
        if (label := part.strip().lower())
    }
    supported = _supported_domain_labels()
    invalid = tuple(sorted(labels - set(supported)))
    if invalid:
        return CookieDomainFailure(invalid_labels=invalid, supported_labels=supported)
    return resolve_cookie_domains(labels)


def resolve_cookie_domains(
    labels: set[str], *, include_optional: bool = False
) -> CookieDomainSelection:
    """Resolve validated labels to optional and complete extraction domains."""
    supported = _supported_domain_labels()
    if include_optional or "all" in labels:
        optional = frozenset().union(*auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL.values())
    else:
        selected: set[str] = set()
        for label in labels:
            selected.update(auth.OPTIONAL_COOKIE_DOMAINS_BY_LABEL[label])
        optional = frozenset(selected)
    extraction = set(auth.REQUIRED_COOKIE_DOMAINS | optional)
    extraction.update(f".google.{cctld}" for cctld in auth.GOOGLE_REGIONAL_CCTLDS)
    return CookieDomainSelection(
        labels=frozenset(labels),
        supported_labels=supported,
        optional_domains=optional,
        extraction_domains=tuple(sorted(extraction)),
    )


def _normalize_cookie_entry(cookie: object) -> dict[str, Any] | CookieImportFailure:
    normalized: dict[str, Any] | None = None
    result: dict[str, Any] | CookieImportFailure | None = None
    try:
        if not isinstance(cookie, dict):
            result = CookieImportFailure(
                code="INVALID_ENTRY",
                message=(
                    "Each cookie must be a JSON object; the cookie list contains "
                    "a non-object entry."
                ),
            )
            return result
        try:
            normalized = auth._validate_cookie_shape(cookie, require_nonempty_value=False)
        except ValueError:
            result = dict(cookie)
            return result
        if "expires" not in normalized:
            normalized["expires"] = normalized.pop("expirationDate", -1)
        normalized.setdefault("path", "/")
        normalized.setdefault("httpOnly", False)
        if normalized["name"].startswith(("__Secure-", "__Host-")):
            normalized["secure"] = True
        else:
            normalized.setdefault("secure", False)
        normalized.setdefault("sameSite", "None")
        result = normalized
        return result
    finally:
        del cookie, normalized, result


def normalize_cookie_payload(payload: object) -> dict[str, Any] | CookieImportFailure:
    """Normalize supported JSON shapes to a cookie-only storage state."""
    cookies: list[Any] | None = None
    cookie: Any = None
    normalized: dict[str, Any] | CookieImportFailure | None = None
    normalized_rows: list[dict[str, Any]] | None = None
    result: dict[str, Any] | CookieImportFailure | None = None
    try:
        if isinstance(payload, dict) and isinstance(payload.get("cookies"), list):
            cookies = payload["cookies"]
        elif isinstance(payload, list):
            cookies = payload
        else:
            result = CookieImportFailure(
                code="INVALID_SHAPE",
                message=(
                    "Cookie JSON must be either a Playwright storage_state object "
                    "with a 'cookies' list or a bare list of cookie objects."
                ),
            )
            return result
        normalized_rows = []
        for cookie in cookies:
            normalized = _normalize_cookie_entry(cookie)
            if isinstance(normalized, CookieImportFailure):
                result = normalized
                return result
            normalized_rows.append(normalized)
        result = {"cookies": normalized_rows, "origins": []}
        return result
    finally:
        del payload, cookies, cookie, normalized, normalized_rows, result


def _nonempty_cookie_names(storage_state: dict[str, Any]) -> set[str]:
    return {
        cookie["name"]
        for cookie in storage_state.get("cookies", [])
        if isinstance(cookie, dict)
        and isinstance(cookie.get("name"), str)
        and isinstance(cookie.get("value"), str)
        and cookie["value"]
    }


def has_usable_secondary_binding(storage_state: dict[str, Any]) -> bool:
    """Apply canonical secondary-binding policy to non-empty cookie names."""
    names = None
    try:
        names = _nonempty_cookie_names(storage_state)
        return auth._has_valid_secondary_binding(names)
    finally:
        del storage_state, names


def import_cookie_payload(
    request: CookieImportRequest,
    *,
    filter_storage_state: StorageStateFilter,
    replace_profile_from_login: LoginWriter,
) -> CookieImportSuccess | CookieImportFailure:
    """Normalize, validate, filter, and persist a cookie payload."""
    payload = storage_path = include_domains = include_optional = None
    normalized_state = filtered_state = shaped_entries = raw_entry = raw_names = None
    empty_required = sanitized_state = cookie_names = secondary_present = None
    raw_cookie = sanitized = sanitized_rows = outcome = None
    result: CookieImportSuccess | CookieImportFailure | None = None
    try:
        payload = request.payload
        storage_path = request.storage_path
        include_domains = request.include_domains
        include_optional = request.include_optional
        normalized_state = normalize_cookie_payload(payload)
        if isinstance(normalized_state, CookieImportFailure):
            result = normalized_state
            return result

        filtered_state = filter_storage_state(
            normalized_state,
            include_optional=include_optional,
            include_domains=include_domains,
        )
        shaped_entries = []
        for raw_entry in filtered_state.get("cookies", []):
            try:
                shaped_entries.append(
                    auth._validate_cookie_shape(raw_entry, require_nonempty_value=False)
                )
            except ValueError:
                continue
        raw_names = {entry["name"] for entry in shaped_entries}
        empty_required = sorted(
            name
            for name in auth.MINIMUM_REQUIRED_COOKIES
            if any(entry["name"] == name and not entry["value"] for entry in shaped_entries)
        )
        if empty_required:
            result = CookieImportFailure(
                code="EMPTY_REQUIRED",
                message=(
                    "Required cookies must have non-empty string values: "
                    + ", ".join(empty_required)
                ),
            )
            return result

        sanitized_rows = []
        for raw_cookie in filtered_state.get("cookies", []):
            sanitized = auth._sanitize_cookie_entry(raw_cookie)
            if sanitized is not None:
                sanitized_rows.append(sanitized)
        sanitized_state = {"cookies": sanitized_rows, "origins": []}
        cookie_names = auth.cookie_names_from_storage(sanitized_state)
        try:
            auth.extract_cookies_from_storage(sanitized_state)
            auth._validate_routable_entries(
                auth._sanitized_auth_entries(sanitized_state),
                to_cookie=auth._storage_entry_to_cookie,
                require_routable=True,
            )
        except ValueError as exc:
            result = CookieImportFailure(
                code="INVALID_REQUIRED",
                message=f"{exc}\n\n{auth.missing_cookies_hint(cookie_names)}",
            )
            return result

        secondary_present = {"OSID", "APISID", "SAPISID", "LSID"} & raw_names
        if secondary_present and not has_usable_secondary_binding({"cookies": shaped_entries}):
            result = CookieImportFailure(
                code="INVALID_BINDING",
                message=(
                    "Secondary-binding cookies are present but do not form a usable "
                    "binding — either their values are empty or the set is incomplete "
                    "(need a non-empty OSID, or all of APISID, SAPISID and LSID). "
                    "Present: " + ", ".join(sorted(secondary_present))
                ),
            )
            return result

        outcome = replace_profile_from_login(
            storage_path,
            sanitized_state,
            include_domains=include_domains,
            include_optional=include_optional,
            account_mode="keep",
            backup=True,
        )
        if outcome.required_cookies_dropped:
            result = CookieImportFailure(
                code="REQUIRED_DROPPED",
                message=(
                    "Required authentication cookies were dropped by the write-time "
                    "cookie-domain policy: "
                    f"{', '.join(outcome.missing_required)}.\n\n"
                    f"{auth.missing_cookies_hint(set(outcome.present_names))}"
                ),
            )
            return result
        if outcome.lock_unavailable:
            result = CookieImportFailure(
                code="LOCK_UNAVAILABLE",
                message=(
                    f"Could not acquire the storage lock to write {storage_path} "
                    "(another process may hold it). Try again."
                ),
            )
            return result
        result = CookieImportSuccess(
            storage_state=sanitized_state,
            backup_path=outcome.backup_path,
        )
        return result
    finally:
        del request, payload, storage_path, include_domains, include_optional
        del normalized_state, filtered_state, shaped_entries, raw_entry, raw_names
        del empty_required, sanitized_state, cookie_names, secondary_present
        del raw_cookie, sanitized, sanitized_rows, outcome, result
        del filter_storage_state, replace_profile_from_login


def project_browser_account(
    account: Account, *, browser_profile: str | None, is_default: bool
) -> Account:
    """Rebuild an account through the invocation-time public constructor."""
    constructor = authuser = email = result = None
    try:
        constructor = auth.Account
        authuser = account.authuser
        email = account.email
        result = constructor(
            authuser=authuser,
            email=email,
            is_default=is_default,
            browser_profile=browser_profile,
        )
        return result
    finally:
        del account, constructor, authuser, email, browser_profile, is_default, result


def _browser_cookie_validation_failure(
    storage_state: dict[str, Any],
    validation_error: ValueError,
    *,
    browser_name: str,
) -> BrowserCookieProbeFailure:
    """Build the neutral validation projection used by current and legacy adapters."""
    cookie_names = hint = result = None
    try:
        cookie_names = auth.cookie_names_from_storage(storage_state)
        hint = auth.missing_cookies_hint(cookie_names, browser_label=browser_name)
        result = BrowserCookieProbeFailure(
            code="COOKIE_VALIDATION",
            detail=str(validation_error),
            cookie_names=frozenset(cookie_names),
            hint=hint,
        )
        return result
    finally:
        del storage_state, validation_error, browser_name, cookie_names, hint, result


def probe_browser_cookie_jar(
    request: BrowserCookieProbeRequest,
    *,
    run_probe: ProbeRunner,
    validate_with_recovery: ValidateWithRecovery,
) -> BrowserCookieProbeSuccess | BrowserCookieProbeFailure:
    """Validate and probe one browser cookie set."""
    raw_cookies = browser_name = browser_profile = quiet = validate_before_probe = None
    storage_state = validation_error = cookie_names = hint = None
    cookie_map = jar = probe_coroutine = accounts = account = projected = projected_accounts = None
    result: BrowserCookieProbeSuccess | BrowserCookieProbeFailure | None = None
    try:
        raw_cookies = request.raw_cookies
        browser_name = request.browser_name
        browser_profile = request.browser_profile
        quiet = request.quiet
        validate_before_probe = request.validate_before_probe

        if validate_before_probe:
            storage_state, validation_error = validate_with_recovery(raw_cookies)
            if validation_error is not None:
                cookie_names = auth.cookie_names_from_storage(storage_state)
                hint = auth.missing_cookies_hint(cookie_names, browser_label=browser_name)
                result = BrowserCookieProbeFailure(
                    code="COOKIE_VALIDATION",
                    detail=str(validation_error),
                    cookie_names=frozenset(cookie_names),
                    hint=hint,
                )
                return result
        else:
            storage_state = auth.convert_rookiepy_cookies_to_storage_state(raw_cookies)

        cookie_map = auth.extract_cookies_with_domains(
            storage_state, validate_required=bool(validate_before_probe)
        )
        jar = auth.build_cookie_jar(cookies=cookie_map)
        probe_coroutine = auth.enumerate_accounts(jar)
        try:
            try:
                accounts = run_probe(probe_coroutine)
            except BaseException:
                if inspect.iscoroutine(probe_coroutine) and (
                    inspect.getcoroutinestate(probe_coroutine) is inspect.CORO_CREATED
                ):
                    probe_coroutine.close()
                raise
        except ValueError as exc:
            result = BrowserCookieProbeFailure(code="STALE_COOKIES", detail=str(exc))
            return result
        except httpx.RequestError as exc:
            if quiet:
                raise
            result = BrowserCookieProbeFailure(code="NETWORK_ERROR", detail=str(exc))
            return result

        if not validate_before_probe:
            storage_state, validation_error = validate_with_recovery(raw_cookies)
            if validation_error is not None:
                cookie_names = auth.cookie_names_from_storage(storage_state)
                hint = auth.missing_cookies_hint(cookie_names, browser_label=browser_name)
                result = BrowserCookieProbeFailure(
                    code="COOKIE_VALIDATION",
                    detail=str(validation_error),
                    cookie_names=frozenset(cookie_names),
                    hint=hint,
                )
                return result

        if browser_profile is None:
            projected = tuple(accounts)
        else:
            projected_accounts = []
            for account in accounts:
                projected_accounts.append(
                    project_browser_account(
                        account,
                        browser_profile=browser_profile,
                        is_default=account.is_default,
                    )
                )
            projected = tuple(projected_accounts)
        result = BrowserCookieProbeSuccess(accounts=projected)
        return result
    finally:
        del request, raw_cookies, browser_name, browser_profile, quiet
        del validate_before_probe, storage_state, validation_error, cookie_names, hint
        del (
            cookie_map,
            jar,
            probe_coroutine,
            accounts,
            account,
            projected,
            projected_accounts,
            result,
        )
        del run_probe, validate_with_recovery
