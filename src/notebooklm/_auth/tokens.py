"""Authentication token container and storage loader."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeAlias, cast

import httpx

from .._deprecation import warn_registered_deprecation
from . import account as _auth_account
from . import cookies as _auth_cookies
from . import psidts_recovery as _auth_psidts_recovery
from . import refresh as _auth_refresh
from . import storage as _auth_storage
from .cookie_types import CookieJar
from .paths import resolve_auth_json_env
from .profile_account import ProfileAccount
from .profile_document import ProfileDocument
from .profile_migration import (
    InBandAccount,
    LegacyAccount,
    LegacyAccountMigrator,
    LegacyPromotionScheduler,
    NoAccount,
)
from .profile_store import CookieMergeDisposition, ProfileStore

logger = logging.getLogger("notebooklm.auth")

DomainCookieMap: TypeAlias = _auth_cookies.DomainCookieMap
FlatCookieMap: TypeAlias = _auth_cookies.FlatCookieMap
CookieSnapshot: TypeAlias = _auth_storage.CookieSnapshot


@dataclass(frozen=True, slots=True, repr=False)
class InlineAuthSource:
    document: ProfileDocument

    def __post_init__(self) -> None:
        if not isinstance(self.document, ProfileDocument):
            raise TypeError("document must be a ProfileDocument")
        object.__setattr__(self, "document", ProfileDocument.decode(self.document.to_json()))


@dataclass(frozen=True, slots=True, repr=False)
class FileAuthSource:
    store: ProfileStore
    profile: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.store, ProfileStore):
            raise TypeError("store must be a ProfileStore")
        if self.profile is not None and not isinstance(self.profile, str):
            raise TypeError("profile must be a string or None")


ResolvedAuthSource: TypeAlias = InlineAuthSource | FileAuthSource


@dataclass(frozen=True, slots=True, repr=False)
class SessionSeed:
    live: httpx.Cookies = field(repr=False)
    baseline: CookieJar = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.live, httpx.Cookies) or not isinstance(self.baseline, CookieJar):
            raise TypeError("session seed fields are invalid")
        object.__setattr__(self, "baseline", CookieJar(tuple(self.baseline)))


@dataclass(frozen=True, slots=True)
class LoadPolicy:
    allow_headless: bool = False
    # False is the Android NAME_ONLY contract: load stored cookie names and
    # permit the read-only homepage token acquisition needed to populate the
    # compatibility AuthTokens object, but never enter the Web recovery/poke
    # ladder or merge that acquisition's cookie observation back to disk.
    heal_psidts: bool = True

    def __post_init__(self) -> None:
        if type(self.allow_headless) is not bool:
            raise TypeError("allow_headless must be a boolean")
        if type(self.heal_psidts) is not bool:
            raise TypeError("heal_psidts must be a boolean")


@dataclass(frozen=True, slots=True, repr=False)
class TokenAcquisition:
    csrf_token: str = field(repr=False)
    session_id: str = field(repr=False)
    live: httpx.Cookies = field(repr=False)
    baseline_before_fetch_mutations: CookieJar = field(repr=False)
    final_account: ProfileAccount | None

    def __post_init__(self) -> None:
        valid = (
            isinstance(self.csrf_token, str)
            and isinstance(self.session_id, str)
            and isinstance(self.live, httpx.Cookies)
            and isinstance(self.baseline_before_fetch_mutations, CookieJar)
            and (self.final_account is None or isinstance(self.final_account, ProfileAccount))
        )
        if not valid:
            raise TypeError("token acquisition fields are invalid")
        object.__setattr__(
            self,
            "baseline_before_fetch_mutations",
            CookieJar(tuple(self.baseline_before_fetch_mutations)),
        )


@dataclass
class AuthTokens:
    """Authentication tokens for NotebookLM API.

    Attributes:
        cookies: Required Google auth cookies keyed by ``(name, domain, path)``
            per RFC 6265 §5.3 (issue #369). Legacy 2-tuple ``(name, domain)``
            and flat ``name -> value`` shapes are still accepted on
            construction and widened to the path-aware shape by
            :func:`normalize_cookie_map` during ``__post_init__``. This is a
            public compatibility/bootstrap shadow: the kernel copies it once
            during client composition and no first-party post-open decision
            reads it. Docs-only deprecated since v0.8.1 for removal in v1;
            runtime warnings would make synthesized dataclass operations noisy.
        csrf_token: CSRF token (SNlM0e) extracted from page
        session_id: Session ID (FdrFJe) extracted from page
        storage_path: Path to the storage_state.json file, if file-based auth was used
        cookie_jar: Domain-preserving public compatibility/bootstrap shadow.
            The kernel copies it once during client composition, then owns the
            sole mutable jar used for HTTP, routing, recovery, and persistence.
            Managed-client code must use the kernel jar, not this field.
            Docs-only deprecated since v0.8.1 for removal in v1.
        authuser: Google ``authuser`` index this profile authenticates as.
            ``0`` (the default account) is used when no in-band account
            metadata is present in ``storage_state.json``, matching
            pre-multi-account behavior. A pre-v0.5.0 profile's account
            metadata in the legacy sibling ``context.json`` is derived into
            in-band shape on load (and promoted in-band durably by a detached
            retryable single-flight) — see ``notebooklm._auth.storage`` — rather than read
            from here.
        account_email: Stable Google account identity for routing. When set,
            NotebookLM requests use it as the ``authuser`` value instead of the
            integer index, because Google account indices can change when other
            accounts sign out.
        cookie_snapshot: Internal save baseline used when a pre-client token
            fetch mutates cookies but persistence fails or CAS-rejects. This
            lets the eventual client retry the unpersisted delta instead
            of snapshotting the already-mutated jar as clean state.
    """

    # Secret fields are excluded from the dataclass-generated ``__repr__`` via
    # ``field(repr=False)`` and re-surfaced as redacted placeholders by the
    # custom ``__repr__`` below. This prevents accidental secret
    # leakage through ``logger.debug("%r", auth)``, ``pytest -vv`` failure
    # diffs, and any third-party tooling that calls ``repr()`` on the dataclass.
    cookies: DomainCookieMap = field(repr=False)
    csrf_token: str = field(repr=False)
    session_id: str = field(repr=False)
    storage_path: Path | None = None
    cookie_jar: httpx.Cookies | None = field(default=None, repr=False)
    authuser: int = 0
    cookie_snapshot: CookieSnapshot | None = field(default=None, repr=False)
    account_email: str | None = None
    _profile_session_generation: int = field(default=0, init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        """Normalize legacy flat cookie mappings into domain-keyed mappings.

        .. warning::
           Constructing ``AuthTokens(...)`` directly with a ``storage_path`` but
           no ``cookie_jar`` reaches ``build_cookie_jar`` →
           ``build_httpx_cookies_from_storage``, which performs a SYNCHRONOUS
           inline ``__Secure-1PSIDTS`` recovery POST + disk write. ``__post_init__``
           is sync and cannot offload, so doing this on a running event loop
           reintroduces the HIGH#2 freeze. Prefer
           ``NotebookLMClient.from_storage(...)``; pass a pre-built ``cookie_jar``
           when you must construct ``AuthTokens`` on the loop.
        """
        self.cookies = _auth_cookies.normalize_cookie_map(self.cookies)
        if self.cookie_jar is None:
            if self.storage_path is not None:
                warn_registered_deprecation("auth_tokens_sync_storage_construction")
            self.cookie_jar = _auth_cookies.build_cookie_jar(
                cookies=self.cookies,
                storage_path=self.storage_path,
            )

    def replace_cookie_jar(self, cookie_jar: httpx.Cookies) -> None:
        """Rebind both public compatibility shadows together.

        ``cookies`` and ``cookie_jar`` hold the same information in two shapes,
        initialized together at bootstrap. The kernel owns the live jar after
        composition; this method is the ADR-0032 Phase-A sync-back for public
        callers that still inspect the old fields. Rebinding only one shadow
        would expose two different sessions through the public object even
        though no first-party runtime decision consults either field.

        Every rebind goes through here so the two cannot diverge. Enforced by
        ``tests/_guardrails/test_authtokens_jar_sync.py``.
        """
        self.cookie_jar = cookie_jar
        self.cookies = _auth_cookies._cookie_map_from_jar(cookie_jar)

    def _replace_profile_session(
        self,
        *,
        target_cookie_jar: httpx.Cookies,
        source_cookie_jar: httpx.Cookies,
        expected_cookie_jar: CookieJar,
        expected_authuser: int,
        expected_account_email: str | None,
        expected_generation: int,
        authuser: int,
        account_email: str | None,
    ) -> bool | None:
        """Install one stored cookie/account generation without an await boundary.

        The kernel-owned target jar, the two public compatibility shadows, and
        the routing identity advance together. The caller holds the auth
        snapshot lock, so RPC snapshots cannot observe cookies from one stored
        generation with ``authuser``/``account_email`` from another.

        Returns:
            Whether the account route changed, or ``None`` when the live jar
            advanced after the disk sample and the stored generation was not
            installed.
        """
        if (
            CookieJar.from_httpx(target_cookie_jar) != expected_cookie_jar
            or (self.authuser, self.account_email) != (expected_authuser, expected_account_email)
            or self._profile_session_generation != expected_generation
        ):
            return None
        route_before = (self.authuser, self.account_email)
        _auth_cookies._replace_cookie_jar(target_cookie_jar, source_cookie_jar)
        self.replace_cookie_jar(target_cookie_jar)
        self.authuser = authuser
        self.account_email = account_email
        self._profile_session_generation += 1
        return route_before != (authuser, account_email)

    def __repr__(self) -> str:
        """Return a redacted representation safe for logs and pytest diffs.

        Cookie values, CSRF + session tokens, the live ``cookie_jar``, and the
        ``cookie_snapshot`` are all credential-equivalent and never appear
        verbatim. The cookie count is preserved so reprs remain useful for
        debugging (e.g. "expected 4 cookies, got 2"). Non-secret identity
        fields (``authuser``, ``account_email``, ``storage_path``) are kept
        for the same reason — they help identify *which* profile is involved
        without leaking *how to impersonate it*.
        """
        jar_summary = "<redacted>" if self.cookie_jar is not None else "None"
        snapshot_summary = "<redacted>" if self.cookie_snapshot is not None else "None"
        return (
            "AuthTokens("
            f"cookies=<{len(self.cookies)} redacted>, "
            "csrf_token=<redacted>, "
            "session_id=<redacted>, "
            f"storage_path={self.storage_path!r}, "
            f"cookie_jar={jar_summary}, "
            f"authuser={self.authuser!r}, "
            f"cookie_snapshot={snapshot_summary}, "
            f"account_email={self.account_email!r}"
            ")"
        )

    def cookie_header_for(self, url: str) -> str:
        """Return the ``Cookie:`` header this session would send to ``url``.

        .. deprecated:: 0.8.1
           Scheduled for removal in v1. Use managed-client request APIs, which
           select from the kernel-owned live jar.

        This is the domain-correct way to build a raw header. Cookie selection
        follows RFC 6265 §5.4 via :attr:`cookie_jar`, so a cookie scoped to one
        host is not sent to another — unlike :attr:`cookie_header`, which
        collapses every domain into one name→value slot and therefore has to
        pick an arbitrary winner when the same name exists on two hosts
        (issue #2054).

        ``url`` is required and deliberately has no default: a default would
        reintroduce the fabricated target that this method exists to remove.

        .. important::
           Routing is only as good as the jar. An ``AuthTokens`` built from a
           bare ``name -> value`` mapping has no domains to preserve, so
           ``__post_init__`` widens every entry to ``.google.com`` and this
           method returns the **same broadcast header for every host** — the
           behaviour it exists to replace, without an error to say so. Build
           from ``storage_state`` (``AuthTokens.from_storage``, or pass
           ``storage_path``) to get real per-host selection.

        Args:
            url: Absolute URL the header is being built for. Must be ``https``:
                selection is ``Secure``-aware, so an ``http`` URL either drops
                the ``Secure`` cookies without saying so or — for a jar built
                from a bare cookie map, where nothing is marked ``Secure`` —
                hands back credentials for a cleartext request. Both are wrong
                for auth, so the scheme is rejected instead.

        Returns:
            Semicolon-separated ``name=value`` pairs, or ``""`` when no stored
            cookie matches ``url``.

        Raises:
            ValueError: If ``url`` is not an absolute ``https`` URL with a host,
                or if this session has no cookie jar. Every failure here is
                raised rather than returned as ``""``: an empty header is
                indistinguishable from "no cookie matched", which is a legitimate
                answer, so a silent empty string would hide a malformed URL
                (``https:/typo`` parses fine and matches nothing) behind a
                plausible-looking result.

        .. note::
           Not a pure query: ``http.cookiejar.add_cookie_header`` ends by calling
           ``clear_expired_cookies()``, so reading a header can prune expired
           entries from :attr:`cookie_jar`. Harmless today — pruning changes what
           the jar *holds*, never what a request *sends*, and Playwright writes
           session cookies as ``expires: -1`` (mapped to ``None``, never expired),
           so the eligible population is normally empty.

           **Revisit if this method ever gains a caller inside**
           ``AuthTokens.from_storage``: between ``snapshot_cookie_jar`` and
           ``save_cookies_to_storage`` a prune would be persisted to disk as a
           deletion. Correct in itself, but it would make a read-shaped call a
           write.
        """
        parsed = httpx.URL(url)
        if parsed.scheme != "https":
            raise ValueError(
                f"cookie_header_for() requires an https URL (got {parsed.scheme!r}). "
                "Auth cookies are Secure-scoped and must not be built for cleartext."
            )
        if not parsed.host:
            raise ValueError(
                f"cookie_header_for() requires an absolute URL with a host (got {url!r}). "
                "A hostless URL matches no cookie and would return an empty header."
            )
        if self.cookie_jar is None:
            raise ValueError(
                "cookie_header_for() requires a cookie jar; this AuthTokens has none. "
                "Cookie selection is per-host, and the flat `cookies` mapping cannot "
                "answer a per-host question."
            )
        request = httpx.Request("GET", parsed)
        self.cookie_jar.set_cookie_header(request)
        return request.headers.get("cookie", "")

    @property
    def cookie_header(self) -> str:
        """Generate a domain-blind Cookie header value.

        .. deprecated:: 0.8.1
           Scheduled for removal in v1. Use managed-client request APIs. This
           property remains warning-free throughout v0.x.

        .. warning::
           **Not correct for building a request.** This is :attr:`flat_cookies`
           joined into header syntax, so it inherits that projection's one slot
           per cookie name: when the same name exists on more than one domain —
           ``OSID`` on both the app host and ``accounts.google.com``, for
           instance — all but one value is discarded, arbitrarily (issue
           #2054). Use :meth:`cookie_header_for` instead, which selects cookies
           per RFC 6265 for a specific URL.

        Returns:
            Semicolon-separated cookie string (e.g., "SID=abc; HSID=def").
        """
        # Keep this separate compatibility property warning-free. Calling the
        # public ``flat_cookies`` property here would attribute its warning to
        # library internals and make a distinct deprecated surface warn
        # indirectly.
        return "; ".join(f"{k}={v}" for k, v in self._flat_cookie_projection().items())

    @property
    def account_route(self) -> str:
        """Return the value to send in NotebookLM ``authuser`` routing fields."""
        return _auth_account.format_authuser_value(self.authuser, self.account_email)

    @property
    def jar(self) -> CookieJar:
        """Return a typed, read-only view of the compatibility cookie shadow.

        .. deprecated:: 0.8.1
           This warning-free v0.x migration shape becomes the immutable
           ``initial_cookies: CookieJar`` bootstrap field in v1.

        This preserves the public Phase-A projection and the future
        ``initial_cookies`` migration shape. It is projected from
        ``cookie_jar`` each access, but that field is itself a compatibility
        shadow; managed-client code asks the kernel-owned jar instead. This is
        a **read-only question view** (``names`` / ``validate_required`` /
        ``has_secondary_binding`` / ``missing_hint``), never a live authority.

        ``same_site``-lossy by construction (:meth:`CookieJar.from_httpx`), so it
        must never be persisted — the SameSite the wire cookies carry is not
        recoverable from ``http.cookiejar``. This is why ``jar`` answers
        questions rather than replacing ``cookie_jar``.
        """
        return CookieJar.from_httpx(self.cookie_jar) if self.cookie_jar is not None else CookieJar()

    @property
    def flat_cookies(self) -> FlatCookieMap:
        """Return a legacy name→value cookie mapping.

        .. deprecated:: 0.8.1
           Scheduled for removal in v1. Direct access emits one
           :class:`DeprecationWarning`; use :attr:`jar` for bootstrap-cookie
           questions and managed-client request APIs for HTTP.

        .. warning::
           **Lossy, and not correct for building a request.** One slot per
           cookie name means duplicates across domains are discarded. Ranking
           is by :func:`notebooklm._auth.cookie_policy._auth_domain_priority`,
           whose named tiers are **not** all distinct — ``.notebooklm.google.com`` and
           ``.notebook.google.com`` share a tier, as do their bare variants,
           and tiers 0 and 1 hold many domains each. Within a tier the first
           entry in iteration order wins, so the survivor is arbitrary and
           changes if ``storage_state`` is reordered (issue #2054).

           Kept for backward compatibility — see the migration note in the
           v0.4.0 CHANGELOG entry that recommended it. Managed-client request
           and persistence paths never consume this projection.
        """
        warn_registered_deprecation("auth_tokens_flat_cookies")
        return self._flat_cookie_projection()

    def _flat_cookie_projection(self) -> FlatCookieMap:
        """Build the legacy flat view without emitting a public warning."""
        return _auth_cookies.flatten_cookie_map(self.cookies)

    @classmethod
    async def from_storage(
        cls,
        path: Path | None = None,
        profile: str | None = None,
        *,
        allow_headless: bool = False,
    ) -> AuthTokens:
        """Create AuthTokens from Playwright storage state file.

        Compatibility loader for callers that still need a standalone token object.
        Prefer ``NotebookLMClient.from_storage(...)`` and access ``client.auth``
        within the managed client lifecycle.

        Args:
            path: Path to storage_state.json. If provided, takes precedence over profile.
            profile: Profile name to load auth from (e.g., "work", "personal").
                If None, uses the active profile (from CLI flag, env var, or config).
            allow_headless: Permit layer-3 browser recovery if stored cookies
                redirect to Google sign-in. Layer 4 remains automatic when a
                sibling ``master_token.json`` is present.

        Returns:
            Fully initialized AuthTokens ready for API calls.

        Raises:
            FileNotFoundError: If storage file doesn't exist
            ValueError: If required cookies are missing or tokens can't be extracted
            httpx.HTTPError: If token fetch request fails

        Example:
            async with NotebookLMClient.from_storage() as client:
                notebooks = await client.list_notebooks()

            # Load from a specific profile
            async with NotebookLMClient.from_storage(profile="work") as client:
                notebooks = await client.list_notebooks()
        """
        warn_registered_deprecation("auth_tokens_from_storage")
        loaded = await _load_stored_auth(
            path=path,
            profile=profile,
            policy=LoadPolicy(allow_headless=allow_headless),
            auth_type=cls,
        )
        return loaded.auth


@dataclass(frozen=True, slots=True, repr=False)
class InlineLoadedAuth:
    auth: AuthTokens

    def __post_init__(self) -> None:
        if not isinstance(self.auth, AuthTokens):
            raise TypeError("auth must be an AuthTokens")


@dataclass(frozen=True, slots=True, repr=False)
class FileLoadedAuth:
    auth: AuthTokens
    store: ProfileStore
    persistence_baseline: CookieJar = field(repr=False)

    def __post_init__(self) -> None:
        if (
            not isinstance(self.auth, AuthTokens)
            or not isinstance(self.store, ProfileStore)
            or not isinstance(self.persistence_baseline, CookieJar)
        ):
            raise TypeError("file loaded auth fields are invalid")
        object.__setattr__(
            self,
            "persistence_baseline",
            CookieJar(tuple(self.persistence_baseline)),
        )


LoadedAuth: TypeAlias = InlineLoadedAuth | FileLoadedAuth


class SessionSeedLoader:
    """Load a source into paired live and persistence-baseline forms."""

    async def load(self, source: ResolvedAuthSource, policy: LoadPolicy) -> SessionSeed:
        if not isinstance(source, InlineAuthSource | FileAuthSource):
            raise TypeError("source must be a resolved auth source")
        if not isinstance(policy, LoadPolicy):
            raise TypeError("policy must be a LoadPolicy")

        def load_sync() -> _auth_cookies._LoadedCookiePair:
            heal_policy = (
                _auth_psidts_recovery.HealPolicy.HEAL_THEN_NAME_ONLY
                if policy.heal_psidts
                else _auth_psidts_recovery.HealPolicy.NAME_ONLY
            )
            if isinstance(source, FileAuthSource):
                if heal_policy is _auth_psidts_recovery.HealPolicy.NAME_ONLY:
                    return _auth_cookies._load_cookie_pair_pure(
                        source.store.path,
                        require_routable=False,
                    )
                return _auth_psidts_recovery.load_with_recovery(
                    source.store.path,
                    heal_policy,
                    load=_auth_cookies._load_cookie_pair_pure,
                )

            def load_captured(
                _path: Path | None,
                *,
                require_routable: bool,
            ) -> _auth_cookies._LoadedCookiePair:
                return _auth_cookies._build_cookie_pair_from_storage_state(
                    source.document.to_json(),
                    require_routable=require_routable,
                )

            def decline_inline(_path: Path | None) -> bool:
                logger.debug(
                    "PSIDTS recovery skipped: env-var auth (NOTEBOOKLM_AUTH_JSON) "
                    "has no writeable backing store"
                )
                return False

            if heal_policy is _auth_psidts_recovery.HealPolicy.NAME_ONLY:
                return load_captured(None, require_routable=False)
            return _auth_psidts_recovery.load_with_recovery(
                None,
                heal_policy,
                load=load_captured,
                heal=decline_inline,
            )

        pair = await asyncio.to_thread(load_sync)
        return SessionSeed(pair.live, pair.baseline)


def _account_from_compatibility(raw: Mapping[str, object]) -> ProfileAccount:
    raw_authuser = raw.get("authuser")
    authuser = raw_authuser if isinstance(raw_authuser, int) and raw_authuser >= 0 else 0
    raw_email = raw.get("email")
    email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
    return ProfileAccount(authuser=authuser, email=email)


class AccountRouteResolver:
    """Resolve one paired account route at each token-attempt boundary."""

    def __init__(
        self,
        source: ResolvedAuthSource,
        *,
        migrator: LegacyAccountMigrator,
        promotions: LegacyPromotionScheduler,
    ) -> None:
        if not isinstance(source, InlineAuthSource | FileAuthSource):
            raise TypeError("source must be a resolved auth source")
        if not isinstance(migrator, LegacyAccountMigrator) or not isinstance(
            promotions, LegacyPromotionScheduler
        ):
            raise TypeError("account route collaborators are invalid")
        self._source = source
        self._migrator = migrator
        self._promotions = promotions

    async def resolve(self) -> ProfileAccount | None:
        source = self._source
        if isinstance(source, InlineAuthSource):
            payload = source.document.to_json()
            namespace = payload.get("notebooklm")
            raw = namespace.get("account") if isinstance(namespace, Mapping) else None
            return _account_from_compatibility(raw) if isinstance(raw, Mapping) and raw else None

        def resolve_file_account():
            resolved, compatibility = self._migrator._resolve_with_projection(source.store)
            return (
                resolved,
                compatibility,
                self._migrator.needs_reconciliation(source.store.path, resolved),
            )

        resolved, compatibility, reconcile = await asyncio.to_thread(resolve_file_account)
        if reconcile:
            self._promotions.schedule(source.store, self._migrator)
        if isinstance(resolved, InBandAccount | LegacyAccount):
            return _account_from_compatibility(compatibility)
        if not isinstance(resolved, NoAccount):  # pragma: no cover - closed migration result
            raise AssertionError("unknown account migration result")
        return None


class TokenAcquirer(Protocol):
    async def acquire(
        self,
        seed: SessionSeed,
        *,
        source: ResolvedAuthSource,
        route: AccountRouteResolver,
        policy: LoadPolicy,
    ) -> TokenAcquisition: ...


class _ProductionTokenAcquirer:
    async def acquire(
        self,
        seed: SessionSeed,
        *,
        source: ResolvedAuthSource,
        route: AccountRouteResolver,
        policy: LoadPolicy,
    ) -> TokenAcquisition:
        final_account: ProfileAccount | None = None

        async def resolve_route(_path: Path | None) -> dict[str, Any]:
            nonlocal final_account
            final_account = await route.resolve()
            kwargs: dict[str, Any] = {
                "authuser": 0 if final_account is None else final_account.authuser
            }
            if final_account is not None and final_account.email is not None:
                kwargs["account_email"] = final_account.email
            return kwargs

        storage_path = source.store.path if isinstance(source, FileAuthSource) else None
        profile = source.profile if isinstance(source, FileAuthSource) else None
        if not policy.heal_psidts:
            # Android only needs its master-token/bearer route.  Keep the
            # historical AuthTokens bootstrap available for the deprecated
            # lazy Web sidecar, but make that bootstrap strictly read-only:
            # no PSIDTS poke, refresh command, headless/master-token cookie
            # recovery, or cookie-store merge.  Set-Cookie observations remain
            # in ``seed.live`` and become persistable only if the Web sidecar is
            # later materialised and assumes lifecycle ownership.
            csrf, session_id = await _auth_refresh._fetch_tokens_with_jar(
                seed.live,
                storage_path,
                poke=False,
                **await resolve_route(storage_path),
            )
            return TokenAcquisition(
                csrf,
                session_id,
                seed.live,
                seed.baseline,
                final_account,
            )
        csrf, session_id, baseline = await _auth_refresh._fetch_tokens_with_exact_baseline(
            seed.live,
            storage_path,
            profile,
            initial_baseline=seed.baseline,
            resolve_route=resolve_route,
            allow_headless=policy.allow_headless,
            env_auth=isinstance(source, InlineAuthSource),
        )
        return TokenAcquisition(
            csrf,
            session_id,
            seed.live,
            baseline,
            final_account,
        )


def _typed_baseline_snapshot(baseline: CookieJar) -> CookieSnapshot:
    """Project the exact typed baseline one way into the v0.x snapshot shape."""
    return {
        _auth_storage.CookieSnapshotKey(cookie.name, cookie.domain, cookie.path): (
            _auth_storage.CookieSnapshotValue(
                cookie.value,
                cast(int | None, cookie.expires),
                cookie.secure,
                cookie.http_only,
            )
        )
        for cookie in baseline
    }


class StoredAuthLoader:
    """Canonical application service for stored authentication."""

    def __init__(
        self,
        *,
        seeds: SessionSeedLoader,
        token_acquirer: TokenAcquirer,
        migrator: LegacyAccountMigrator,
        promotions: LegacyPromotionScheduler,
    ) -> None:
        if not isinstance(seeds, SessionSeedLoader):
            raise TypeError("seeds must be a SessionSeedLoader")
        if not isinstance(migrator, LegacyAccountMigrator) or not isinstance(
            promotions, LegacyPromotionScheduler
        ):
            raise TypeError("stored auth collaborators are invalid")
        self._seeds = seeds
        self._token_acquirer = token_acquirer
        self._migrator = migrator
        self._promotions = promotions

    async def load(
        self,
        *,
        path: Path | None,
        profile: str | None,
        policy: LoadPolicy,
        auth_type: type[AuthTokens],
    ) -> LoadedAuth:
        if (
            not isinstance(policy, LoadPolicy)
            or not isinstance(auth_type, type)
            or not issubclass(auth_type, AuthTokens)
        ):
            raise TypeError("stored auth load arguments are invalid")
        source = await self._resolve_source(path, profile)
        seed = await self._seeds.load(source, policy)
        route = AccountRouteResolver(
            source,
            migrator=self._migrator,
            promotions=self._promotions,
        )
        acquired = await self._token_acquirer.acquire(
            seed,
            source=source,
            route=route,
            policy=policy,
        )
        selected_baseline = acquired.baseline_before_fetch_mutations
        if isinstance(source, FileAuthSource) and policy.heal_psidts:
            store: ProfileStore = source.store
            observation = CookieJar.from_httpx(acquired.live)
            merge = await asyncio.to_thread(
                self._merge_observation,
                store,
                observation,
                selected_baseline,
            )
            if merge.disposition is not CookieMergeDisposition.HARD_FAILURE:
                if merge.next_baseline is None:  # pragma: no cover - result invariant
                    raise AssertionError("advancing merge must return a baseline")
                selected_baseline = merge.next_baseline

        account = acquired.final_account
        auth = auth_type(
            cookies=_auth_cookies._cookie_map_from_jar(acquired.live),
            csrf_token=acquired.csrf_token,
            session_id=acquired.session_id,
            storage_path=source.store.path if isinstance(source, FileAuthSource) else None,
            cookie_jar=acquired.live,
            authuser=0 if account is None else account.authuser,
            cookie_snapshot=_typed_baseline_snapshot(selected_baseline),
            account_email=None if account is None else account.email,
        )
        if isinstance(source, InlineAuthSource):
            logger.debug("Skipping cookie sync: Auth loaded from NOTEBOOKLM_AUTH_JSON env var")
            return InlineLoadedAuth(auth)
        return FileLoadedAuth(auth, source.store, selected_baseline)

    @staticmethod
    def _merge_observation(
        store: ProfileStore,
        observation: CookieJar,
        baseline: CookieJar,
    ):
        return store.merge_cookie_observation(observation, baseline=baseline)

    @staticmethod
    async def _resolve_source(path: Path | None, profile: str | None) -> ResolvedAuthSource:
        if path is not None:
            return FileAuthSource(ProfileStore(path), profile)
        env_auth_json = resolve_auth_json_env()
        if env_auth_json is not None:
            state = await asyncio.to_thread(
                _auth_cookies._load_storage_state_from_env_value,
                env_auth_json,
            )
            return InlineAuthSource(ProfileDocument.decode(state))
        from ..paths import get_storage_path

        return FileAuthSource(ProfileStore(get_storage_path(profile=profile)), profile)


async def _load_stored_auth(
    *,
    path: Path | None,
    profile: str | None,
    policy: LoadPolicy,
    auth_type: type[AuthTokens],
) -> LoadedAuth:
    """Build the concrete collaborators and run the canonical stored-auth load."""
    migrator = LegacyAccountMigrator()
    loader = StoredAuthLoader(
        seeds=SessionSeedLoader(),
        token_acquirer=_ProductionTokenAcquirer(),
        migrator=migrator,
        promotions=LegacyPromotionScheduler.process_default(),
    )
    return await loader.load(
        path=path,
        profile=profile,
        policy=policy,
        auth_type=auth_type,
    )


AuthTokens.__module__ = "notebooklm.auth"


def load_auth_from_storage(path: Path | None = None) -> dict[str, str]:
    """Load Google cookies from storage as a flat name→value dict.

    Loads authentication cookies with the following precedence:
    1. Explicit path argument (from --storage CLI flag)
    2. NOTEBOOKLM_AUTH_JSON environment variable (inline JSON, no file needed)
    3. Profile storage path from :func:`notebooklm.paths.get_storage_path`
       (``$NOTEBOOKLM_HOME/profiles/<profile>/storage_state.json`` with legacy
       home-root fallback for the default profile)

    Duplicate-name resolution follows
    :func:`notebooklm._auth.cookie_policy._auth_domain_priority`, matching
    :attr:`AuthTokens.flat_cookies` for the same storage state — previously the
    two paths disagreed on names that live only on non-base hosts (e.g.
    ``OSID`` on ``myaccount.google.com`` vs ``notebooklm.google.com``). See
    issue #375.

    Args:
        path: Path to storage_state.json. If provided, takes precedence over env vars.

    Returns:
        Dict mapping cookie names to values (e.g., {"SID": "...", "HSID": "..."}).

    Raises:
        FileNotFoundError: If storage file doesn't exist (when using file-based auth).
        ValueError: If required cookies (``SID`` + ``__Secure-1PSIDTS``) are
            missing, or if storage JSON is malformed.

    Example::

        # CLI flag takes precedence
        cookies = load_auth_from_storage(Path("/custom/path.json"))

        # Or use NOTEBOOKLM_AUTH_JSON for CI/CD (no file writes needed)
        # export NOTEBOOKLM_AUTH_JSON='{"cookies":[...]}'
        cookies = load_auth_from_storage()
    """
    # Inline ``__Secure-1PSIDTS`` recovery (issue #865). Playwright login can
    # land a ``storage_state.json`` that carries SID + secondary binding but
    # lacks PSIDTS, because Google only mints PSIDTS deterministically in
    # response to the dedicated ``RotateCookies`` POST — not on the passive
    # ``goto()`` navigations the login flow uses. The preflight then rejects
    # before the keepalive's RotateCookies path can heal the state.
    #
    # The sequence (strict load -> heal -> name-only retry) has ONE owner,
    # :func:`notebooklm._auth.psidts_recovery.load_with_recovery`; this wrapper
    # supplies the flat-map loader and keeps its own name, signature and patch
    # seam (ADR-0017). It used to be a second copy of that control flow, and by
    # #2154 the copy had decayed: both arms of its ``if not recovered:`` returned
    # the identical name-only call, distinguishable only by their comments.
    #
    # The recovery invocation stays in a WRAPPER body — the network-free
    # :func:`_load_auth_cookies_pure` never triggers it (issue #2061 /
    # event-loop-blocking fix). Sync callers (CLI) keep this inline recovery; an
    # async caller must offload the wrapper via ``asyncio.to_thread``.
    return _auth_psidts_recovery.load_with_recovery(
        path,
        _auth_psidts_recovery.HealPolicy.HEAL_THEN_NAME_ONLY,
        load=_load_auth_cookies_pure,
    )


def _load_auth_cookies_pure(
    path: Path | None = None, *, require_routable: bool = True
) -> dict[str, str]:
    """PURE flat-cookie loader: file I/O + validation ONLY — never any network.

    Network-free half of :func:`load_auth_from_storage`: reads the storage
    state, extracts the flat ``name -> value`` map, and runs the required-cookie
    + RFC 6265 routing preflight. On failure it raises
    :class:`~notebooklm._auth.cookie_policy.RequiredCookieValidationError` with a
    closed-enum ``reason`` and STOPS — it never fires the inline
    ``RotateCookies`` recovery POST. Composing recovery on top of the typed
    reason is the wrapper's job (mirrors
    :func:`notebooklm._auth.cookies._load_cookies_pure`).

    ``require_routable`` toggles the RFC 6265 routing preflight; pass it ``True``
    only where a recovery attempt follows.
    """
    storage_state = _auth_cookies._load_storage_state(path)
    cookies = _auth_cookies.extract_cookies_from_storage(storage_state)
    entries = _auth_cookies._sanitized_auth_entries(storage_state)
    _auth_cookies._validate_routable_entries(
        entries,
        to_cookie=_auth_cookies._storage_entry_to_cookie,
        require_routable=require_routable,
    )
    return cookies


__all__ = ["AuthTokens", "load_auth_from_storage"]
