"""Transport-neutral per-hop credentials for guarded asset requests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field

import httpx


@dataclass(frozen=True)
class HopCredentials:
    """Credentials selected for one validated request hop.

    Cookie jars stay structured so each transport performs normal domain/path
    matching. Header credentials are also structured rather than installed on a
    client session, which lets the next policy decision remove them completely.
    """

    cookies: httpx.Cookies | None = None
    headers: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if any(name.lower() == "cookie" for name in self.headers):
            raise ValueError("HopCredentials.headers must not contain a flat Cookie header")


CredentialPolicy = Callable[[str], Awaitable[HopCredentials | None]]


__all__ = ["CredentialPolicy", "HopCredentials"]
