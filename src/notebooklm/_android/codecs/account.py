"""Strict projection for the exact Android account response."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ...exceptions import DecodingError


@dataclass(frozen=True)
class AndroidAccount:
    """Evidence-bounded account flags returned by the Android backend."""

    accepted_tos: bool
    opted_in_to_marketing_emails: bool
    is_eea_user: bool
    is_premium_user: bool


def decode_account(response: Any, *, method_id: str) -> AndroidAccount:
    """Project all evidenced leaves, rejecting absent required message blocks."""

    if not response.HasField("account"):
        raise DecodingError(
            "Android GetOrCreateAccount response omitted account",
            method_id=method_id,
        )
    account = response.account
    if not account.HasField("user_info"):
        raise DecodingError(
            "Android GetOrCreateAccount response omitted user info",
            method_id=method_id,
        )
    if not account.HasField("premium_user_info"):
        raise DecodingError(
            "Android GetOrCreateAccount response omitted premium user info",
            method_id=method_id,
        )
    return AndroidAccount(
        accepted_tos=account.user_info.accepted_tos,
        opted_in_to_marketing_emails=account.user_info.opted_in_to_marketing_emails,
        is_eea_user=account.user_info.is_eea_user,
        is_premium_user=account.premium_user_info.is_premium_user,
    )


__all__ = ["AndroidAccount", "decode_account"]
