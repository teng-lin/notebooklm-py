"""Dependency-bottom master-token value and legacy record codec."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class MasterTokenError(Exception):
    """The master token (or its exchange) was rejected — re-bootstrap needed.

    Raised for revoked/expired master tokens, gpsoauth failures, and a minted
    cookie jar missing the cookies the web client needs. Carries no secrets.
    """


MasterTokenError.__module__ = "notebooklm._auth.master_token"


@dataclass(frozen=True, repr=False)
class MasterToken:
    """An immutable master-token credential with a redacted secret."""

    email: str
    android_id: str
    secret: str = field(repr=False)

    def __repr__(self) -> str:
        return (
            f"MasterToken(email={self.email!r}, android_id={self.android_id!r}, secret=<redacted>)"
        )


_MASTER_TOKEN_RECORD_VERSION = 1


class _MasterTokenRecordError(ValueError):
    """A redacted legacy-record shape or version error."""


def _master_token_from_legacy_record(record: object) -> MasterToken:
    """Decode the historical version-1 mapping predicate without tightening it."""
    if not isinstance(record, dict):
        raise _MasterTokenRecordError("master_token.json is malformed or an unsupported version.")
    if record.get("version") != _MASTER_TOKEN_RECORD_VERSION:
        raise _MasterTokenRecordError("master_token.json is malformed or an unsupported version.")
    for key in ("master_token", "email", "android_id"):
        if not record.get(key):
            raise _MasterTokenRecordError(
                "master_token.json is malformed or an unsupported version."
            )
    return MasterToken(
        email=record["email"],
        android_id=record["android_id"],
        secret=record["master_token"],
    )


def _master_token_to_legacy_record(token: MasterToken) -> dict[str, Any]:
    """Encode a credential in the historical canonical insertion order."""
    if type(token) is not MasterToken:
        raise TypeError("token must be an exact MasterToken instance")
    return {
        "version": _MASTER_TOKEN_RECORD_VERSION,
        "email": token.email,
        "android_id": token.android_id,
        "master_token": token.secret,
    }
