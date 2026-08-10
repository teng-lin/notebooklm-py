"""Immutable account and stored-session values for profile persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TypeAlias

from .cookie_types import CookieJar

ACCOUNT_ROUTE_CLEARED_KEY = "account_route_cleared"


@dataclass(frozen=True, slots=True)
class ProfileAccount:
    authuser: int
    email: str | None = None


@dataclass(frozen=True, slots=True)
class KeepAccount:
    pass


@dataclass(frozen=True, slots=True)
class ClearAccount:
    pass


@dataclass(frozen=True, slots=True)
class SetAccount:
    record: ProfileAccount


AccountDirective: TypeAlias = KeepAccount | ClearAccount | SetAccount


class AccountView(Enum):
    ROUTE = "route"
    OWNER = "owner"
    CARRY = "carry"


@dataclass(frozen=True, slots=True)
class DomainSelection:
    include_domains: frozenset[str] = frozenset()
    include_optional: bool = False


@dataclass(frozen=True, slots=True)
class StoredSession:
    cookies: CookieJar
    account: ProfileAccount | None
    domain_selection: DomainSelection


def parse_profile_account(value: object, *, view: AccountView) -> ProfileAccount | None:
    """Project an untrusted account mapping into the requested typed view."""
    if not isinstance(value, Mapping):
        return None

    raw_authuser = value.get("authuser")
    authuser = raw_authuser if type(raw_authuser) is int and raw_authuser >= 0 else 0
    raw_email = value.get("email")
    email = raw_email.strip() if isinstance(raw_email, str) and raw_email.strip() else None
    account = ProfileAccount(authuser=authuser, email=email)

    if view is AccountView.ROUTE:
        return account
    if view is AccountView.OWNER:
        return account if email is not None else None
    if view is AccountView.CARRY:
        return account
    raise AssertionError("unreachable AccountView")


def parse_domain_selection(namespace: object) -> DomainSelection:
    """Project stored profile opt-ins without retaining mutable input containers."""
    if not isinstance(namespace, Mapping):
        return DomainSelection()

    raw_domains = namespace.get("include_domains")
    include_domains = (
        frozenset(item for item in raw_domains if isinstance(item, str))
        if isinstance(raw_domains, list)
        else frozenset()
    )
    return DomainSelection(
        include_domains=include_domains,
        include_optional=namespace.get("include_optional") is True,
    )
