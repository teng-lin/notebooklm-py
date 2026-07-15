from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .base import ExternalKBConnector


_PROVIDERS: dict[str, type[ExternalKBConnector]] = {}


class ConnectorRegistry:
    @staticmethod
    def register(provider_type: str, connector_class: type[ExternalKBConnector]) -> None:
        _PROVIDERS[provider_type] = connector_class

    @staticmethod
    def create(provider_type: str, config: dict) -> ExternalKBConnector:
        cls = _PROVIDERS.get(provider_type)
        if cls is None:
            msg = f"Unknown provider type: {provider_type!r}. Available: {list(_PROVIDERS)}"
            raise ValueError(msg)
        return cls(config)

    @staticmethod
    def list_providers() -> list[str]:
        return list(_PROVIDERS)
