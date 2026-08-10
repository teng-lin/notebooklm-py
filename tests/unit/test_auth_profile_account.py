"""Tests for dependency-bottom profile account and session values."""

from __future__ import annotations

import ast
import dataclasses
import inspect
from pathlib import Path
from typing import get_args

import pytest

from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.profile_account import (
    AccountDirective,
    AccountView,
    ClearAccount,
    DomainSelection,
    KeepAccount,
    ProfileAccount,
    SetAccount,
    StoredSession,
    parse_domain_selection,
    parse_profile_account,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "src" / "notebooklm" / "_auth" / "profile_account.py"


def test_value_shapes_signatures_and_immutability_are_exact() -> None:
    assert [field.name for field in dataclasses.fields(ProfileAccount)] == ["authuser", "email"]
    assert [field.name for field in dataclasses.fields(KeepAccount)] == []
    assert [field.name for field in dataclasses.fields(ClearAccount)] == []
    assert [field.name for field in dataclasses.fields(SetAccount)] == ["record"]
    assert [field.name for field in dataclasses.fields(DomainSelection)] == [
        "include_domains",
        "include_optional",
    ]
    assert [field.name for field in dataclasses.fields(StoredSession)] == [
        "cookies",
        "account",
        "domain_selection",
    ]

    assert str(inspect.signature(ProfileAccount)) == (
        "(authuser: 'int', email: 'str | None' = None) -> None"
    )
    assert str(inspect.signature(KeepAccount)) == "() -> None"
    assert str(inspect.signature(ClearAccount)) == "() -> None"
    assert str(inspect.signature(SetAccount)) == "(record: 'ProfileAccount') -> None"
    assert str(inspect.signature(DomainSelection)) == (
        "(include_domains: 'frozenset[str]' = frozenset(), "
        "include_optional: 'bool' = False) -> None"
    )
    assert str(inspect.signature(StoredSession)) == (
        "(cookies: 'CookieJar', account: 'ProfileAccount | None', "
        "domain_selection: 'DomainSelection') -> None"
    )
    assert str(inspect.signature(parse_profile_account)) == (
        "(value: 'object', *, view: 'AccountView') -> 'ProfileAccount | None'"
    )
    assert str(inspect.signature(parse_domain_selection)) == (
        "(namespace: 'object') -> 'DomainSelection'"
    )

    values = [
        ProfileAccount(2, "a@example.com"),
        KeepAccount(),
        ClearAccount(),
        SetAccount(ProfileAccount(2)),
        DomainSelection(),
    ]
    for value in values:
        assert type(value).__dataclass_params__.frozen is True
        assert not hasattr(value, "__dict__")

    account_value = values[0]
    with pytest.raises(dataclasses.FrozenInstanceError):
        account_value.authuser = 7  # type: ignore[misc]

    assert ProfileAccount(2, "a@example.com") == ProfileAccount(2, "a@example.com")
    assert hash(ProfileAccount(2, "a@example.com")) == hash(ProfileAccount(2, "a@example.com"))
    assert KeepAccount() == KeepAccount()
    assert hash(KeepAccount()) == hash(KeepAccount())
    assert ClearAccount() == ClearAccount()
    assert hash(ClearAccount()) == hash(ClearAccount())
    assert SetAccount(ProfileAccount(2)) == SetAccount(ProfileAccount(2))
    assert hash(SetAccount(ProfileAccount(2))) == hash(SetAccount(ProfileAccount(2)))
    assert hash(DomainSelection()) == hash(DomainSelection())
    assert DomainSelection().include_domains == frozenset()
    with pytest.raises(AttributeError):
        DomainSelection().include_domains.add("youtube")  # type: ignore[attr-defined]


def test_direct_construction_is_deliberately_permissive() -> None:
    odd = ProfileAccount(authuser=True, email=17)  # type: ignore[arg-type]
    assert odd.authuser is True
    assert odd.email == 17


def test_directive_union_enum_and_composition_are_exact() -> None:
    assert get_args(AccountDirective) == (KeepAccount, ClearAccount, SetAccount)
    assert [(member.name, member.value) for member in AccountView] == [
        ("ROUTE", "route"),
        ("OWNER", "owner"),
        ("CARRY", "carry"),
    ]
    account = ProfileAccount(4, "owner@example.com")
    assert SetAccount(account).record is account

    jar = CookieJar([Cookie(name="SID", domain=".google.com", path="/", value="secret")])
    selection = DomainSelection(frozenset({"youtube"}), include_optional=True)
    session = StoredSession(jar, account, selection)
    assert session == StoredSession(jar, account, selection)
    assert session.cookies is jar
    assert not hasattr(session, "__dict__")


@pytest.mark.parametrize("view", list(AccountView))
@pytest.mark.parametrize("value", [None, 7, "account", ["authuser", 2]])
def test_account_parser_rejects_non_mappings(value: object, view: AccountView) -> None:
    assert parse_profile_account(value, view=view) is None


@pytest.mark.parametrize(
    ("value", "view", "expected"),
    [
        ({}, AccountView.ROUTE, ProfileAccount(0)),
        ({}, AccountView.OWNER, None),
        ({}, AccountView.CARRY, ProfileAccount(0)),
        ({"authuser": True}, AccountView.ROUTE, ProfileAccount(0)),
        ({"authuser": -1}, AccountView.ROUTE, ProfileAccount(0)),
        ({"authuser": "2"}, AccountView.ROUTE, ProfileAccount(0)),
        ({"authuser": 2}, AccountView.ROUTE, ProfileAccount(2)),
        (
            {"authuser": 2, "email": "  owner@example.com  "},
            AccountView.ROUTE,
            ProfileAccount(2, "owner@example.com"),
        ),
        (
            {"authuser": 2, "email": "  owner@example.com  "},
            AccountView.OWNER,
            ProfileAccount(2, "owner@example.com"),
        ),
        (
            {"authuser": 2, "email": "  owner@example.com  "},
            AccountView.CARRY,
            ProfileAccount(2, "owner@example.com"),
        ),
        ({"authuser": 2, "email": "   "}, AccountView.OWNER, None),
        ({"authuser": 2, "email": 17}, AccountView.OWNER, None),
        ({"unknown": {"mutable": []}}, AccountView.CARRY, ProfileAccount(0)),
    ],
)
def test_account_parser_normalization_matrix(
    value: object, view: AccountView, expected: ProfileAccount | None
) -> None:
    assert parse_profile_account(value, view=view) == expected


@pytest.mark.parametrize(
    ("namespace", "expected"),
    [
        (None, DomainSelection()),
        ("namespace", DomainSelection()),
        ({}, DomainSelection()),
        ({"include_domains": ("youtube",)}, DomainSelection()),
        ({"include_domains": {"youtube"}}, DomainSelection()),
        (
            {"include_domains": ["youtube", "youtube", 2, True, None, "drive"]},
            DomainSelection(frozenset({"youtube", "drive"})),
        ),
        ({"include_optional": True}, DomainSelection(include_optional=True)),
        ({"include_optional": 1}, DomainSelection()),
        ({"include_optional": "true"}, DomainSelection()),
    ],
)
def test_domain_selection_parser_matrix(namespace: object, expected: DomainSelection) -> None:
    assert parse_domain_selection(namespace) == expected


def test_parsers_do_not_mutate_or_retain_mutable_inputs() -> None:
    account_input: dict[str, object] = {
        "authuser": 3,
        "email": " owner@example.com ",
        "unknown": ["preserve"],
    }
    account_before = {**account_input, "unknown": ["preserve"]}
    account = parse_profile_account(account_input, view=AccountView.CARRY)
    assert account_input == account_before
    account_input["authuser"] = 9
    account_input["email"] = "changed@example.com"
    assert account == ProfileAccount(3, "owner@example.com")

    domains = ["youtube", "drive"]
    namespace: dict[str, object] = {"include_domains": domains, "include_optional": True}
    selection = parse_domain_selection(namespace)
    assert namespace == {"include_domains": ["youtube", "drive"], "include_optional": True}
    domains.append("maps")
    namespace["include_optional"] = False
    assert selection == DomainSelection(frozenset({"youtube", "drive"}), True)


def test_profile_account_dependency_guard_covers_every_lexical_scope() -> None:
    tree = ast.parse(MODULE_PATH.read_text(encoding="utf-8"), filename=str(MODULE_PATH))
    imports = [node for node in ast.walk(tree) if isinstance(node, ast.Import | ast.ImportFrom)]
    function_imports: list[ast.Import | ast.ImportFrom] = []
    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    ):
        function_imports.extend(
            node for node in ast.walk(function) if isinstance(node, ast.Import | ast.ImportFrom)
        )
    assert function_imports == []

    descriptors = []
    for node in imports:
        if isinstance(node, ast.Import):
            descriptors.extend((0, item.name, item.asname) for item in node.names)
        else:
            descriptors.extend((node.level, node.module or "", item.name) for item in node.names)
    assert descriptors == [
        (0, "__future__", "annotations"),
        (0, "collections.abc", "Mapping"),
        (0, "dataclasses", "dataclass"),
        (0, "enum", "Enum"),
        (0, "typing", "TypeAlias"),
        (1, "cookie_types", "CookieJar"),
    ]
