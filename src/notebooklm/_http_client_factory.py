"""Private, instance-owned HTTP construction overrides for transfer owners."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx as httpx_module


@dataclass(frozen=True)
class HttpClientFactories:
    """Override construction without changing transport selection or URL policy.

    The transport kind remains the production resolver's decision. In particular,
    an injected HTTPX factory must not select the buffered curl download branch
    merely because its callable has a different identity from ``AsyncClient``.
    """

    httpx: Callable[..., Any] | None = None
    curl_cffi: Callable[..., Any] | None = None

    def select(self, default: Callable[..., Any]) -> Callable[..., Any]:
        override = self.httpx if default is httpx_module.AsyncClient else self.curl_cffi
        return override if override is not None else default

    def create(self, **kwargs: Any) -> Any:
        from ._curl_cffi_transport import resolve_transport_factory

        return self.select(resolve_transport_factory())(**kwargs)

    def create_httpx(self, **kwargs: Any) -> Any:
        factory = self.httpx if self.httpx is not None else httpx_module.AsyncClient
        return factory(**kwargs)
