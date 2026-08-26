"""Transport-neutral records and operation definitions for account settings."""

from __future__ import annotations

from dataclasses import dataclass

from ..._operations import CallPolicy, Operation, OperationDef


@dataclass(frozen=True, slots=True)
class AccountLimitsRecord:
    """Account quota facts decoded without exposing their web positions."""

    notebook_limit: int | None = None
    source_limit: int | None = None
    raw_limits: tuple[object, ...] = ()
    tier: int | None = None


@dataclass(frozen=True, slots=True)
class UserSettingsRecord:
    """One account settings row and its tolerant language projection."""

    limits: AccountLimitsRecord = AccountLimitsRecord()
    output_language: object | None = None


@dataclass(frozen=True, slots=True)
class SettingsGetInput:
    """Input for the account-routed settings read."""


@dataclass(frozen=True, slots=True)
class SettingsGetResult:
    """Combined settings result produced from one account RPC."""

    settings: UserSettingsRecord


@dataclass(frozen=True, slots=True)
class SettingsGetLimitsInput:
    """Input for the tolerant account-limit projection."""


@dataclass(frozen=True, slots=True)
class SettingsGetLimitsResult:
    """Account limits produced without decoding the optional language block."""

    limits: AccountLimitsRecord


@dataclass(frozen=True, slots=True)
class SettingsSetLanguageInput:
    """Requested global output-language code."""

    language: str


@dataclass(frozen=True, slots=True)
class SettingsSetLanguageResult:
    """Server-projected output language after mutation."""

    output_language: object | None


SETTINGS_GET_DEF: OperationDef[SettingsGetInput, SettingsGetResult] = OperationDef(
    Operation.SETTINGS_GET,
    CallPolicy.READ,
    SettingsGetInput,
    SettingsGetResult,
)
SETTINGS_GET_LIMITS_DEF: OperationDef[SettingsGetLimitsInput, SettingsGetLimitsResult] = (
    OperationDef(
        Operation.SETTINGS_GET_LIMITS,
        CallPolicy.READ,
        SettingsGetLimitsInput,
        SettingsGetLimitsResult,
    )
)
SETTINGS_SET_LANGUAGE_DEF: OperationDef[SettingsSetLanguageInput, SettingsSetLanguageResult] = (
    OperationDef(
        Operation.SETTINGS_SET_LANGUAGE,
        CallPolicy.MUTATION,
        SettingsSetLanguageInput,
        SettingsSetLanguageResult,
    )
)


__all__ = [
    "SETTINGS_GET_DEF",
    "SETTINGS_GET_LIMITS_DEF",
    "SETTINGS_SET_LANGUAGE_DEF",
    "AccountLimitsRecord",
    "SettingsGetInput",
    "SettingsGetLimitsInput",
    "SettingsGetLimitsResult",
    "SettingsGetResult",
    "SettingsSetLanguageInput",
    "SettingsSetLanguageResult",
    "UserSettingsRecord",
]
