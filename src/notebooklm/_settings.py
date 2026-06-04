"""User settings API."""

import logging
from collections.abc import Sequence
from typing import Any

from ._runtime.contracts import RpcCaller
from .rpc import RPCMethod, safe_index
from .types import AccountLimits, AccountTier

logger = logging.getLogger(__name__)


_ACCOUNT_LIMITS_PATH = (0, 1)
_NOTEBOOK_LIMIT_INDEX = 1
_SOURCE_LIMIT_INDEX = 2
_TIER_PREFIX = "NOTEBOOKLM_TIER_"
_TIER_PLAN_NAMES = {
    "NOTEBOOKLM_TIER_STANDARD": "Standard",
    "NOTEBOOKLM_TIER_PLUS": "Google AI Plus",
    "NOTEBOOKLM_TIER_PRO": "Google AI Pro",
    "NOTEBOOKLM_TIER_PRO_DASHER_END_USER": "Google Workspace Pro",
    "NOTEBOOKLM_TIER_ULTRA": "Google AI Ultra",
}


def build_get_user_settings_params() -> list[Any]:
    """Build GET_USER_SETTINGS params without sharing a mutable list."""
    return [
        None,
        [1, None, None, None, None, None, None, None, None, None, [1]],
    ]


def build_get_user_tier_params() -> list[Any]:
    """Build GET_USER_TIER params for the NotebookLM homepage context."""
    return [
        [
            [
                [None, "1", 627],
                [None, None, None, None, None, None, None, None, None, [None, None, 2]],
                1,
            ]
        ]
    ]


def _extract_language(
    data: list | None,
    required_prefix: Sequence[int],
    optional_tail: Sequence[int],
    *,
    method_id: str | int | None,
    source: str,
) -> str | None:
    """Extract the output-language code from a settings RPC response.

    The descent is split into two regimes, per ADR-0011 (schema-validation
    policy) and the ``_notebooks._extract_suggested_topics`` precedent for a
    routinely-optional trailing slot:

    1. ``required_prefix`` walks the *always-present* settings envelope down to
       the settings-flags block (``result[0][2]`` for GET, ``result[2]`` for
       SET). This block is structurally mandatory in every healthy response, so
       it goes through :func:`safe_index`: genuine schema drift in the envelope
       raises :class:`UnknownRPCMethodError` rather than silently degrading to
       ``None``.
    2. ``optional_tail`` walks the *optional* language slot inside that block
       (index ``[4]``, then the ``[0]`` unwrap of its ``["code"]`` wrapper). A
       user who never set a language legitimately has an empty/absent language
       slot, and that absence is **not distinguishable from drift at that exact
       position** — a trailing-optional element omitted when unset looks
       identical to a block that drifted shorter. So the tail uses a plain
       bounded guard that degrades to ``None`` on any absence/empty/non-list,
       preserving the optional-language contract (must return ``None``, never
       raise).

    Args:
        data: The nested list structure to extract from.
        required_prefix: Indices descending the mandatory settings envelope.
        optional_tail: Indices descending the optional language slot.
        method_id: RPC method ID, threaded into :func:`safe_index` diagnostics.
        source: Caller label for :func:`safe_index` drift diagnostics.

    Returns:
        The language code, or ``None`` when the language is unset/empty. Raises
        :class:`UnknownRPCMethodError` only on genuine envelope drift.
    """
    block = safe_index(data, *required_prefix, method_id=method_id, source=source)
    result: Any = block
    for idx in optional_tail:
        # Bound-check both ends: ``idx >= len`` guards the trailing-optional
        # absence; ``idx < 0`` guards against a negative index silently
        # wrapping to Python's from-the-end semantics for any future caller
        # (today's tails are hardcoded non-negative tuples).
        if not isinstance(result, list) or not 0 <= idx < len(result):
            return None
        result = result[idx]
    return result or None


def _extract_nested_list(data: list | None, path: Sequence[int]) -> list[Any] | None:
    """Extract a nested list by following an index path."""
    result: Any = data
    try:
        for idx in path:
            if not isinstance(result, list):
                return None
            result = result[idx]
    except IndexError:
        return None
    return result if isinstance(result, list) else None


def _positive_int(value: Any) -> int | None:
    """Return value only when it is a positive int, excluding bools."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def extract_account_limits(data: list | None) -> AccountLimits:
    """Extract account-level limits from GET_USER_SETTINGS response data."""
    limits = _extract_nested_list(data, _ACCOUNT_LIMITS_PATH)
    if limits is None:
        return AccountLimits()

    raw_limits = tuple(limits)
    notebook_limit = (
        _positive_int(limits[_NOTEBOOK_LIMIT_INDEX])
        if len(limits) > _NOTEBOOK_LIMIT_INDEX
        else None
    )
    source_limit = (
        _positive_int(limits[_SOURCE_LIMIT_INDEX]) if len(limits) > _SOURCE_LIMIT_INDEX else None
    )
    return AccountLimits(
        notebook_limit=notebook_limit,
        source_limit=source_limit,
        raw_limits=raw_limits,
    )


def _find_tier_string(value: Any) -> str | None:
    """Find the first NotebookLM tier string in a nested response."""
    stack = [value]
    while stack:
        item = stack.pop()
        if isinstance(item, str) and item.startswith(_TIER_PREFIX):
            return item
        if isinstance(item, list):
            stack.extend(reversed(item))
    return None


def extract_account_tier(data: list | None) -> AccountTier:
    """Extract the NotebookLM subscription tier from GET_USER_TIER response data."""
    tier = _find_tier_string(data)
    return AccountTier(tier=tier, plan_name=_TIER_PLAN_NAMES.get(tier) if tier else None)


class SettingsAPI:
    """Operations on NotebookLM user settings.

    Provides methods for managing global user settings like output language.

    Usage:
        async with NotebookLMClient.from_storage() as client:
            lang = await client.settings.get_output_language()
            await client.settings.set_output_language("zh_Hans")
    """

    # Response paths for extracting the language code from settings RPC
    # responses, split into a mandatory envelope prefix (routed through
    # ``safe_index`` — raises on drift) and an optional language tail (plain
    # guard — degrades to ``None`` when the user has no language set). See
    # ``_extract_language`` for the ADR-0011 rationale behind the split.
    #
    # SET_USER_SETTINGS shape: result[2][4][0]   (flags block at result[2])
    _SET_LANGUAGE_PREFIX = (2,)
    _SET_LANGUAGE_TAIL = (4, 0)
    # GET_USER_SETTINGS shape: result[0][2][4][0] (flags block at result[0][2])
    _GET_SETTINGS_PREFIX = (0, 2)
    _GET_SETTINGS_TAIL = (4, 0)

    def __init__(self, rpc: RpcCaller) -> None:
        """Initialize the settings API.

        Args:
            rpc: RPC dispatch surface (typically the shared client session).
        """
        self._rpc = rpc

    async def set_output_language(self, language: str) -> str | None:
        """Set the output language for artifact generation.

        This is a global setting that affects all notebooks in your account.

        Note: Use get_output_language() to read the current setting.
        Empty strings are rejected (they would reset to default, not read current).

        Args:
            language: Language code (e.g., "en", "zh_Hans", "ja").
                     Must be a non-empty valid language code.

        Returns:
            The language that was set, or None if the response couldn't be parsed.
        """
        if not language:
            logger.warning(
                "Empty string not supported - use get_output_language() to read the current setting. "
                "Passing empty string to the API would reset the language to default, not read it."
            )
            return None

        logger.debug("Setting output language: %s", language)

        # Params structure: [[[null,[[null,null,null,null,["language_code"]]]]]]
        params = [[[None, [[None, None, None, None, [language]]]]]]

        result = await self._rpc.rpc_call(
            RPCMethod.SET_USER_SETTINGS,
            params,
            source_path="/",
        )

        current_language = _extract_language(
            result,
            self._SET_LANGUAGE_PREFIX,
            self._SET_LANGUAGE_TAIL,
            method_id=RPCMethod.SET_USER_SETTINGS.value,
            source="_settings.set_output_language",
        )
        self._log_language_result(current_language, "Output language is now")
        return current_language

    async def get_output_language(self) -> str | None:
        """Get the current output language setting.

        Fetches user settings from the server and extracts the language code.

        Returns:
            The current language code (e.g., "en", "ja", "zh_Hans"),
            or None if not set or couldn't be parsed.
        """
        logger.debug("Fetching user settings to get output language")

        result = await self._rpc.rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            build_get_user_settings_params(),
            source_path="/",
        )

        current_language = _extract_language(
            result,
            self._GET_SETTINGS_PREFIX,
            self._GET_SETTINGS_TAIL,
            method_id=RPCMethod.GET_USER_SETTINGS.value,
            source="_settings.get_output_language",
        )
        self._log_language_result(current_language, "Current output language")
        return current_language

    async def get_account_limits(self) -> AccountLimits:
        """Get account-level limits advertised by NotebookLM user settings.

        Returns:
            AccountLimits with parsed notebook/source limits when present.
        """
        logger.debug("Fetching user settings to get account limits")

        result = await self._rpc.rpc_call(
            RPCMethod.GET_USER_SETTINGS,
            build_get_user_settings_params(),
            source_path="/",
        )

        limits = extract_account_limits(result)
        if limits.notebook_limit is not None:
            logger.debug("Notebook limit from user settings: %s", limits.notebook_limit)
        else:
            logger.debug("Could not parse account limits from response")
        return limits

    async def get_account_tier(self) -> AccountTier:
        """Get the NotebookLM subscription tier for the current account.

        Returns:
            AccountTier with the raw tier string and a friendly plan name when known.
        """
        logger.debug("Fetching user tier")

        result = await self._rpc.rpc_call(
            RPCMethod.GET_USER_TIER,
            build_get_user_tier_params(),
            source_path="/",
        )

        tier = extract_account_tier(result)
        if tier.tier:
            logger.debug("NotebookLM account tier: %s", tier.tier)
        else:
            logger.debug("Could not parse account tier from response")
        return tier

    def _log_language_result(self, language: str | None, success_prefix: str) -> None:
        """Log the result of a language operation."""
        if language:
            logger.debug("%s: %s", success_prefix, language)
        else:
            logger.debug("Could not parse language from response")
