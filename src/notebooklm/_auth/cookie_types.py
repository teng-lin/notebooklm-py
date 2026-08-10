"""The canonical cookie types: :class:`Cookie` and :class:`CookieJar`.

ADR-0031 Stage 1. A cookie currently exists in the auth layer in six shapes —
the raw Playwright storage-state row, the rookiepy row, ``DomainCookieMap``
(``(name, domain, path) -> value``), ``FlatCookieMap`` (``name -> value``),
the legacy 2-tuple ``LegacyDomainCookieMap``, and the "sanitized entries"
list — and the conversions between them are free functions scattered across
:mod:`notebooklm._auth.cookies` and :mod:`notebooklm._auth.cookie_policy`.
Because no type owns "a set of auth cookies", the questions callers ask about
one ("which names are here?", "is this set usable?", "does it still bind?")
have no method to be, so every flow reaches independently for the same
scattered private helpers. That fan-out is what makes the auth layer hard to
move: see ADR-0031's Context.

This module introduces the types those questions belong to. It completes the
``cookie_*`` family: :mod:`~notebooklm._auth.cookie_semantics` owns row
validation/normalization, :mod:`~notebooklm._auth.cookie_policy` owns which
cookies are required and allowed, and this module owns the shape callers
hold. (Not named ``cookie_jar`` — ``cli/services/login/cookie_jar.py`` is a
different thing: the CLI's browser-account enumeration service.) Codec work
points down to :mod:`~notebooklm._auth.cookie_semantics`; compatibility free
functions adapt over the same leaf rather than being imported back into this
value module. ``AuthTokens`` keeps its ``cookies`` / ``cookie_jar`` field pair
until ADR-0031 Stage 4.

Round-tripping is lossy in one direction, by design:
:meth:`CookieJar.from_storage_state` applies the same allowlist and pure
row-sanitization filter the rest of the auth layer applies, so a jar built
from a storage state holds only routable, structurally valid auth rows.
:meth:`CookieJar.to_storage_state` therefore reproduces *that filtered view*,
not the original file. Callers persisting cookies must keep using the
canonical storage writer (ADR-0029), which owns merge semantics this type
deliberately does not.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any, NamedTuple, cast

import httpx

from . import cookie_policy as _cookie_policy
from . import cookie_semantics as _cookie_semantics


class CookieIdentity(NamedTuple):
    """RFC 6265 §5.3 cookie identity with a root-path default."""

    name: str
    domain: str
    path: str = "/"


@dataclass(frozen=True, slots=True, eq=False)
class Cookie:
    """One auth cookie, keyed by its RFC 6265 §5.3 identity.

    ``(name, domain, path)`` is the identity: two cookies sharing a name at
    distinct domains or paths are independent entries, never one shadowing the
    other (issue #369). ``expires`` is the Playwright epoch-seconds convention
    where ``-1`` means a session cookie.

    ``same_site`` is carried rather than defaulted: dropping a stored value is
    the exact regression ``storage._preserved_same_site`` exists to prevent, so
    ``None`` here means "the row carried none", never "assume ``None``".
    """

    name: str
    domain: str
    path: str
    value: str = field(repr=False)
    expires: float | int | None = None
    http_only: bool = False
    secure: bool = False
    same_site: str | None = field(default=None, compare=False, repr=False)

    @property
    def identity(self) -> tuple[str, str, str]:
        """The RFC 6265 §5.3 ``(name, domain, path)`` identity."""
        return (self.name, self.domain, self.path)

    @property
    def key(self) -> CookieIdentity:
        """Typed form of :attr:`identity` for new value-oriented code."""
        return CookieIdentity(self.name, self.domain, self.path)

    def _compared_fields(self) -> tuple[Any, ...]:
        return (
            self.name,
            self.domain,
            self.path,
            self.value,
            self.expires,
            self.http_only,
            self.secure,
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Cookie):
            return NotImplemented
        return self._compared_fields() == other._compared_fields()

    def __hash__(self) -> int:
        return hash(self._compared_fields())


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class CookieJar:
    """An ordered, immutable sequence of auth cookies.

    Construct through the ``from_*`` classmethods rather than ``__init__`` —
    each one routes through the conversion the auth layer already trusts for
    that input shape, so a jar can never hold rows the rest of the layer would
    have filtered out.
    """

    _cookies: tuple[Cookie, ...] = field(default=(), repr=False)

    def __init__(self, cookies: Iterable[Cookie] = ()) -> None:
        object.__setattr__(self, "_cookies", tuple(cookies))

    # -- constructors ------------------------------------------------------

    @classmethod
    def from_storage_state(cls, storage_state: Mapping[str, Any]) -> CookieJar:
        """Build from a parsed Playwright ``storage_state`` mapping.

        Rows pass the shared pure sanitizer and domain policy, so malformed
        rows and non-auth domains are dropped exactly as in compatibility
        readers. Source order and duplicate rows are retained.
        """
        return cls(_cookie_from_entry(entry) for entry in _sanitized_auth_entries(storage_state))

    @classmethod
    def from_rookiepy(cls, rows: list[dict[str, Any]]) -> CookieJar:
        """Build from rookiepy's browser-extraction rows.

        Uses the dependency-bottom snake_case → camelCase adapter and session
        expiry convention shared by the compatibility free function.
        """
        converted: list[Cookie] = []
        for row in rows:
            try:
                normalized = _cookie_semantics.sanitize_cookie_entry(row)
            except _cookie_semantics.CookieRowError:
                continue
            if not _cookie_policy._is_allowed_auth_domain(normalized["domain"]):
                continue
            storage_row = _cookie_semantics.rookiepy_row_to_storage_row(normalized)
            converted.append(
                _cookie_from_entry(_cookie_semantics.sanitize_cookie_entry(storage_row))
            )
        return cls(converted)

    @classmethod
    def from_domain_map(cls, cookies: Any) -> CookieJar:
        """Build from any legacy cookie-map shape.

        Accepts path-aware three-tuple keys, legacy two-tuple keys, and flat
        ``name -> value``. The map shapes carry no expiry/flags, so those default.
        """
        normalized = _cookie_semantics.normalize_legacy_cookie_map(cookies)
        return cls(
            tuple(
                Cookie(name=name, domain=domain, path=path, value=value)
                for (name, domain, path), value in normalized.items()
            )
        )

    @classmethod
    def from_httpx(cls, jar: httpx.Cookies) -> CookieJar:
        """Snapshot a live ``httpx.Cookies`` jar into an immutable jar.

        Preserves ``expires`` / ``secure`` / ``http_only`` straight off the
        underlying ``http.cookiejar.Cookie`` objects — only ``same_site`` is
        lost, and unavoidably so.

        Applies the same allowed-auth-domain + non-``None`` value filter as
        :func:`notebooklm._auth.cookies._cookie_map_from_jar` (the contract
        that builds ``AuthTokens.cookies``), so this constructor cannot hold a
        row the rest of the layer would have filtered out — a non-auth-domain
        cookie named ``SID``/``OSID`` must not let ``.jar`` report a binding the
        filtered ``cookies`` view correctly excludes.

        .. warning::
           **``same_site``-lossy — never for persistence or a save baseline.**
           ``http.cookiejar.Cookie`` cannot carry a SameSite attribute, so every
           ``Cookie`` produced here has ``same_site=None`` regardless of the
           cookie's actual SameSite. It is therefore valid as a live observation
           for pure merge decisions, whose dirtiness policy deliberately excludes
           SameSite. Round-tripping this jar through :meth:`to_storage_state`
           would drop ``sameSite`` — the exact #2150 downgrade — so never use it
           as a durable save baseline or standalone document serializer.
        """
        return cls(
            tuple(
                Cookie(
                    name=c.name,
                    domain=c.domain,
                    path=c.path or "/",
                    value=c.value,
                    expires=c.expires,
                    secure=bool(c.secure),
                    http_only=_cookie_semantics.cookie_is_http_only(c),
                )
                for c in jar.jar
                if c.name
                and c.domain
                and c.value is not None
                and _cookie_policy._is_allowed_auth_domain(c.domain)
            )
        )

    @classmethod
    def from_live_httpx_for_merge(cls, jar: httpx.Cookies, *, include_none: bool) -> CookieJar:
        """Project a live jar for persistence without applying domain policy."""
        return cls(
            Cookie(
                name=cookie.name,
                domain=cookie.domain,
                path=cookie.path or "/",
                value=cast(str, cookie.value),
                expires=cookie.expires,
                secure=cast(bool, cookie.secure),
                http_only=_cookie_semantics.cookie_is_http_only(cookie),
                same_site=None,
            )
            for cookie in jar.jar
            if cookie.name and cookie.domain and (include_none or cookie.value is not None)
        )

    # -- converters --------------------------------------------------------

    def domain_map_first_wins(self) -> dict[tuple[str, str, str], str]:
        """Project to a path-aware map with an explicit first-wins rule.

        First occurrence wins for a repeated identity, matching
        :func:`notebooklm._auth.cookies.extract_cookies_with_domains`.
        """
        result: dict[tuple[str, str, str], str] = {}
        for cookie in self._cookies:
            result.setdefault(cookie.identity, cookie.value)
        return result

    def to_domain_map(self) -> dict[tuple[str, str, str], str]:
        """Compatibility delegate to :meth:`domain_map_first_wins`."""
        return self.domain_map_first_wins()

    # NOTE: there is deliberately no ``to_flat_map`` / ``FlatCookieMap`` export.
    # Flattening to ``name -> value`` collapses the path component (#369) and
    # picks an arbitrary winner among same-tier domains — ``AuthTokens.flat_cookies``
    # documents itself as "lossy, and not correct for building a request", with a
    # survivor that changes when ``storage_state`` is reordered (#2054). Every
    # remaining caller of ``flatten_cookie_map`` is back-compat: the public
    # ``flat_cookies`` property and ``_update_cookie_input``'s write-back into a
    # legacy-shaped caller dict. Giving the canonical type a method for it would
    # carry that footgun into the model meant to retire it, so the legacy shape
    # stays reachable only through the legacy free function. Callers that want
    # bytes on the wire use :meth:`to_httpx`, which is path- and domain-correct.

    def to_httpx(self) -> httpx.Cookies:
        """Return a faithful domain- and attribute-preserving live jar.

        Rows are inserted directly into the underlying stdlib jar. Exact
        duplicate identities therefore follow its last-wins rule; path- or
        domain-distinct siblings survive independently.
        """
        jar = httpx.Cookies()
        for cookie in self._cookies:
            row = {
                "name": cookie.name,
                "value": cookie.value,
                "domain": cookie.domain,
                "path": cookie.path,
                "expires": cookie.expires,
                "httpOnly": cookie.http_only,
                "secure": cookie.secure,
            }
            jar.jar.set_cookie(
                _cookie_semantics.cookie_from_normalized_entry(row, http_only_key="httpOnly")
            )
        return jar

    def to_storage_rows(self) -> list[dict[str, Any]]:
        """Serialize the ordered typed view, retaining every source row."""
        rows: list[dict[str, Any]] = []
        for cookie in self._cookies:
            rows.append(
                _cookie_semantics.cookie_to_storage_row(
                    cookie,
                    http_only=cookie.http_only,
                    same_site=cookie.same_site,
                    include_same_site=cookie.same_site is not None,
                )
            )
        return rows

    def to_storage_state(self) -> dict[str, Any]:
        """Compatibility projection with an empty ``origins`` list.

        Reproduces the jar's *filtered* view, not any original file — see the
        module docstring. ``origins`` is empty: this type models cookies only.
        """
        return {"cookies": self.to_storage_rows(), "origins": []}

    # -- queries -----------------------------------------------------------

    def names(self) -> set[str]:
        """Return the set of cookie names present, on any domain."""
        return {c.name for c in self._cookies}

    def _auth_material_state(self) -> dict[CookieIdentity, Cookie]:
        """Project authentication-bearing rows by their full cookie identity."""
        names = _cookie_policy._AUTH_MATERIAL_COOKIE_NAMES
        return {cookie.key: cookie for cookie in self._cookies if cookie.name in names}

    def has_secondary_binding(self) -> bool:
        """Whether the Tier-2 binding (``OSID``, or ``APISID`` + ``SAPISID`` +
        ``LSID``) is intact — see
        :func:`notebooklm._auth.cookie_policy._has_valid_secondary_binding`."""
        return _cookie_policy._has_valid_secondary_binding(self.names())

    def is_rotatable(self) -> bool:
        """Whether a ``RotateCookies`` attempt is worth making.

        Deliberately weaker than :meth:`has_secondary_binding` — see
        :func:`notebooklm._auth.cookie_policy._has_rotatable_secondary_binding`.
        """
        return _cookie_policy._has_rotatable_secondary_binding(self.names())

    def validate_required(self, *, context: str = "") -> None:
        """Raise unless the Tier-1 required cookies are present.

        Delegates the policy — including the Tier-2 warning — to
        :func:`notebooklm._auth.cookie_policy._validate_required_cookies`,
        raising its ``RequiredCookieValidationError`` (a ``ValueError``) with
        the same closed-enum ``reason`` every other caller sees.
        """
        _cookie_policy._validate_required_cookies(self.names(), context=context)

    def missing_hint(self, *, browser_label: str | None = None) -> str:
        """Return the actionable recovery hint for this jar's missing cookies."""
        return _cookie_policy.missing_cookies_hint(self.names(), browser_label=browser_label)

    # -- container protocol ------------------------------------------------

    def __iter__(self) -> Iterator[Cookie]:
        return iter(self._cookies)

    def __len__(self) -> int:
        return len(self._cookies)

    def __bool__(self) -> bool:
        return bool(self._cookies)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, CookieJar):
            return NotImplemented
        return self._cookies == other._cookies

    def __repr__(self) -> str:
        """Redacted: names and count only — values are credential-equivalent."""
        return f"CookieJar({len(self._cookies)} cookies: {sorted(self.names())})"


def _sanitized_auth_entries(storage_state: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return the pure sanitized, allowlisted typed view of storage rows."""
    raw_entries = storage_state.get("cookies", [])
    if not isinstance(raw_entries, list):
        return []
    entries: list[dict[str, Any]] = []
    for raw_entry in raw_entries:
        try:
            entry = _cookie_semantics.sanitize_cookie_entry(raw_entry)
        except _cookie_semantics.CookieRowError:
            continue
        if _cookie_policy._is_allowed_auth_domain(entry["domain"]):
            entries.append(entry)
    return entries


def _cookie_from_entry(entry: Mapping[str, Any]) -> Cookie:
    """Build a :class:`Cookie` from an already-sanitized storage-state row.

    Callers must pass rows that have been through
    ``_sanitized_auth_entries`` / ``sanitize_cookie_entry``: identity and value
    fields are trusted here, and ``path`` has already been defaulted to ``/``.
    """
    same_site = entry.get("sameSite", entry.get("same_site"))
    return Cookie(
        name=entry["name"],
        domain=entry["domain"],
        path=entry.get("path") or "/",
        value=entry["value"],
        expires=entry.get("expires"),
        http_only=bool(entry.get("httpOnly", entry.get("http_only", False))),
        secure=bool(entry.get("secure", False)),
        same_site=same_site if isinstance(same_site, str) else None,
    )
