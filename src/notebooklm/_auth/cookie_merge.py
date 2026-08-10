"""Pure cookie merge decisions over immutable auth profile values."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from . import cookie_policy as _cookie_policy
from . import cookie_semantics as _cookie_semantics
from .cookie_types import Cookie, CookieIdentity, CookieJar
from .profile_document import ProfileDocument

_VALID_SAME_SITE = frozenset({"Strict", "Lax", "None"})
_ConflictKind = Literal["existing", "new"]


@dataclass(frozen=True, slots=True, repr=False)
class RecoveryObservation:
    """Pre-recovery values grouped by path-aware cookie identity."""

    values_by_identity: Mapping[CookieIdentity, frozenset[str | None]] = field(repr=False)

    def __post_init__(self) -> None:
        try:
            frozen = {
                CookieIdentity(identity.name, identity.domain, identity.path): frozenset(values)
                for identity, values in self.values_by_identity.items()
            }
        except Exception:
            raise TypeError("recovery observation must map identities to value sets") from None
        object.__setattr__(self, "values_by_identity", MappingProxyType(frozen))


@dataclass(frozen=True, slots=True, repr=False)
class CookieMergeDecision:
    """Redacted immutable result of one pure cookie merge decision."""

    document: ProfileDocument | None
    next_baseline: CookieJar = field(repr=False)
    rejected: frozenset[CookieIdentity] = frozenset()
    updated_rows: int = 0
    _conflicts: tuple[tuple[CookieIdentity, _ConflictKind], ...] = field(
        default=(), repr=False, compare=False
    )

    def __post_init__(self) -> None:
        if type(self.updated_rows) is not int:
            raise TypeError("updated_rows must be an integer")
        if self.updated_rows < 0:
            raise ValueError("updated_rows must be nonnegative")
        if (self.document is None) is not (self.updated_rows == 0):
            raise ValueError("document presence must match updated_rows")
        try:
            baseline = CookieJar(tuple(self.next_baseline))
            rejected = frozenset(self.rejected)
            conflicts = tuple(self._conflicts)
            document = (
                None if self.document is None else ProfileDocument.decode(self.document.to_json())
            )
        except Exception:
            raise TypeError("cookie merge decision fields are invalid") from None
        object.__setattr__(self, "document", document)
        object.__setattr__(self, "next_baseline", baseline)
        object.__setattr__(self, "rejected", rejected)
        object.__setattr__(self, "_conflicts", conflicts)


def equivalent_identities(identity: CookieIdentity) -> frozenset[CookieIdentity]:
    """Return exact-path leading-dot/bare-domain equivalents."""
    alternate = identity.domain[1:] if identity.domain.startswith(".") else f".{identity.domain}"
    return frozenset((identity, CookieIdentity(identity.name, alternate, identity.path)))


def stored_cookie_identity(row: object) -> CookieIdentity | None:
    """Project a structurally usable identity from one raw storage row."""
    if not isinstance(row, Mapping):
        return None
    name = row.get("name")
    domain = row.get("domain", "")
    if not isinstance(name, str) or not name or not isinstance(domain, str) or not domain:
        return None
    raw_path = row.get("path")
    if raw_path is not None and not isinstance(raw_path, str):
        return None
    return CookieIdentity(name, domain, raw_path or "/")


def snapshot_index_last_wins(jar: CookieJar) -> Mapping[CookieIdentity, Cookie]:
    """Return a read-only last-wins index retaining first insertion position."""
    return MappingProxyType({cookie.key: cookie for cookie in jar})


def _serialized_expiry(cookie: Cookie) -> int | float:
    row = _cookie_semantics.cookie_to_storage_row(cookie, http_only=cookie.http_only)
    expires = row["expires"]
    if not isinstance(expires, int | float) or isinstance(expires, bool):
        raise AssertionError("serialized cookie expiry must be numeric")
    return expires


def _canonical_row(cookie: Cookie, *, same_site: object = None) -> dict[str, object]:
    carried = same_site if same_site in _VALID_SAME_SITE else "None"
    return _cookie_semantics.cookie_to_storage_row(
        cookie,
        http_only=cookie.http_only,
        same_site=str(carried),
        include_same_site=True,
    )


def _patched_row(row: Mapping[object, object], cookie: Cookie) -> dict[object, object]:
    updated = dict(row)
    fresh = _canonical_row(cookie, same_site=row.get("sameSite"))
    for field_name in ("value", "expires", "path", "secure", "httpOnly", "sameSite"):
        updated[field_name] = fresh[field_name]
    return updated


def _dirty_tuple(cookie: Cookie) -> tuple[object, ...]:
    return (
        cookie.value,
        cookie.expires,
        bool(cookie.secure),
        cookie.http_only,
    )


def _variant_entry(
    index: Mapping[CookieIdentity, Cookie], identity: CookieIdentity
) -> tuple[CookieIdentity, Cookie] | None:
    for variant in equivalent_identities(identity):
        cookie = index.get(variant)
        if cookie is not None:
            return variant, cookie
    return None


def _next_baseline(
    baseline: CookieJar,
    observation: CookieJar,
    rejected: frozenset[CookieIdentity],
    final_rows: list[object],
) -> CookieJar:
    final = _selected_final_cookies(final_rows)
    advanced = dict(snapshot_index_last_wins(observation))
    for identity, observed in tuple(advanced.items()):
        selected = _variant_entry(final, identity)
        if selected is None:
            continue
        advanced[identity] = Cookie(
            name=observed.name,
            domain=observed.domain,
            path=observed.path,
            value=observed.value,
            expires=observed.expires,
            secure=observed.secure,
            http_only=observed.http_only,
            same_site=selected[1].same_site,
        )
    if not rejected:
        return CookieJar(tuple(advanced.values()))
    old = snapshot_index_last_wins(baseline)
    for identity in rejected:
        original = _variant_entry(old, identity)
        for variant in equivalent_identities(identity):
            advanced.pop(variant, None)
        if original is not None:
            advanced[original[0]] = original[1]
    return CookieJar(tuple(advanced.values()))


def _selected_final_cookies(rows: list[object]) -> Mapping[CookieIdentity, Cookie]:
    """Project final stored rows with the live loader's first-successful rule."""
    selected: dict[CookieIdentity, Cookie] = {}
    for row in rows:
        try:
            normalized = _cookie_semantics.sanitize_cookie_entry(row)
        except _cookie_semantics.CookieRowError:
            continue
        if not _cookie_policy._is_allowed_cookie_domain(normalized["domain"]):
            continue
        identity = CookieIdentity(normalized["name"], normalized["domain"], normalized["path"])
        if identity in selected:
            continue
        try:
            converted = _cookie_semantics.cookie_from_normalized_entry(
                normalized,
                http_only_key="httpOnly",
            )
        except (ValueError, TypeError, OverflowError):
            continue
        raw_same_site = normalized.get("sameSite", normalized.get("same_site"))
        selected[identity] = Cookie(
            name=converted.name,
            domain=converted.domain,
            path=converted.path or "/",
            value=converted.value if converted.value is not None else "",
            expires=converted.expires,
            secure=bool(converted.secure),
            http_only=_cookie_semantics.cookie_is_http_only(converted),
            same_site=raw_same_site if isinstance(raw_same_site, str) else None,
        )
    return MappingProxyType(selected)


def _recovery_prepass(
    *,
    rows: list[object],
    deltas: Mapping[CookieIdentity, Cookie],
    observation: RecoveryObservation | None,
) -> tuple[list[object], int, set[CookieIdentity], set[CookieIdentity]]:
    if observation is None:
        return rows, 0, set(), set()

    replacements: dict[int, dict[str, object]] = {}
    removals: set[int] = set()
    appends: list[dict[str, object]] = []
    handled: set[CookieIdentity] = set()
    rejected: set[CookieIdentity] = set()
    updated_rows = 0

    for identity, cookie in deltas.items():
        if identity.name not in _cookie_policy._RECOVERY_TARGET_COOKIE_NAMES:
            continue
        variants = equivalent_identities(identity)
        observed_values: set[str | None] = set()
        for variant in variants:
            observed_values.update(observation.values_by_identity.get(variant, frozenset()))
        if not observed_values:
            continue

        row_indices = [
            index
            for index, row in enumerate(rows)
            if (stored_identity := stored_cookie_identity(row)) is not None
            and variants & equivalent_identities(stored_identity)
        ]
        replaceable: list[int] = []
        conflicts: list[int] = []
        for index in row_indices:
            row = rows[index]
            stored_value = row.get("value") if isinstance(row, Mapping) else None
            unusable = not isinstance(stored_value, str) or not stored_value
            if (
                stored_value == cookie.value
                or stored_value in observed_values
                or (unusable and None in observed_values)
            ):
                replaceable.append(index)
            else:
                conflicts.append(index)

        if conflicts:
            rejected.add(identity)
        if replaceable:
            winner = replaceable[0]
            winner_row = rows[winner]
            same_site = winner_row.get("sameSite") if isinstance(winner_row, Mapping) else None
            replacements[winner] = _canonical_row(cookie, same_site=same_site)
            removals.update(replaceable[1:])
            updated_rows += len(replaceable)
            handled.add(identity)
        elif not row_indices:
            appends.append(_canonical_row(cookie))
            updated_rows += 1
            handled.add(identity)
        elif conflicts:
            handled.add(identity)

    merged = [
        replacements.get(index, row) for index, row in enumerate(rows) if index not in removals
    ]
    merged.extend(appends)
    return merged, updated_rows, rejected, handled


def decide_cookie_merge(
    *,
    stored: ProfileDocument,
    observation: CookieJar,
    baseline: CookieJar,
    recovery_observation: RecoveryObservation | None,
) -> CookieMergeDecision:
    """Apply snapshot/delta CAS policy without performing any side effect."""
    rows = list(stored.raw_cookie_rows())
    current = snapshot_index_last_wins(observation)
    original = snapshot_index_last_wins(baseline)
    allowed_current = {
        identity: cookie
        for identity, cookie in current.items()
        if _cookie_policy._is_allowed_cookie_domain(identity.domain)
    }
    deltas = {
        identity: cookie
        for identity, cookie in allowed_current.items()
        if identity not in original or _dirty_tuple(original[identity]) != _dirty_tuple(cookie)
    }
    deletions = {
        identity
        for identity in original
        if identity not in current and _cookie_policy._is_allowed_cookie_domain(identity.domain)
    }

    rows, updated_rows, recovery_rejected, handled = _recovery_prepass(
        rows=rows,
        deltas=deltas,
        observation=recovery_observation,
    )
    rejected = set(recovery_rejected)
    conflicts: list[tuple[CookieIdentity, _ConflictKind]] = []
    remaining_deltas = {
        identity: cookie for identity, cookie in deltas.items() if identity not in handled
    }
    matched: set[CookieIdentity] = set(handled)
    merged: list[object] = []

    for row in rows:
        stored_identity = stored_cookie_identity(row)
        if stored_identity is None:
            merged.append(row)
            continue

        delta_match = _variant_entry(remaining_deltas, stored_identity)
        if delta_match is not None:
            identity, cookie = delta_match
            baseline_match = _variant_entry(original, identity)
            stored_value = row.get("value") if isinstance(row, Mapping) else None
            if (
                baseline_match is not None
                and stored_value != baseline_match[1].value
                and stored_value != cookie.value
            ):
                rejected.add(identity)
                conflicts.append((identity, "existing"))
                matched.add(identity)
                merged.append(row)
                continue
            if baseline_match is None and stored_value != cookie.value:
                rejected.add(identity)
                conflicts.append((identity, "new"))
                matched.add(identity)
                merged.append(row)
                continue
            if not isinstance(row, Mapping):  # pragma: no cover - identity proves mapping
                raise AssertionError("identified cookie row must be a mapping")
            merged.append(_patched_row(row, cookie))
            matched.add(identity)
            updated_rows += 1
            continue

        deletion_match = next(
            (variant for variant in equivalent_identities(stored_identity) if variant in deletions),
            None,
        )
        if deletion_match is not None:
            stored_value = row.get("value") if isinstance(row, Mapping) else None
            if stored_value == original[deletion_match].value:
                updated_rows += 1
                continue
            rejected.add(deletion_match)
        merged.append(row)

    for identity, cookie in remaining_deltas.items():
        if identity not in matched:
            merged.append(_canonical_row(cookie))
            updated_rows += 1

    rejected_frozen = frozenset(rejected)
    document = stored.replace_cookie_rows(merged) if updated_rows else None
    final_rows = merged if document is not None else list(stored.raw_cookie_rows())
    return CookieMergeDecision(
        document=document,
        next_baseline=_next_baseline(baseline, observation, rejected_frozen, final_rows),
        rejected=rejected_frozen,
        updated_rows=updated_rows,
        _conflicts=tuple(conflicts),
    )


def _legacy_candidate(
    index: Mapping[CookieIdentity, Cookie],
    identity: CookieIdentity,
    stored_value: object,
) -> Cookie | None:
    variants = sorted(
        equivalent_identities(identity),
        key=lambda variant: (variant.domain.startswith("."), variant.domain),
    )
    candidates = [index[variant] for variant in variants if variant in index]
    return next((cookie for cookie in candidates if cookie.value != stored_value), None) or next(
        iter(candidates), None
    )


def decide_legacy_cookie_overlay(
    *,
    stored: ProfileDocument,
    observation: CookieJar,
) -> CookieMergeDecision:
    """Apply the permanent no-baseline compatibility overlay."""
    index = MappingProxyType(
        {
            identity: cookie
            for identity, cookie in snapshot_index_last_wins(observation).items()
            if _cookie_policy._is_allowed_cookie_domain(identity.domain)
        }
    )
    rows = list(stored.raw_cookie_rows())
    stored_keys: set[CookieIdentity] = set()
    updated_rows = 0
    merged: list[object] = []

    for row in rows:
        identity = stored_cookie_identity(row)
        if identity is None:
            merged.append(row)
            continue
        stored_keys.update(equivalent_identities(identity))
        candidate = _legacy_candidate(
            index,
            identity,
            row.get("value") if isinstance(row, Mapping) else None,
        )
        if candidate is None:
            merged.append(row)
            continue
        if not isinstance(row, Mapping):  # pragma: no cover - identity proves mapping
            raise AssertionError("identified cookie row must be a mapping")
        if row.get("value") != candidate.value or row.get("expires") != _serialized_expiry(
            candidate
        ):
            merged.append(_patched_row(row, candidate))
            updated_rows += 1
        else:
            merged.append(row)

    for identity, cookie in index.items():
        if identity not in stored_keys:
            merged.append(_canonical_row(cookie))
            updated_rows += 1

    return CookieMergeDecision(
        document=stored.replace_cookie_rows(merged) if updated_rows else None,
        next_baseline=CookieJar(tuple(index.values())),
        updated_rows=updated_rows,
    )
