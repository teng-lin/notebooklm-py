"""Tests for the lossless, copy-on-write profile document value."""

from __future__ import annotations

import dataclasses
import inspect
from collections.abc import Mapping
from copy import deepcopy
from types import MappingProxyType

import pytest

from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.profile_account import AccountView, ProfileAccount
from notebooklm._auth.profile_document import (
    CookieRowPatch,
    ProfileDocument,
    _ProfileDocumentStructureError,
)

COOKIE_SECRET = "COOKIE-VALUE-SENTINEL"
PATH_SECRET = "PATH-VALUE-SENTINEL"
DOMAIN_SECRET = "DOMAIN-VALUE-SENTINEL"
ACCOUNT_SECRET = "ACCOUNT-VALUE-SENTINEL"
UNKNOWN_SECRET = "UNKNOWN-VALUE-SENTINEL"
REPLACEMENT_SECRET = "REPLACEMENT-VALUE-SENTINEL"
ALL_SENTINELS = (
    COOKIE_SECRET,
    PATH_SECRET,
    DOMAIN_SECRET,
    ACCOUNT_SECRET,
    UNKNOWN_SECRET,
    REPLACEMENT_SECRET,
)


def _literal_payload() -> dict[str, object]:
    return {
        "unknown_root": {
            "secret_equivalent": UNKNOWN_SECRET,
            "scalars": [None, True, 7, 2.5],
        },
        "origins": [
            {
                "origin": "https://notebook.google.com",
                "localStorage": [{"name": "opaque", "value": UNKNOWN_SECRET}],
            },
            "malformed-origin",
            None,
        ],
        "notebooklm": {
            "version": 99,
            "account": {
                "authuser": True,
                "email": "  owner@example.com  ",
                "opaque": ACCOUNT_SECRET,
            },
            "include_domains": ["youtube", "youtube", 7, "drive"],
            "include_optional": True,
            "unknown_namespace": {"secret_equivalent": ACCOUNT_SECRET},
        },
        "cookies": [
            {
                "name": "SID",
                "value": COOKIE_SECRET,
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
                "unknown": {
                    "secret_equivalent": UNKNOWN_SECRET,
                    "scalars": [None, True, 1, 1.5],
                },
            },
            {
                "name": "SID",
                "value": COOKIE_SECRET,
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": True,
                "secure": False,
                "sameSite": "Lax",
                "unknown": {
                    "secret_equivalent": UNKNOWN_SECRET,
                    "scalars": [None, True, 1, 1.5],
                },
            },
            {
                "name": "SID",
                "value": PATH_SECRET,
                "domain": ".google.com",
                "path": "/nested",
                "expires": -1.0,
            },
            {
                "name": "SID",
                "value": DOMAIN_SECRET,
                "domain": ".google.com.sg",
                "path": "/",
                "expires": 1800000000,
            },
            {
                "name": "BROKEN",
                "value": UNKNOWN_SECRET,
                "unknown": {"still": "preserved"},
            },
            "opaque-cookie-row",
            17,
            ["opaque-list-row", {"secret_equivalent": UNKNOWN_SECRET}],
            True,
            None,
        ],
        "tail": {"preserve_order": [3, 2, 1]},
    }


def test_value_shapes_signatures_and_redacted_representations_are_exact() -> None:
    assert [field.name for field in dataclasses.fields(CookieRowPatch)] == [
        "index",
        "replacement",
    ]
    assert [field.name for field in dataclasses.fields(ProfileDocument)] == ["_payload"]
    assert str(inspect.signature(CookieRowPatch)) == (
        "(index: 'int', replacement: 'object') -> None"
    )
    assert str(inspect.signature(ProfileDocument)) == "() -> 'None'"
    assert str(inspect.signature(ProfileDocument.decode)) == (
        "(payload: 'object') -> 'ProfileDocument'"
    )
    assert str(inspect.signature(ProfileDocument.empty)) == "() -> 'ProfileDocument'"

    patch = CookieRowPatch(0, {"secret": REPLACEMENT_SECRET})
    assert repr(patch) == "CookieRowPatch(index=0)"
    with pytest.raises(dataclasses.FrozenInstanceError):
        patch.index = 1  # type: ignore[misc]

    document = ProfileDocument.decode(_literal_payload())
    rendered = f"{document!r} {document!s} {patch!r}"
    assert "_payload" not in rendered
    assert all(secret not in rendered for secret in ALL_SENTINELS)

    with pytest.raises(AssertionError) as raised:
        assert document == ProfileDocument.decode(document.to_json())
    assert all(secret not in str(raised.value) for secret in ALL_SENTINELS)


def test_profile_document_construction_is_factory_only() -> None:
    with pytest.raises(TypeError) as raised:
        ProfileDocument()
    assert str(raised.value) == "ProfileDocument must be created with decode() or empty()"
    assert ProfileDocument.decode({}).to_json() == {}
    assert ProfileDocument.empty().to_json() == {"cookies": [], "origins": []}


@pytest.mark.parametrize("payload", [None, [], "root-secret", 7, True])
def test_decode_rejects_non_object_roots_with_a_value_free_error(payload: object) -> None:
    with pytest.raises(_ProfileDocumentStructureError) as raised:
        ProfileDocument.decode(payload)
    error = raised.value
    assert type(error) is _ProfileDocumentStructureError
    assert error.field == "root"
    assert error.reason == "not_object"
    assert str(error) == "profile document root must be an object"
    assert repr(error) == (
        "_ProfileDocumentStructureError('profile document root must be an object')"
    )
    assert "root-secret" not in f"{error!s} {error!r}"


@pytest.mark.parametrize(
    "payload",
    [{}, {"cookies": None}, {"cookies": {}}, {"cookies": "cookie-secret"}, {"cookies": ()}],
)
def test_raw_cookie_rows_rejects_missing_or_non_list_values(payload: dict[str, object]) -> None:
    document = ProfileDocument.decode(payload)
    with pytest.raises(_ProfileDocumentStructureError) as raised:
        document.raw_cookie_rows()
    error = raised.value
    assert error.field == "cookies"
    assert error.reason == "not_list"
    assert str(error) == "profile document cookies must be a list"
    assert "cookie-secret" not in f"{error!s} {error!r}"


def test_round_trip_preserves_order_unknowns_duplicates_and_scalar_types() -> None:
    payload = _literal_payload()
    expected = deepcopy(payload)
    document = ProfileDocument.decode(payload)

    result = document.to_json()
    assert result == expected
    assert list(result) == list(expected)
    rows = result["cookies"]
    assert isinstance(rows, list)
    assert rows[0] == rows[1]
    assert rows[0] is not rows[1]
    assert type(rows[0]["expires"]) is int
    assert type(rows[2]["expires"]) is float
    assert result["origins"] == expected["origins"]
    assert result["notebooklm"] == expected["notebooklm"]

    payload["unknown_root"]["scalars"].append("input-attack")
    payload["cookies"][0]["unknown"]["scalars"].append("input-attack")
    payload["origins"].clear()
    assert document.to_json() == expected


def test_backing_snapshot_and_all_returned_containers_are_isolated() -> None:
    document = ProfileDocument.decode(_literal_payload())
    expected = document.to_json()

    with pytest.raises(TypeError):
        document._payload["root-attack"] = True  # type: ignore[index]
    backing_root = document._payload["unknown_root"]
    assert isinstance(backing_root, Mapping)
    with pytest.raises(TypeError):
        backing_root["nested-attack"] = True  # type: ignore[index]
    backing_scalars = backing_root["scalars"]
    assert isinstance(backing_scalars, tuple)
    with pytest.raises(TypeError):
        backing_scalars[0] = "nested-attack"  # type: ignore[index]

    first = document.to_json()
    second = document.to_json()
    assert first is not second
    first["unknown_root"]["scalars"].append("output-attack")
    first["cookies"][0]["unknown"]["scalars"].append("row-output-attack")
    first["notebooklm"]["account"]["authuser"] = 22
    assert second == expected
    assert document.to_json() == expected

    raw = document.raw_cookie_rows()
    assert raw == tuple(expected["cookies"])
    raw[0]["unknown"]["scalars"].append("raw-row-attack")
    raw[7].append("raw-list-attack")
    assert document.to_json() == expected


def test_empty_is_exact_and_distinct_from_an_empty_decoded_object() -> None:
    assert ProfileDocument.empty().to_json() == {"cookies": [], "origins": []}
    assert ProfileDocument.decode({}).to_json() == {}
    assert list(ProfileDocument.empty().raw_cookie_rows()) == []
    assert ProfileDocument.decode({}).cookies() == CookieJar()


def test_typed_views_are_literal_lossy_projections_without_round_trip_effects() -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()

    assert list(document.cookies()) == [
        Cookie(
            name="SID",
            domain=".google.com",
            path="/",
            value=COOKIE_SECRET,
            expires=None,
            http_only=True,
            secure=False,
            same_site="Lax",
        ),
        Cookie(
            name="SID",
            domain=".google.com",
            path="/",
            value=COOKIE_SECRET,
            expires=None,
            http_only=True,
            secure=False,
            same_site="Lax",
        ),
        Cookie(
            name="SID",
            domain=".google.com",
            path="/nested",
            value=PATH_SECRET,
            expires=-1.0,
        ),
        Cookie(
            name="SID",
            domain=".google.com.sg",
            path="/",
            value=DOMAIN_SECRET,
            expires=1800000000,
        ),
    ]
    normalized_account = ProfileAccount(0, "owner@example.com")
    assert document.account_for(AccountView.ROUTE) == normalized_account
    assert document.account_for(AccountView.OWNER) == normalized_account
    assert document.account_for(AccountView.CARRY) == normalized_account
    assert document.include_domains() == frozenset({"youtube", "drive"})
    assert document.include_optional() is True

    assert document.cookies().to_storage_state() != {
        "cookies": original["cookies"],
        "origins": [],
    }
    assert document.to_json() == original


def test_malformed_namespace_is_preserved_while_typed_views_default() -> None:
    namespace = {
        "account": ACCOUNT_SECRET,
        "include_domains": "youtube",
        "include_optional": 1,
        "unknown": {"secret": UNKNOWN_SECRET},
    }
    document = ProfileDocument.decode({"cookies": [], "notebooklm": namespace})
    assert document.account_for(AccountView.ROUTE) is None
    assert document.account_for(AccountView.OWNER) is None
    assert document.account_for(AccountView.CARRY) is None
    assert document.include_domains() == frozenset()
    assert document.include_optional() is False
    assert document.to_json()["notebooklm"] == namespace


def test_replace_cookie_rows_consumes_once_and_isolates_every_direction() -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()
    replacement = {"opaque": {"nested": [REPLACEMENT_SECRET]}}
    supplied = [replacement, "opaque-row", replacement]
    observed: list[int] = []

    def rows():
        for index, row in enumerate(supplied):
            observed.append(index)
            yield row

    replaced = document.replace_cookie_rows(rows())
    expected = deepcopy(original)
    expected["cookies"] = deepcopy(supplied)
    assert observed == [0, 1, 2]
    assert replaced.to_json() == expected
    assert document.to_json() == original

    replacement["opaque"]["nested"].append("source-attack")
    supplied.append("iterable-attack")
    result = replaced.to_json()
    result["cookies"][0]["opaque"]["nested"].append("result-attack")
    assert replaced.to_json() == expected


def test_replace_cookie_rows_snapshots_each_row_before_requesting_the_next() -> None:
    document = ProfileDocument.decode(_literal_payload())
    first = {"nested": [REPLACEMENT_SECRET]}

    def rows():
        yield first
        first["nested"].append("mutation-after-first-yield")
        yield MappingProxyType({"second": UNKNOWN_SECRET})

    replaced = document.replace_cookie_rows(rows())
    assert first == {"nested": [REPLACEMENT_SECRET, "mutation-after-first-yield"]}
    assert replaced.to_json()["cookies"] == [
        {"nested": [REPLACEMENT_SECRET]},
        {"second": UNKNOWN_SECRET},
    ]


def test_with_namespace_replaces_removes_and_preserves_empty_exactly() -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()
    namespace = {
        "account": {"authuser": 8, "email": ACCOUNT_SECRET},
        "unknown": {"nested": [UNKNOWN_SECRET]},
    }

    replaced = document.with_namespace(namespace)
    expected = deepcopy(original)
    expected["notebooklm"] = deepcopy(namespace)
    assert replaced.to_json() == expected
    namespace["unknown"]["nested"].append("namespace-input-attack")
    assert replaced.to_json() == expected

    emptied = document.with_namespace({})
    empty_expected = deepcopy(original)
    empty_expected["notebooklm"] = {}
    assert emptied.to_json() == empty_expected

    removed = document.with_namespace(None)
    removed_expected = deepcopy(original)
    del removed_expected["notebooklm"]
    assert removed.to_json() == removed_expected
    assert document.to_json() == original


def test_patch_cookie_rows_replaces_mapping_and_opaque_slots_atomically() -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()
    replacement_mapping = {
        "name": "NEW",
        "value": REPLACEMENT_SECRET,
        "domain": ".google.com",
        "unknown": {"nested": [UNKNOWN_SECRET]},
    }
    supplied = [
        CookieRowPatch(0, "mapping-to-opaque"),
        CookieRowPatch(5, replacement_mapping),
    ]
    observed: list[int] = []

    def patches():
        for patch in supplied:
            observed.append(patch.index)
            yield patch

    patched = document.patch_cookie_rows(patches())
    expected = deepcopy(original)
    expected["cookies"][0] = "mapping-to-opaque"
    expected["cookies"][5] = deepcopy(replacement_mapping)
    assert observed == [0, 5]
    assert patched.to_json() == expected
    assert document.to_json() == original

    replacement_mapping["unknown"]["nested"].append("patch-input-attack")
    assert patched.to_json() == expected


@pytest.mark.parametrize("index", [True, 1.0, "1", None])
def test_patch_cookie_rows_rejects_bool_and_non_integer_indices(index: object) -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()
    patch = CookieRowPatch(index, REPLACEMENT_SECRET)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="^cookie row patch index must be an integer$") as raised:
        document.patch_cookie_rows([patch])
    assert REPLACEMENT_SECRET not in f"{raised.value!s} {raised.value!r}"
    assert document.to_json() == original


@pytest.mark.parametrize("index", [-1, 10, 100])
def test_patch_cookie_rows_rejects_out_of_range_indices(index: int) -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()
    with pytest.raises(IndexError, match="^cookie row patch index is out of range$") as raised:
        document.patch_cookie_rows([CookieRowPatch(index, REPLACEMENT_SECRET)])
    assert REPLACEMENT_SECRET not in f"{raised.value!s} {raised.value!r}"
    assert document.to_json() == original


def test_patch_cookie_rows_rejects_duplicates_after_full_materialization() -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()
    observed: list[str] = []

    def patches():
        observed.append("first")
        yield CookieRowPatch(1, REPLACEMENT_SECRET)
        observed.append("second")
        yield CookieRowPatch(1, UNKNOWN_SECRET)

    with pytest.raises(ValueError, match="^cookie row patch indices must be unique$") as raised:
        document.patch_cookie_rows(patches())
    assert observed == ["first", "second"]
    assert all(secret not in f"{raised.value!s} {raised.value!r}" for secret in ALL_SENTINELS)
    assert document.to_json() == original


def test_invalid_patch_is_checked_only_after_the_generator_is_fully_consumed() -> None:
    document = ProfileDocument.decode(_literal_payload())
    original = document.to_json()
    observed: list[str] = []

    def patches():
        observed.append("invalid")
        yield CookieRowPatch(True, REPLACEMENT_SECRET)
        observed.append("later")
        yield CookieRowPatch(2, UNKNOWN_SECRET)

    with pytest.raises(TypeError, match="^cookie row patch index must be an integer$"):
        document.patch_cookie_rows(patches())
    assert observed == ["invalid", "later"]
    assert document.to_json() == original


def test_patch_cookie_rows_keeps_structural_errors_separate_from_patch_policy() -> None:
    document = ProfileDocument.decode({"cookies": "not-a-list", "opaque": UNKNOWN_SECRET})
    with pytest.raises(_ProfileDocumentStructureError) as raised:
        document.patch_cookie_rows([CookieRowPatch(0, REPLACEMENT_SECRET)])
    assert raised.value.field == "cookies"
    assert raised.value.reason == "not_list"
    assert all(secret not in f"{raised.value!s} {raised.value!r}" for secret in ALL_SENTINELS)


def test_copy_operations_accept_nested_read_only_mappings_without_aliasing() -> None:
    document = ProfileDocument.decode(_literal_payload())

    replace_nested_source = {"items": [REPLACEMENT_SECRET]}
    replace_row_source: dict[str, object] = {"opaque": MappingProxyType(replace_nested_source)}
    replace_row = MappingProxyType(replace_row_source)
    replaced = document.replace_cookie_rows([replace_row])
    replace_expected = {"opaque": {"items": [REPLACEMENT_SECRET]}}
    assert replaced.to_json()["cookies"] == [replace_expected]
    replace_nested_source["items"].append("replace-source-attack")
    replace_row_source["later"] = UNKNOWN_SECRET
    replace_output = replaced.to_json()
    replace_output["cookies"][0]["opaque"]["items"].append("replace-output-attack")
    assert replaced.to_json()["cookies"] == [replace_expected]

    patch_nested_source = {"items": [REPLACEMENT_SECRET]}
    patch_row_source: dict[str, object] = {"opaque": MappingProxyType(patch_nested_source)}
    patch_row = MappingProxyType(patch_row_source)
    patched = document.patch_cookie_rows([CookieRowPatch(0, patch_row)])
    patch_expected = {"opaque": {"items": [REPLACEMENT_SECRET]}}
    assert patched.to_json()["cookies"][0] == patch_expected
    patch_nested_source["items"].append("patch-source-attack")
    patch_row_source["later"] = UNKNOWN_SECRET
    patch_output = patched.to_json()
    patch_output["cookies"][0]["opaque"]["items"].append("patch-output-attack")
    assert patched.to_json()["cookies"][0] == patch_expected

    namespace_nested_source = {"items": [ACCOUNT_SECRET]}
    namespace_source: dict[str, object] = {"unknown": MappingProxyType(namespace_nested_source)}
    namespace = MappingProxyType(namespace_source)
    namespaced = document.with_namespace(namespace)
    namespace_expected = {"unknown": {"items": [ACCOUNT_SECRET]}}
    assert namespaced.to_json()["notebooklm"] == namespace_expected
    namespace_nested_source["items"].append("namespace-source-attack")
    namespace_source["later"] = UNKNOWN_SECRET
    namespace_output = namespaced.to_json()
    namespace_output["notebooklm"]["unknown"]["items"].append("namespace-output-attack")
    assert namespaced.to_json()["notebooklm"] == namespace_expected
