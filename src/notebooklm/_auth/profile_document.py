"""Lossless immutable profile-document value and copy-on-write operations."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from .cookie_types import CookieJar
from .profile_account import (
    AccountView,
    ProfileAccount,
    parse_domain_selection,
    parse_profile_account,
)


class _FrozenList(tuple[object, ...]):
    """Internal marker that preserves JSON list identity while preventing mutation."""


def _freeze_json(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({deepcopy(key): _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return _FrozenList(_freeze_json(item) for item in value)
    return deepcopy(value)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {deepcopy(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, _FrozenList):
        return [_thaw_json(item) for item in value]
    return deepcopy(value)


class _ProfileDocumentStructureError(ValueError):
    """A bounded, value-free description of an invalid document container."""

    field: Literal["root", "cookies"]
    reason: Literal["not_object", "not_list"]

    def __init__(
        self,
        *,
        field: Literal["root", "cookies"],
        reason: Literal["not_object", "not_list"],
    ) -> None:
        self.field = field
        self.reason = reason
        if field == "root":
            message = "profile document root must be an object"
        else:
            message = "profile document cookies must be a list"
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class CookieRowPatch:
    index: int
    replacement: object = field(repr=False)


@dataclass(frozen=True, slots=True, init=False, repr=False, eq=False)
class ProfileDocument:
    """An isolated JSON-tree snapshot with lossless copy-on-write updates."""

    _payload: Mapping[str, object] = field(repr=False)

    def __init__(self) -> None:
        raise TypeError("ProfileDocument must be created with decode() or empty()")

    @classmethod
    def _from_snapshot(cls, payload: Mapping[str, object]) -> ProfileDocument:
        document = object.__new__(cls)
        frozen = _freeze_json(payload)
        if not isinstance(frozen, Mapping):
            raise AssertionError("profile document snapshot must be a mapping")
        object.__setattr__(document, "_payload", frozen)
        return document

    @classmethod
    def decode(cls, payload: object) -> ProfileDocument:
        """Capture an independent snapshot without choosing corruption policy."""
        if not isinstance(payload, Mapping):
            raise _ProfileDocumentStructureError(field="root", reason="not_object")
        return cls._from_snapshot(dict(payload))

    @classmethod
    def empty(cls) -> ProfileDocument:
        """Return the absent-file seed used by account-update operations."""
        return cls._from_snapshot({"cookies": [], "origins": []})

    def to_json(self) -> dict[str, object]:
        """Return a fresh mutable deep copy of the complete raw document."""
        return {key: _thaw_json(value) for key, value in self._payload.items()}

    def raw_cookie_rows(self) -> tuple[object, ...]:
        """Return isolated raw cookie-list slots without typed projection."""
        rows = self._payload.get("cookies")
        if not isinstance(rows, _FrozenList):
            raise _ProfileDocumentStructureError(field="cookies", reason="not_list")
        return tuple(_thaw_json(row) for row in rows)

    def cookies(self) -> CookieJar:
        """Return the deliberately tolerant and lossy canonical cookie view."""
        return CookieJar.from_storage_state(self.to_json())

    def account_for(self, view: AccountView) -> ProfileAccount | None:
        """Project only the raw ``notebooklm.account`` value for one view."""
        namespace = _thaw_json(self._payload.get("notebooklm"))
        account = namespace.get("account") if isinstance(namespace, Mapping) else None
        return parse_profile_account(account, view=view)

    def include_domains(self) -> frozenset[str]:
        """Return the normalized domain-selection labels from the raw namespace."""
        namespace = _thaw_json(self._payload.get("notebooklm"))
        return parse_domain_selection(namespace).include_domains

    def include_optional(self) -> bool:
        """Return the exact-true optional-domain projection from the raw namespace."""
        namespace = _thaw_json(self._payload.get("notebooklm"))
        return parse_domain_selection(namespace).include_optional

    def replace_cookie_rows(self, rows: Iterable[object]) -> ProfileDocument:
        """Replace raw cookie slots after eagerly isolating the supplied iterable."""
        replacements = [_thaw_json(_freeze_json(row)) for row in rows]
        snapshot = self.to_json()
        snapshot["cookies"] = replacements
        return self._from_snapshot(snapshot)

    def patch_cookie_rows(self, patches: Iterable[CookieRowPatch]) -> ProfileDocument:
        """Atomically replace distinct, valid raw cookie-list slots."""
        rows = self.raw_cookie_rows()
        materialized = tuple(patches)
        seen: set[int] = set()
        for patch in materialized:
            index = patch.index
            if type(index) is not int:
                raise TypeError("cookie row patch index must be an integer")
            if index < 0 or index >= len(rows):
                raise IndexError("cookie row patch index is out of range")
            if index in seen:
                raise ValueError("cookie row patch indices must be unique")
            seen.add(index)

        updated = list(rows)
        for patch in materialized:
            updated[patch.index] = patch.replacement
        return self.replace_cookie_rows(updated)

    def with_namespace(self, namespace: Mapping[str, object] | None) -> ProfileDocument:
        """Replace, preserve-empty, or remove the raw ``notebooklm`` namespace."""
        snapshot = self.to_json()
        if namespace is None:
            snapshot.pop("notebooklm", None)
        else:
            snapshot["notebooklm"] = namespace
        return self._from_snapshot(snapshot)
