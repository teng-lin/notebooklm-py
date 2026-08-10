"""Direct tests for the dependency-bottom master-token value and codec."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import FrozenInstanceError, fields
from typing import Any, cast

import pytest

from notebooklm._auth.master_token import read_master_token
from notebooklm._auth.master_token_types import (
    MasterToken,
    _master_token_from_legacy_record,
    _master_token_to_legacy_record,
    _MasterTokenRecordError,
)

_MALFORMED = "master_token.json is malformed or an unsupported version."


class _Unrenderable:
    def __repr__(self) -> str:
        raise AssertionError("secret repr must not be evaluated")

    def __str__(self) -> str:
        raise AssertionError("secret str must not be evaluated")


class _MasterTokenSubclass(MasterToken):
    pass


def test_master_token_has_exact_frozen_redacted_dataclass_shape() -> None:
    token = MasterToken(email="person@example.com", android_id="android-1", secret="secret")

    assert [item.name for item in fields(MasterToken)] == ["email", "android_id", "secret"]
    assert MasterToken.__dataclass_params__.frozen is True
    assert MasterToken.__dataclass_params__.repr is False
    assert token.__dict__ == {
        "email": "person@example.com",
        "android_id": "android-1",
        "secret": "secret",
    }
    assert token == MasterToken(email="person@example.com", android_id="android-1", secret="secret")
    with pytest.raises(FrozenInstanceError):
        token.secret = "replacement"  # type: ignore[misc]


def test_repr_never_evaluates_or_discloses_secret() -> None:
    secret = cast(str, _Unrenderable())
    rendered = repr(MasterToken(email="person@example.com", android_id="android-1", secret=secret))

    assert rendered == (
        "MasterToken(email='person@example.com', android_id='android-1', secret=<redacted>)"
    )
    assert "<redacted>" in rendered


def test_codec_canonical_round_trip_and_fresh_mapping() -> None:
    record = {
        "version": 1,
        "email": "person@example.com",
        "android_id": "android-1",
        "master_token": "secret",
    }
    token = _master_token_from_legacy_record(record)

    assert token == MasterToken(email="person@example.com", android_id="android-1", secret="secret")
    first = _master_token_to_legacy_record(token)
    second = _master_token_to_legacy_record(token)
    assert first == record
    assert first is not second
    assert list(first) == ["version", "email", "android_id", "master_token"]
    assert first["master_token"] == "secret"


def test_decode_ignores_unknown_keys_but_projects_typed_value() -> None:
    record = {
        "version": 1,
        "email": "person@example.com",
        "android_id": "android-1",
        "master_token": "secret",
        "future": {"opaque": True},
    }

    token = _master_token_from_legacy_record(record)

    assert token == MasterToken(email="person@example.com", android_id="android-1", secret="secret")
    assert "future" not in _master_token_to_legacy_record(token)
    assert record["future"] == {"opaque": True}


@pytest.mark.parametrize("field", ["master_token", "email", "android_id"])
@pytest.mark.parametrize("falsy", [None, "", 0, False, [], {}])
def test_decode_rejects_missing_and_falsy_required_fields(field: str, falsy: object) -> None:
    record: dict[str, object] = {
        "version": 1,
        "email": "person@example.com",
        "android_id": "android-1",
        "master_token": "secret",
    }
    record[field] = falsy

    with pytest.raises(_MasterTokenRecordError, match="^" + _MALFORMED.replace(".", r"\.") + "$"):
        _master_token_from_legacy_record(record)

    del record[field]
    with pytest.raises(_MasterTokenRecordError) as captured:
        _master_token_from_legacy_record(record)
    assert str(captured.value) == _MALFORMED
    assert captured.value.args == (_MALFORMED,)


@pytest.mark.parametrize("root", [None, [], (), "record", 1, object()])
def test_decode_rejects_non_dict_roots_with_only_fixed_error(root: object) -> None:
    with pytest.raises(_MasterTokenRecordError) as captured:
        _master_token_from_legacy_record(root)

    assert str(captured.value) == _MALFORMED
    assert captured.value.args == (_MALFORMED,)
    assert captured.value.__dict__ == {}


def test_decode_preserves_truthy_non_string_values_without_coercion() -> None:
    email = ["person@example.com"]
    android_id = {"id": 7}
    secret = 123
    record: dict[str, Any] = {
        "version": 1,
        "email": email,
        "android_id": android_id,
        "master_token": secret,
    }

    token = _master_token_from_legacy_record(record)

    assert token.email is email
    assert token.android_id is android_id
    assert token.secret is secret
    assert _master_token_to_legacy_record(token)["master_token"] is secret


@pytest.mark.parametrize("version", [1, True, 1.0])
def test_decode_uses_ordinary_python_version_equality(version: object) -> None:
    token = _master_token_from_legacy_record(
        {
            "version": version,
            "email": "person@example.com",
            "android_id": "android-1",
            "master_token": "secret",
        }
    )
    assert token.secret == "secret"


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_decode_rejects_unequal_versions(version: object) -> None:
    with pytest.raises(_MasterTokenRecordError) as captured:
        _master_token_from_legacy_record(
            {
                "version": version,
                "email": "person@example.com",
                "android_id": "android-1",
                "master_token": "secret",
            }
        )
    assert str(captured.value) == _MALFORMED


@pytest.mark.parametrize(
    "candidate",
    [
        object(),
        {"secret": "must-not-render"},
        _MasterTokenSubclass(email="e", android_id="a", secret="s"),
    ],
)
def test_encode_rejects_non_exact_master_token_without_rendering(candidate: object) -> None:
    class _Candidate:
        def __repr__(self) -> str:
            raise AssertionError("candidate repr must not be evaluated")

    values = [candidate, _Candidate()]
    for value in values:
        with pytest.raises(TypeError) as captured:
            _master_token_to_legacy_record(cast(MasterToken, value))
        assert captured.value.args == ("token must be an exact MasterToken instance",)


def test_codec_does_not_mutate_inputs_or_use_effectful_facilities(monkeypatch) -> None:
    record = {
        "version": 1,
        "email": "person@example.com",
        "android_id": "android-1",
        "master_token": "secret",
    }
    original = dict(record)
    token = MasterToken(email="person@example.com", android_id="android-1", secret="secret")

    def forbidden(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("pure codec reached an effectful facility")

    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr(json, "loads", forbidden)
    monkeypatch.setattr(json, "dumps", forbidden)
    monkeypatch.setattr(asyncio, "create_task", forbidden)
    monkeypatch.setattr(threading, "Thread", forbidden)

    decoded = _master_token_from_legacy_record(record)
    encoded = _master_token_to_legacy_record(token)

    assert record == original
    assert decoded == token
    assert encoded == original
    assert token.__dict__ == {
        "email": "person@example.com",
        "android_id": "android-1",
        "secret": "secret",
    }


def test_legacy_public_reader_returns_captured_raw_dict_without_canonicalizing(tmp_path) -> None:
    path = tmp_path / "master_token.json"
    path.write_text(
        '{"version": true, "email": ["person@example.com"], '
        '"android_id": {"id": 7}, "master_token": 1, '
        '"unknown": {"keep": true}, "duplicate": "first", "duplicate": "last"}',
        encoding="utf-8",
    )

    record = read_master_token(path)

    assert record == {
        "version": True,
        "email": ["person@example.com"],
        "android_id": {"id": 7},
        "master_token": 1,
        "unknown": {"keep": True},
        "duplicate": "last",
    }
