from collections.abc import Iterator
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NamedTuple, TypeAlias
import httpx

CookieKey: TypeAlias = tuple[str, str, str]
DomainCookieMap: TypeAlias = dict[CookieKey, str]
FlatCookieMap: TypeAlias = dict[str, str]
# ``CookieInput`` also accepts the legacy ``(name, domain) -> value`` shape that
# pre-#369 callers constructed by hand; :func:`normalize_cookie_map` widens
# those entries to ``(name, domain, "/")`` so the rest of the pipeline sees a
# uniform path-aware shape.
LegacyDomainCookieMap: TypeAlias = dict[tuple[str, str], str]
CookieInput: TypeAlias = DomainCookieMap | LegacyDomainCookieMap | FlatCookieMap

class CookieSnapshotKey(NamedTuple):
    """Path-aware cookie identity used by the snapshot/delta save machinery."""
    name: str
    domain: str
    path: str

class CookieSnapshotValue(NamedTuple):
    """Snapshot value tuple: ``(value, expires, secure, http_only)``."""
    value: str
    expires: int | None
    secure: bool
    http_only: bool

CookieSnapshot: TypeAlias = dict[CookieSnapshotKey, CookieSnapshotValue]

@dataclass(frozen=True)
class CookieSaveResult:
    """Detailed result for callers that need to maintain a save baseline."""
    ok: bool
    cas_rejected_keys: frozenset[CookieSnapshotKey] = frozenset()

# Moving AuthTokens out later, because AuthTokens has from_storage which depends on everything.
# Wait, AuthTokens is the core data structure. We can keep from_storage in a loader module.
