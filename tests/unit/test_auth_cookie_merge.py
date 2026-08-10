"""Literal pure tests for immutable cookie merge decisions."""

from __future__ import annotations

import builtins
import dataclasses
import logging
import os
import subprocess
import sys
import warnings
from collections.abc import Mapping
from dataclasses import FrozenInstanceError
from http.cookiejar import Cookie as HttpCookie
from types import MappingProxyType

import httpx
import pytest

from notebooklm._auth import storage
from notebooklm._auth.cookie_merge import (
    CookieMergeDecision,
    RecoveryObservation,
    decide_cookie_merge,
    decide_legacy_cookie_overlay,
    equivalent_identities,
    snapshot_index_last_wins,
    stored_cookie_identity,
)
from notebooklm._auth.cookie_types import Cookie, CookieIdentity, CookieJar
from notebooklm._auth.profile_document import ProfileDocument


def _cookie(
    value: str,
    *,
    name: str = "SID",
    domain: str = ".google.com",
    path: str = "/",
    expires: int | float | None = None,
    secure: bool = False,
    http_only: bool = False,
    same_site: str | None = None,
) -> Cookie:
    return Cookie(
        name=name,
        domain=domain,
        path=path,
        value=value,
        expires=expires,
        secure=secure,
        http_only=http_only,
        same_site=same_site,
    )


def _row(
    value: object,
    *,
    name: object = "SID",
    domain: object = ".google.com",
    path: object = "/",
    **extra: object,
) -> dict[str, object]:
    return {"name": name, "value": value, "domain": domain, "path": path, **extra}


def _document(rows: list[object], **extra: object) -> ProfileDocument:
    return ProfileDocument.decode(
        {
            "cookies": rows,
            "origins": [{"origin": "https://preserved.example", "localStorage": []}],
            "notebooklm": {"account": {"authuser": 2}},
            "futureRoot": {"nested": ["preserved"]},
            **extra,
        }
    )


def _http_cookie(
    name: str,
    value: str | None,
    *,
    secure: object = False,
) -> HttpCookie:
    return HttpCookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".google.com",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=secure,  # type: ignore[arg-type]  # deliberate legacy duck value
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={},
        rfc2109=False,
    )


def _json(decision: CookieMergeDecision) -> dict[str, object] | None:
    return None if decision.document is None else decision.document.to_json()


def _baseline_values(jar: CookieJar) -> dict[CookieIdentity, tuple[object, ...]]:
    return {
        cookie.key: (
            cookie.value,
            type(cookie.expires),
            cookie.expires,
            cookie.secure,
            cookie.http_only,
        )
        for cookie in jar
    }


def _baseline_same_site(jar: CookieJar) -> dict[CookieIdentity, str | None]:
    return {cookie.key: cookie.same_site for cookie in jar}


def _decide(
    rows: list[object],
    *,
    observation: tuple[Cookie, ...],
    baseline: tuple[Cookie, ...] = (),
    recovery: RecoveryObservation | None = None,
) -> CookieMergeDecision:
    return decide_cookie_merge(
        stored=_document(rows),
        observation=CookieJar(observation),
        baseline=CookieJar(baseline),
        recovery_observation=recovery,
    )


def test_value_shapes_are_frozen_slotted_redacted_and_defensively_copied() -> None:
    identity = CookieIdentity("SID", ".google.com", "/")
    source_values: set[str | None] = {"recovery-secret"}
    source: dict[CookieIdentity, set[str | None]] = {identity: source_values}
    recovery = RecoveryObservation(source)  # type: ignore[arg-type]
    source_values.add("later-secret")
    source.clear()

    baseline_source = [_cookie("baseline-secret")]
    document_source = {"cookies": [_row("document-secret")], "origins": []}
    rejected_source = {identity}
    decision = CookieMergeDecision(
        document=ProfileDocument.decode(document_source),
        next_baseline=CookieJar(baseline_source),
        rejected=frozenset(rejected_source),
        updated_rows=1,
    )
    baseline_source.append(_cookie("later-baseline-secret", name="APISID"))
    document_source["cookies"] = []
    rejected_source.clear()

    assert dataclasses.is_dataclass(recovery)
    assert dataclasses.is_dataclass(decision)
    assert recovery.__slots__ == ("values_by_identity",)
    assert set(CookieMergeDecision.__slots__) == {
        "document",
        "next_baseline",
        "rejected",
        "updated_rows",
        "_conflicts",
    }
    assert recovery.values_by_identity == {identity: frozenset({"recovery-secret"})}
    assert len(decision.next_baseline) == 1
    assert decision.document is not None
    assert decision.document.to_json()["cookies"] == [_row("document-secret")]
    assert decision.rejected == frozenset({identity})
    assert isinstance(recovery.values_by_identity, MappingProxyType)
    with pytest.raises(TypeError):
        recovery.values_by_identity[identity] = frozenset()  # type: ignore[index]
    with pytest.raises(FrozenInstanceError):
        decision.updated_rows = 2  # type: ignore[misc]
    rendered = repr(recovery) + str(recovery) + repr(decision) + str(decision)
    assert "secret" not in rendered


@pytest.mark.parametrize(
    ("kwargs", "error", "message"),
    [
        (
            {"document": None, "next_baseline": CookieJar(), "updated_rows": True},
            TypeError,
            "integer",
        ),
        (
            {"document": None, "next_baseline": CookieJar(), "updated_rows": -1},
            ValueError,
            "nonnegative",
        ),
        (
            {
                "document": ProfileDocument.empty(),
                "next_baseline": CookieJar(),
                "updated_rows": 0,
            },
            ValueError,
            "presence",
        ),
        (
            {"document": None, "next_baseline": CookieJar(), "updated_rows": 1},
            ValueError,
            "presence",
        ),
    ],
)
def test_decision_constructor_rejects_invalid_counts_with_bounded_errors(
    kwargs: dict[str, object], error: type[Exception], message: str
) -> None:
    with pytest.raises(error, match=message) as excinfo:
        CookieMergeDecision(**kwargs)  # type: ignore[arg-type]
    assert "secret" not in str(excinfo.value)


def test_recovery_constructor_error_is_bounded_and_redacted() -> None:
    with pytest.raises(TypeError, match="map identities to value sets") as excinfo:
        RecoveryObservation({"secret-identity": {"secret-value"}})  # type: ignore[dict-item]
    assert "secret" not in str(excinfo.value)


def test_identity_helpers_are_path_exact_domain_equivalent_and_tolerant() -> None:
    dotted = CookieIdentity("SID", ".google.com", "/a")
    assert equivalent_identities(dotted) == frozenset(
        {dotted, CookieIdentity("SID", "google.com", "/a")}
    )
    assert equivalent_identities(CookieIdentity("SID", "google.com", "/b")) == frozenset(
        {
            CookieIdentity("SID", "google.com", "/b"),
            CookieIdentity("SID", ".google.com", "/b"),
        }
    )
    assert stored_cookie_identity(_row("v", path="")) == CookieIdentity("SID", ".google.com", "/")
    assert stored_cookie_identity("opaque") is None
    assert stored_cookie_identity(_row("v", name=1)) is None
    assert stored_cookie_identity(_row("v", path=1)) is None


def test_snapshot_index_is_read_only_last_wins_without_moving_first_position() -> None:
    first = _cookie("first")
    sibling = _cookie("sibling", name="APISID")
    last = _cookie("last")
    index = snapshot_index_last_wins(CookieJar((first, sibling, last)))
    assert list(index) == [first.key, sibling.key]
    assert list(index.values()) == [last, sibling]
    with pytest.raises(TypeError):
        index[first.key] = first  # type: ignore[index]


def test_baseline_truth_table_unchanged_preserves_complete_document() -> None:
    raw = _row("same", sameSite="Strict", futureRow={"nested": [1]})
    decision = _decide(
        [raw, "opaque", {"malformed": "row"}],
        observation=(_cookie("same"),),
        baseline=(_cookie("same"),),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset()
    assert _baseline_values(decision.next_baseline) == _baseline_values(
        CookieJar((_cookie("same"),))
    )


def test_baseline_truth_table_new_absent_appends_canonical_row() -> None:
    decision = _decide(["opaque"], observation=(_cookie("fresh"),))
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset()
    assert _json(decision) == {
        "cookies": [
            "opaque",
            {
                "name": "SID",
                "value": "fresh",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "None",
            },
        ],
        "origins": [{"origin": "https://preserved.example", "localStorage": []}],
        "notebooklm": {"account": {"authuser": 2}},
        "futureRoot": {"nested": ["preserved"]},
    }
    assert _baseline_values(decision.next_baseline) == _baseline_values(
        CookieJar((_cookie("fresh"),))
    )


def test_baseline_truth_table_new_existing_equal_applies_owned_attributes() -> None:
    decision = _decide(
        [_row("fresh", expires=1, secure=False, httpOnly=False, sameSite="Lax", unknown="keep")],
        observation=(_cookie("fresh", expires=2, secure=True, http_only=True),),
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset()
    assert _json(decision)["cookies"] == [  # type: ignore[index]
        {
            "name": "SID",
            "value": "fresh",
            "domain": ".google.com",
            "path": "/",
            "expires": 2,
            "secure": True,
            "httpOnly": True,
            "sameSite": "Lax",
            "unknown": "keep",
        }
    ]


def test_baseline_truth_table_new_existing_different_rejects_and_keeps_absence() -> None:
    identity = CookieIdentity("SID", ".google.com", "/")
    raw = _row("sibling", unknown="keep")
    decision = _decide([raw], observation=(_cookie("fresh"),))
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset({identity})
    assert list(decision.next_baseline) == []


@pytest.mark.parametrize("disk_value", ["baseline", "fresh"], ids=["disk-baseline", "disk-current"])
def test_baseline_truth_table_changed_accepted_values_apply(disk_value: str) -> None:
    decision = _decide(
        [_row(disk_value, expires=1, sameSite="Strict", unknown="keep")],
        observation=(_cookie("fresh", expires=9, secure=True, http_only=True),),
        baseline=(_cookie("baseline", expires=1),),
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset()
    assert _json(decision)["cookies"] == [  # type: ignore[index]
        {
            "name": "SID",
            "value": "fresh",
            "domain": ".google.com",
            "path": "/",
            "expires": 9,
            "sameSite": "Strict",
            "unknown": "keep",
            "secure": True,
            "httpOnly": True,
        }
    ]


def test_baseline_truth_table_changed_third_value_rejects_old_baseline() -> None:
    identity = CookieIdentity("SID", ".google.com", "/")
    decision = _decide(
        [_row("third", unknown="keep")],
        observation=(_cookie("fresh"),),
        baseline=(_cookie("baseline"),),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset({identity})
    assert _baseline_values(decision.next_baseline) == _baseline_values(
        CookieJar((_cookie("baseline"),))
    )


@pytest.mark.parametrize(
    ("disk_value", "updated", "rejected"),
    [("baseline", 1, False), ("sibling", 0, True)],
)
def test_baseline_truth_table_deletion_is_value_cas_guarded(
    disk_value: str, updated: int, rejected: bool
) -> None:
    identity = CookieIdentity("SID", ".google.com", "/")
    decision = _decide([_row(disk_value)], observation=(), baseline=(_cookie("baseline"),))
    assert decision.updated_rows == updated
    assert decision.rejected == (frozenset({identity}) if rejected else frozenset())
    if updated:
        assert _json(decision)["cookies"] == []  # type: ignore[index]
        assert list(decision.next_baseline) == []
    else:
        assert decision.document is None
        assert _baseline_values(decision.next_baseline) == _baseline_values(
            CookieJar((_cookie("baseline"),))
        )


@pytest.mark.parametrize(
    "observation",
    [
        _cookie("same", expires=2),
        _cookie("same", secure=True),
        _cookie("same", http_only=True),
    ],
    ids=["expiry", "secure", "http-only"],
)
def test_baseline_truth_table_owned_attribute_only_changes_are_dirty(
    observation: Cookie,
) -> None:
    decision = _decide(
        [_row("same", expires=-1, secure=False, httpOnly=False)],
        observation=(observation,),
        baseline=(_cookie("same"),),
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset()


def test_baseline_truth_table_same_site_only_change_is_not_dirty() -> None:
    decision = _decide(
        [_row("same", sameSite="Strict", unknown="keep")],
        observation=(_cookie("same", same_site="None"),),
        baseline=(_cookie("same", same_site="Lax"),),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset()


def test_delta_wins_over_dotted_deletion_and_path_sibling_survives() -> None:
    decision = _decide(
        [
            _row("baseline", domain="google.com", path="/", unknown="keep"),
            _row("path-sibling", domain="google.com", path="/other", untouched=True),
        ],
        observation=(_cookie("fresh", domain="google.com"),),
        baseline=(_cookie("baseline", domain=".google.com"),),
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset()
    assert _json(decision)["cookies"] == [  # type: ignore[index]
        {
            "name": "SID",
            "value": "fresh",
            "domain": "google.com",
            "path": "/",
            "unknown": "keep",
            "expires": -1,
            "secure": False,
            "httpOnly": False,
            "sameSite": "None",
        },
        {
            "name": "SID",
            "value": "path-sibling",
            "domain": "google.com",
            "path": "/other",
            "untouched": True,
        },
    ]


def test_new_cookie_cas_refuses_dotted_equivalent_and_mixed_duplicates_count_slots() -> None:
    identity = CookieIdentity("SID", ".google.com", "/")
    decision = _decide(
        [_row("fresh", domain="google.com", slot=1), _row("sibling", slot=2)],
        observation=(_cookie("fresh"),),
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset({identity})
    assert _json(decision)["cookies"] == [  # type: ignore[index]
        {
            "name": "SID",
            "value": "fresh",
            "domain": "google.com",
            "path": "/",
            "slot": 1,
            "expires": -1,
            "secure": False,
            "httpOnly": False,
            "sameSite": "None",
        },
        {"name": "SID", "value": "sibling", "domain": ".google.com", "path": "/", "slot": 2},
    ]
    assert list(decision.next_baseline) == []


def test_accepted_duplicates_each_count_and_observation_duplicates_are_last_wins() -> None:
    decision = _decide(
        [_row("baseline", slot=1), _row("fresh", slot=2)],
        observation=(_cookie("ignored"), _cookie("fresh", secure=True)),
        baseline=(_cookie("baseline"),),
    )
    assert decision.updated_rows == 2
    assert decision.rejected == frozenset()
    rows = _json(decision)["cookies"]  # type: ignore[index]
    assert [row["slot"] for row in rows] == [1, 2]  # type: ignore[index]
    assert [row["value"] for row in rows] == ["fresh", "fresh"]  # type: ignore[index]
    assert all(row["secure"] is True for row in rows)  # type: ignore[index]


def test_disallowed_changed_domain_is_entirely_untouched() -> None:
    raw = _row("disk", domain=".evil.com", unknown={"secret": [1]})
    decision = _decide(
        [raw],
        observation=(_cookie("fresh", domain=".evil.com"),),
        baseline=(_cookie("baseline", domain=".evil.com"),),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset()
    assert _baseline_values(decision.next_baseline) == _baseline_values(
        CookieJar((_cookie("fresh", domain=".evil.com"),))
    )


def test_partial_conflict_advances_accepted_identity_and_keeps_rejected_absence() -> None:
    sid = _cookie("fresh-sid")
    apisid = _cookie("fresh-api", name="APISID")
    decision = _decide(
        [_row("baseline-sid"), _row("sibling-api", name="APISID")],
        observation=(sid, apisid),
        baseline=(_cookie("baseline-sid"),),
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset({apisid.key})
    assert _baseline_values(decision.next_baseline) == {
        sid.key: ("fresh-sid", type(None), None, False, False)
    }


def test_expiry_type_only_minus_one_difference_matches_parent_numeric_equality() -> None:
    decision = _decide(
        [_row("same", expires=-1.0)],
        observation=(_cookie("same", expires=-1),),
        baseline=(_cookie("same", expires=-1.0),),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset()


def test_dated_minus_one_serialization_type_survives_another_dirty_field() -> None:
    decision = _decide(
        [_row("old", expires=-1.0)],
        observation=(_cookie("fresh", expires=-1),),
        baseline=(_cookie("old", expires=-1.0),),
    )
    assert decision.updated_rows == 1
    written = _json(decision)["cookies"][0]["expires"]  # type: ignore[index]
    assert type(written) is float
    assert written == -1.0
    next_expiry = next(iter(decision.next_baseline)).expires
    assert type(next_expiry) is int


def test_truthy_secure_type_only_difference_matches_parent_boolean_snapshot() -> None:
    observation = _cookie("same", secure=True)
    object.__setattr__(observation, "secure", "raw-secure-marker")
    decision = _decide(
        [_row("same", expires=-1, secure=True, httpOnly=False)],
        observation=(observation,),
        baseline=(_cookie("same", secure=True),),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset()


def test_same_site_valid_is_carried_and_invalid_missing_new_default_to_none() -> None:
    observation = (
        _cookie("fresh-valid"),
        _cookie("fresh-invalid", name="APISID"),
        _cookie("fresh-missing", name="SAPISID"),
        _cookie("fresh-new", name="SSID"),
    )
    baseline = (
        _cookie("old-valid"),
        _cookie("old-invalid", name="APISID"),
        _cookie("old-missing", name="SAPISID"),
    )
    decision = _decide(
        [
            _row("old-valid", sameSite="Lax"),
            _row("old-invalid", name="APISID", sameSite="bogus"),
            _row("old-missing", name="SAPISID"),
        ],
        observation=observation,
        baseline=baseline,
    )
    assert decision.updated_rows == 4
    rows = _json(decision)["cookies"]  # type: ignore[index]
    assert [row["sameSite"] for row in rows] == ["Lax", "None", "None", "None"]  # type: ignore[index]


def _recovery(values: set[str | None], *, domain: str = ".google.com") -> RecoveryObservation:
    return RecoveryObservation({CookieIdentity("__Secure-1PSIDTS", domain, "/"): frozenset(values)})


def test_recovery_observation_never_makes_an_unchanged_target_dirty() -> None:
    target = _cookie("same", name="__Secure-1PSIDTS")
    decision = _decide(
        [
            _row("same", name="__Secure-1PSIDTS", unknown="winner"),
            _row("duplicate", name="__Secure-1PSIDTS", unknown="sibling"),
        ],
        observation=(target,),
        baseline=(target,),
        recovery=_recovery({"same", "duplicate"}),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset()


def test_recovery_replaces_and_collapses_observed_duplicates_with_canonical_winner() -> None:
    target = _cookie("fresh", name="__Secure-1PSIDTS", expires=9, secure=True, http_only=True)
    decision = _decide(
        [
            _row("observed-a", name="__Secure-1PSIDTS", sameSite="Strict", unknown="drop"),
            "opaque",
            _row("observed-b", name="__Secure-1PSIDTS", unknown="drop-too"),
            _row("path-sibling", name="__Secure-1PSIDTS", path="/other", unknown="keep"),
        ],
        observation=(target,),
        baseline=(_cookie("baseline", name="__Secure-1PSIDTS"),),
        recovery=_recovery({"observed-a", "observed-b"}, domain="google.com"),
    )
    assert decision.updated_rows == 2
    assert decision.rejected == frozenset()
    assert _json(decision)["cookies"] == [  # type: ignore[index]
        {
            "name": "__Secure-1PSIDTS",
            "value": "fresh",
            "domain": ".google.com",
            "path": "/",
            "expires": 9,
            "httpOnly": True,
            "secure": True,
            "sameSite": "Strict",
        },
        "opaque",
        {
            "name": "__Secure-1PSIDTS",
            "value": "path-sibling",
            "domain": ".google.com",
            "path": "/other",
            "unknown": "keep",
        },
    ]


def test_recovery_mixed_replacement_and_conflict_changes_document_but_rejects() -> None:
    target = _cookie("fresh", name="__Secure-1PSIDTS")
    decision = _decide(
        [
            _row("observed", name="__Secure-1PSIDTS", unknown="drop"),
            _row("sibling", name="__Secure-1PSIDTS", unknown="keep"),
        ],
        observation=(target,),
        baseline=(_cookie("baseline", name="__Secure-1PSIDTS"),),
        recovery=_recovery({"observed"}),
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset({target.key})
    assert _json(decision)["cookies"] == [  # type: ignore[index]
        {
            "name": "__Secure-1PSIDTS",
            "value": "fresh",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": False,
            "sameSite": "None",
        },
        {
            "name": "__Secure-1PSIDTS",
            "value": "sibling",
            "domain": ".google.com",
            "path": "/",
            "unknown": "keep",
        },
    ]
    assert _baseline_values(decision.next_baseline) == _baseline_values(
        CookieJar((_cookie("baseline", name="__Secure-1PSIDTS"),))
    )


@pytest.mark.parametrize(
    ("rows", "expected_rows", "updated", "rejected"),
    [
        (
            [],
            [
                {
                    "name": "__Secure-1PSIDTS",
                    "value": "fresh",
                    "domain": ".google.com",
                    "path": "/",
                    "expires": -1,
                    "httpOnly": False,
                    "secure": False,
                    "sameSite": "None",
                }
            ],
            1,
            False,
        ),
        ([_row("sibling", name="__Secure-1PSIDTS", unknown="keep")], None, 0, True),
        ([_row(None, name="__Secure-1PSIDTS", unknown="drop")], None, 0, True),
    ],
    ids=["no-row-appends", "conflict-only", "unusable-without-none-conflicts"],
)
def test_recovery_no_row_conflict_and_unusable_truth_table(
    rows: list[object],
    expected_rows: list[object] | None,
    updated: int,
    rejected: bool,
) -> None:
    target = _cookie("fresh", name="__Secure-1PSIDTS")
    decision = _decide(
        rows,
        observation=(target,),
        baseline=(_cookie("baseline", name="__Secure-1PSIDTS"),),
        recovery=_recovery({"observed"}),
    )
    assert decision.updated_rows == updated
    assert decision.rejected == (frozenset({target.key}) if rejected else frozenset())
    if expected_rows is None:
        assert decision.document is None
    else:
        assert _json(decision)["cookies"] == expected_rows  # type: ignore[index]


def test_recovery_unusable_row_is_replaceable_only_when_none_was_observed() -> None:
    target = _cookie("fresh", name="__Secure-3PSIDTS")
    recovery = RecoveryObservation(
        {CookieIdentity("__Secure-3PSIDTS", ".google.com", "/"): frozenset({None})}
    )
    decision = _decide(
        [_row("", name="__Secure-3PSIDTS", unknown="drop")],
        observation=(target,),
        baseline=(_cookie("baseline", name="__Secure-3PSIDTS"),),
        recovery=recovery,
    )
    assert decision.updated_rows == 1
    assert decision.rejected == frozenset()
    assert _json(decision)["cookies"][0] == {  # type: ignore[index]
        "name": "__Secure-3PSIDTS",
        "value": "fresh",
        "domain": ".google.com",
        "path": "/",
        "expires": -1,
        "httpOnly": False,
        "secure": False,
        "sameSite": "None",
    }


def test_recovery_without_values_delegates_to_ordinary_cas() -> None:
    target = _cookie("fresh", name="__Secure-1PSIDTS", secure=True)
    decision = _decide(
        [_row("baseline", name="__Secure-1PSIDTS", unknown="keep")],
        observation=(target,),
        baseline=(_cookie("baseline", name="__Secure-1PSIDTS"),),
        recovery=RecoveryObservation({}),
    )
    assert decision.updated_rows == 1
    assert _json(decision)["cookies"][0]["unknown"] == "keep"  # type: ignore[index]


def test_legacy_never_deletes_and_secure_http_only_only_changes_are_noops() -> None:
    stored = _document(
        [
            _row(
                "same",
                expires=-1,
                secure=False,
                httpOnly=False,
                sameSite="Strict",
                unknown="keep",
            ),
            _row("absent", name="APISID", unknown="keep-absent"),
        ]
    )
    decision = decide_legacy_cookie_overlay(
        stored=stored,
        observation=CookieJar((_cookie("same", secure=True, http_only=True),)),
    )
    assert decision.updated_rows == 0
    assert decision.document is None
    assert decision.rejected == frozenset()


def test_legacy_dirty_update_and_append_preserve_raw_document_and_order() -> None:
    stored = _document(
        [
            "opaque",
            _row("old", path="", expires=-1, sameSite="Lax", unknown={"keep": [1]}),
            {"malformed": "row"},
        ]
    )
    decision = decide_legacy_cookie_overlay(
        stored=stored,
        observation=CookieJar(
            (
                _cookie("ignored-first"),
                _cookie("fresh", expires=7, secure=True, http_only=True),
                _cookie("new", name="APISID"),
                _cookie("blocked", name="TRACKER", domain=".evil.com"),
            )
        ),
    )
    assert decision.updated_rows == 2
    assert decision.rejected == frozenset()
    assert _json(decision) == {
        "cookies": [
            "opaque",
            {
                "name": "SID",
                "value": "fresh",
                "domain": ".google.com",
                "path": "/",
                "expires": 7,
                "sameSite": "Lax",
                "unknown": {"keep": [1]},
                "secure": True,
                "httpOnly": True,
            },
            {"malformed": "row"},
            {
                "name": "APISID",
                "value": "new",
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": False,
                "sameSite": "None",
            },
        ],
        "origins": [{"origin": "https://preserved.example", "localStorage": []}],
        "notebooklm": {"account": {"authuser": 2}},
        "futureRoot": {"nested": ["preserved"]},
    }


def test_legacy_dotted_bare_prefers_candidate_that_differs_from_each_row() -> None:
    decision = decide_legacy_cookie_overlay(
        stored=_document([_row("equal", domain=".google.com", sameSite="invalid")]),
        observation=CookieJar(
            (
                _cookie("equal", domain=".google.com"),
                _cookie("changed", domain="google.com"),
            )
        ),
    )
    assert decision.updated_rows == 1
    assert _json(decision)["cookies"] == [  # type: ignore[index]
        {
            "name": "SID",
            "value": "changed",
            "domain": ".google.com",
            "path": "/",
            "sameSite": "None",
            "expires": -1,
            "secure": False,
            "httpOnly": False,
        }
    ]


def test_legacy_two_changed_dotted_bare_tie_keeps_fixed_hash_seed_parent_winner() -> None:
    source = """
from notebooklm._auth.cookie_merge import decide_legacy_cookie_overlay
from notebooklm._auth.cookie_types import Cookie, CookieJar
from notebooklm._auth.profile_document import ProfileDocument

decision = decide_legacy_cookie_overlay(
    stored=ProfileDocument.decode(
        {"cookies": [{"name": "SID", "value": "disk", "domain": ".google.com", "path": "/", "expires": -1}]}
    ),
    observation=CookieJar(
        (
            Cookie("SID", ".google.com", "/", "dotted"),
            Cookie("SID", "google.com", "/", "bare"),
        )
    ),
)
assert decision.document is not None
print(decision.document.to_json()["cookies"][0]["value"])
"""
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = "0"
    completed = subprocess.run(
        [sys.executable, "-c", source],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.stdout == "bare\n"
    assert completed.stderr == ""


def test_pure_calls_do_not_log_warn_or_touch_io(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def forbidden_open(*args: object, **kwargs: object) -> None:
        raise AssertionError("pure merge touched filesystem")

    monkeypatch.setattr(builtins, "open", forbidden_open)
    with warnings.catch_warnings(record=True) as caught, caplog.at_level(logging.DEBUG):
        decision = _decide(
            [_row("baseline")],
            observation=(_cookie("fresh"),),
            baseline=(_cookie("baseline"),),
        )
    assert decision.updated_rows == 1
    assert caught == []
    assert caplog.records == []


def test_decisions_do_not_mutate_or_alias_any_input() -> None:
    raw_row = _row("baseline", nested={"values": [1]})
    payload = {"cookies": [raw_row], "origins": []}
    stored = ProfileDocument.decode(payload)
    observation_items = [_cookie("fresh")]
    baseline_items = [_cookie("baseline")]
    recovery_values = {"observed"}
    recovery_source = {CookieIdentity("__Secure-1PSIDTS", ".google.com", "/"): recovery_values}
    decision = decide_cookie_merge(
        stored=stored,
        observation=CookieJar(observation_items),
        baseline=CookieJar(baseline_items),
        recovery_observation=RecoveryObservation(recovery_source),  # type: ignore[arg-type]
    )
    raw_row["value"] = "mutated"
    payload["cookies"] = []
    observation_items.clear()
    baseline_items.clear()
    recovery_values.add("mutated")
    recovery_source.clear()
    assert decision.updated_rows == 1
    assert _json(decision)["cookies"][0]["value"] == "fresh"  # type: ignore[index]
    assert stored.to_json()["cookies"][0]["value"] == "baseline"  # type: ignore[index]
    assert len(decision.next_baseline) == 1


def test_raw_mapping_rows_are_accepted_without_mutating_read_only_input() -> None:
    raw: Mapping[str, object] = MappingProxyType(_row("baseline", unknown="keep"))
    decision = _decide([raw], observation=(_cookie("fresh"),), baseline=(_cookie("baseline"),))
    assert decision.updated_rows == 1
    assert _json(decision)["cookies"][0]["unknown"] == "keep"  # type: ignore[index]


def test_legacy_storage_adapter_preserves_null_values_and_raw_secure_markers() -> None:
    jar = httpx.Cookies()
    jar.jar.set_cookie(_http_cookie("SID", None, secure="update-secure-marker"))
    jar.jar.set_cookie(_http_cookie("APISID", None, secure="append-secure-marker"))
    storage_data: dict[str, object] = {
        "cookies": [_row("old", expires=-1, unknown="keep")],
        "origins": [{"origin": "https://preserved.example"}],
    }

    updated = storage._merge_cookies_legacy(jar, storage_data)  # noqa: SLF001

    assert updated == 2
    assert storage_data == {
        "cookies": [
            {
                "name": "SID",
                "value": None,
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "unknown": "keep",
                "secure": "update-secure-marker",
                "httpOnly": False,
                "sameSite": "None",
            },
            {
                "name": "APISID",
                "value": None,
                "domain": ".google.com",
                "path": "/",
                "expires": -1,
                "httpOnly": False,
                "secure": "append-secure-marker",
                "sameSite": "None",
            },
        ],
        "origins": [{"origin": "https://preserved.example"}],
    }


def test_snapshot_storage_adapter_preserves_raw_secure_marker() -> None:
    jar = httpx.Cookies()
    jar.jar.set_cookie(_http_cookie("SID", "fresh", secure="cas-secure-marker"))
    storage_data: dict[str, object] = {
        "cookies": [_row("old", expires=-1, unknown="keep")],
        "origins": [],
    }
    baseline: storage.CookieSnapshot = {
        storage.CookieSnapshotKey("SID", ".google.com", "/"): storage.CookieSnapshotValue(
            "old", None, False, False
        )
    }

    updated, rejected = storage._merge_cookies_with_snapshot(  # noqa: SLF001
        jar, storage_data, baseline
    )

    assert updated == 1
    assert rejected == frozenset()
    assert storage_data["cookies"] == [
        {
            "name": "SID",
            "value": "fresh",
            "domain": ".google.com",
            "path": "/",
            "expires": -1,
            "unknown": "keep",
            "secure": "cas-secure-marker",
            "httpOnly": False,
            "sameSite": "None",
        }
    ]


def test_snapshot_storage_adapter_keeps_none_as_a_deletion_not_a_null_delta() -> None:
    jar = httpx.Cookies()
    jar.jar.set_cookie(_http_cookie("SID", None))
    storage_data: dict[str, object] = {"cookies": [_row("old")], "origins": []}
    key = storage.CookieSnapshotKey("SID", ".google.com", "/")
    baseline: storage.CookieSnapshot = {key: storage.CookieSnapshotValue("old", None, False, False)}

    updated, rejected = storage._merge_cookies_with_snapshot(  # noqa: SLF001
        jar, storage_data, baseline
    )

    assert updated == 1
    assert rejected == frozenset()
    assert storage_data == {"cookies": [], "origins": []}


def test_next_baseline_no_change_takes_forward_compatible_same_site_from_final_row() -> None:
    identity = CookieIdentity("SID", ".google.com", "/")
    decision = _decide(
        [_row("same", sameSite="FuturePolicy")],
        observation=(_cookie("same"),),
        baseline=(_cookie("same", same_site="old-baseline"),),
    )

    assert decision.document is None
    assert _baseline_same_site(decision.next_baseline) == {identity: "FuturePolicy"}


def test_next_baseline_accepted_uses_final_row_and_rejected_keeps_exact_old_cookie() -> None:
    sid = CookieIdentity("SID", ".google.com", "/")
    apisid = CookieIdentity("APISID", ".google.com", "/")
    decision = _decide(
        [
            _row("concurrent", sameSite="disk-concurrent"),
            _row("old-api", name="APISID", sameSite="Lax"),
        ],
        observation=(
            _cookie("new-sid", expires=22, secure=True),
            _cookie("new-api", name="APISID", expires=33),
        ),
        baseline=(
            _cookie("old-sid", expires=11, same_site="rejected-old"),
            _cookie("old-api", name="APISID", same_site="api-old"),
        ),
    )

    assert decision.rejected == frozenset({sid})
    projected = {cookie.key: cookie for cookie in decision.next_baseline}
    assert (
        projected[sid].value,
        projected[sid].expires,
        projected[sid].secure,
        projected[sid].same_site,
    ) == ("old-sid", 11, False, "rejected-old")
    assert (projected[apisid].value, projected[apisid].same_site) == (
        "new-api",
        "Lax",
    )


def test_next_baseline_final_selector_skips_malformed_first_duplicate() -> None:
    identity = CookieIdentity("SID", ".google.com", "/")
    decision = _decide(
        [
            _row("ignored", expires="never", sameSite="wrong"),
            _row("same", expires="41.9", sameSite="later-survivor"),
        ],
        observation=(_cookie("same", expires=41),),
        baseline=(_cookie("same", expires=41),),
    )

    assert _baseline_same_site(decision.next_baseline) == {identity: "later-survivor"}


def test_next_baseline_final_selector_matches_bare_and_dotted_domains() -> None:
    observed = _cookie("same", domain="google.com")
    decision = _decide(
        [_row("same", domain=".google.com", sameSite="variant-carried")],
        observation=(observed,),
        baseline=(observed,),
    )

    assert next(iter(decision.next_baseline)).same_site == "variant-carried"
