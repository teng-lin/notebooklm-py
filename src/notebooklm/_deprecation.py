"""Internal helpers for emitting the project's ``DeprecationWarning`` family.

Centralises the one-off ``warnings.warn`` calls so the message text, the
``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression gate, and the ``stacklevel``
bookkeeping live in a single, tested place instead of being copy-pasted at
every deprecated call site.

This is an implementation module. There is no public surface here; the public
deprecation *policy* (what is deprecated, since when, removal target) is
documented in ``docs/deprecations.md``.

Four families live here:

* ``warn_deprecated`` — the generic gated primitive for one-off deprecations
  that don't fit the three specific families below (e.g. awaiting
  ``from_storage(...)``, ``ResearchAPI.poll(task_id=None)`` ambiguity,
  ``NotebooksAPI.share()``). It exists so ad-hoc deprecations have a gated home
  rather than hand-rolling ``warnings.warn(...)`` and silently bypassing the
  suppression switch (issue #1369). Note that not every inline warning is a
  deprecation: ``save_cookies_to_storage(original_snapshot=None)`` emits a
  permanent ``RuntimeWarning`` race advisory (not a scheduled removal), so it
  is emitted inline and is *not* routed through here.
* ``warn_get_returns_none`` — marks ``<resource>.get()`` returning ``None`` on
  a miss as deprecated (issue #1247).
* ``deprecated_kwarg`` — the keyword-alias pattern used when a public method
  renames a parameter but keeps the old name working for one MINOR cycle. The
  canonical case is the wait/poll timeout standardization (issue #1208):
  ``ResearchAPI.wait_for_completion`` renamed ``interval`` to
  ``initial_interval`` (matching ``SourcesAPI.wait_until_ready`` /
  ``ArtifactsAPI.wait_for_completion``) and accepts the old name as a
  deprecated alias removed in v0.8.0.

* ``MappingCompatMixin`` — the dict-subscript backward-compat bridge used when
  a public method that historically returned ``dict[str, Any]`` is upgraded to
  a typed dataclass (issue #1209). Mixing it into the dataclass keeps the old
  ``result["key"]`` / ``result.get("key")`` / ``result.keys()`` /
  ``"key" in result`` access working (each subscript emits a
  ``DeprecationWarning``) while ``result.key`` becomes the typed, warning-free
  path. The mixin — and the dict-style access — is removed in v0.8.0.

These families share the single ``NOTEBOOKLM_QUIET_DEPRECATIONS`` suppression
gate (read live, never cached) and a parameterized ``stacklevel`` so the
warning's ``filename``/``lineno`` point at the *user's* call site. The warning
message always names the removal version (so ``scripts/check_deprecation_targets.py``
can verify the shipping release never names *itself* as the removal target), and
passing BOTH the old and new keyword raises :class:`TypeError` rather than
silently preferring one.

A second, orthogonal gate lives here too: ``NOTEBOOKLM_FUTURE_ERRORS``
(:func:`future_errors_enabled`). When on, the three runways above adopt their
v0.8.0 *target* behavior early — ``deprecated_kwarg`` and the
:class:`MappingCompatMixin` subscript raise instead of warn (the
``<resource>.get()`` runway is routed through ``_lookup.resolve_get``). It is an
opt-in forward-compat preview, default-off, and takes precedence over the quiet
gate (a runway raises regardless of quiet; quiet only silences the warn path
that future mode replaces). See ``docs/deprecations.md``.
"""

from __future__ import annotations

import os
import warnings
from collections.abc import ItemsView, Iterator, KeysView, ValuesView
from typing import Any, ClassVar, TypeVar

# Suppression gate. Setting ``NOTEBOOKLM_QUIET_DEPRECATIONS`` to a truthy value
# silences the warnings emitted through this module. This re-activates the
# historically-documented env var (``docs/configuration.md``) for the new
# get()-returns-None deprecation; it is intentionally read live (not cached) so
# tests and callers can toggle it per call.
_QUIET_ENV_VAR = "NOTEBOOKLM_QUIET_DEPRECATIONS"

# Forward-compat preview gate. Setting ``NOTEBOOKLM_FUTURE_ERRORS`` to a truthy
# value makes the v0.7.0 deprecation *runways* adopt their v0.8.0 *target*
# behavior early, so callers and CI can test forward-compatibility against the
# breaking error contract (ADR-0019, umbrella #1346) before it ships. It is
# read live (not cached) so tests and callers can toggle it per call, exactly
# like the quiet gate. Default-off behavior is byte-identical to current
# v0.7.0 behavior. ``FUTURE_ERRORS`` takes precedence over
# ``QUIET_DEPRECATIONS``: when future errors are on, the runway raises
# regardless of the quiet gate (quiet only silences the warn path that future
# mode replaces). See ``docs/deprecations.md``.
_FUTURE_ERRORS_ENV_VAR = "NOTEBOOKLM_FUTURE_ERRORS"

# Follow-up issue tracking the actual breaking flip in v0.8.0, where these
# ``get()`` methods stop returning ``None`` and start raising the relevant
# ``*NotFoundError``. Referenced in the warning message and in
# ``docs/deprecations.md`` so callers can find the migration guidance.
GET_RETURNS_NONE_FLIP_ISSUE = 1247

# Canonical removal target for the kwarg aliases introduced by issue #1208.
# Kept as a module constant so the message text and the docs stay in lockstep
# and the release gate has a single string to scan. Warns in 0.7.0, removed in
# 0.8.0.
DEFAULT_REMOVAL = "0.8.0"

_T = TypeVar("_T")


def _deprecations_quiet() -> bool:
    """Return ``True`` when deprecation warnings are suppressed via env var."""
    raw = os.environ.get(_QUIET_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def deprecations_quiet() -> bool:
    """Public alias for :func:`_deprecations_quiet`.

    ``NOTEBOOKLM_QUIET_DEPRECATIONS=1`` (or any truthy ``1``/``true``/``yes``/
    ``on`` spelling, case-insensitive) silences the ``DeprecationWarning``
    emitted by :func:`deprecated_kwarg`. Any other value — including unset —
    leaves the warning enabled.
    """
    return _deprecations_quiet()


def _future_errors_enabled() -> bool:
    """Return ``True`` when the v0.8.0 error-contract preview is opted in."""
    raw = os.environ.get(_FUTURE_ERRORS_ENV_VAR, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def future_errors_enabled() -> bool:
    """Public alias for :func:`_future_errors_enabled`.

    ``NOTEBOOKLM_FUTURE_ERRORS=1`` (or any truthy ``1``/``true``/``yes``/``on``
    spelling, case-insensitive) opts a process into the **v0.8.0 error contract**
    early. When on, the v0.7.0 deprecation *runways* that still warn today adopt
    their v0.8.0 *target* behavior instead — ``<resource>.get()`` raises the
    matching ``*NotFoundError`` on a miss (#1247), the
    :class:`MappingCompatMixin` dict-subscript bridge raises instead of
    warning-and-returning (#1251), and :func:`deprecated_kwarg` raises on the
    deprecated keyword instead of aliasing it (#1254). Any other value —
    including unset — keeps the current (warn) behavior, so default-off is
    byte-identical to v0.7.0.

    This gate takes precedence over :func:`deprecations_quiet`: when future
    errors are enabled the runway *raises* regardless of the quiet setting,
    because the quiet gate only silences the warning that future mode replaces
    with an exception. The purely-behavioral v0.8.0 changes that lack a clean
    warn-runway (bool returns, refusal-suppression, fail-loud listing) are
    **not** covered by this flag yet; they will be folded in as their v0.8.0
    behavior is defined (tracked as a follow-up).
    """
    return _future_errors_enabled()


def warn_deprecated(message: str, *, removal: str | None = None, stacklevel: int = 3) -> None:
    """Emit a project ``DeprecationWarning``, honoring the suppression gate.

    The generic primitive for one-off deprecations that don't fit the three
    specific families (``warn_get_returns_none`` / ``deprecated_kwarg`` /
    ``MappingCompatMixin``). Routing every ad-hoc warning through here keeps the
    ``NOTEBOOKLM_QUIET_DEPRECATIONS`` gate and the ``DeprecationWarning``
    category in one place — ADR-018 rejects inline ``warnings.warn(...)`` calls
    scattered through feature modules precisely because they bypass this gate.

    No-ops when :func:`_deprecations_quiet` is true (i.e. when
    ``NOTEBOOKLM_QUIET_DEPRECATIONS`` is set to a truthy value); otherwise emits
    a single :class:`DeprecationWarning` with ``message``.

    Args:
        message: The full warning text. Callers own the wording (what is
            deprecated, what to use instead). When ``removal`` is given and the
            message does not already name that version, a sentence naming the
            removal version is appended so every gated warning states its
            removal target consistently.
        removal: Optional removal version, e.g. ``"1.0"`` or ``"0.8.0"``. Pass
            a version when one is scheduled; the version is ensured to appear in
            the emitted text. Pass ``None`` in two cases — the message is emitted
            verbatim for both: (a) a *permanent* back-compat shim that is never
            scheduled for removal, or (b) a deprecation that *will* be removed
            but has no pinned version yet (the message can still say "a future
            major release"). Always pass ``removal`` explicitly so a future
            reader patching the call to add a version knows where to put it.
        stacklevel: ``warnings.warn`` stacklevel. The default of ``3`` accounts
            for the single-hop case ``warn_deprecated`` (1) → the deprecated
            method/property (2) → the user's call site (3), so the warning's
            ``filename``/``lineno`` point at user code. Pass ``4`` (etc.) when an
            extra wrapper frame sits between the deprecated public surface and
            this helper (e.g. ``poll`` → ``_select_polled_tasks`` →
            ``warn_deprecated``). The default ``3`` is correct for any call made
            directly from the deprecated public surface; do not drop to ``2``,
            which would attribute the warning to the library's own line.
    """
    if _deprecations_quiet():
        return

    text = message
    # ``v{removal}`` is the precise spelling our messages use; the bare
    # ``removal`` fallback catches messages that name the version without the
    # ``v`` prefix. Both checks are substring matches — fine for the short,
    # single-sentence deprecation messages this helper emits (no version-looking
    # URLs or longer numbers), and only ever skip an otherwise-redundant append.
    if removal is not None and f"v{removal}" not in text and removal not in text:
        text = f"{text} It will be removed in v{removal}."
    warnings.warn(text, DeprecationWarning, stacklevel=stacklevel)


def _not_found_error_exists(exc_name: str) -> bool:
    """Return ``True`` if ``exc_name`` is already defined in ``exceptions``.

    Lazy/local import keeps ``_deprecation`` free of a module-load-time
    dependency on ``exceptions`` (which would risk an import cycle). Used only
    to decide whether the migration hint can name the exception unqualified.
    """
    from . import exceptions

    return hasattr(exceptions, exc_name)


def warn_get_returns_none(resource: str, *, removal: str = "0.8.0", stacklevel: int = 3) -> None:
    """Warn that ``<resource>.get()`` returning ``None`` on a miss is deprecated.

    ``sources.get`` / ``artifacts.get`` / ``notes.get`` currently return
    ``None`` when the entity is not found, while ``notebooks.get`` raises
    :class:`~notebooklm.exceptions.NotebookNotFoundError`. This warning marks
    the ``None``-returning behavior as deprecated; in **v0.8.0** these methods
    will instead raise the relevant ``*NotFoundError`` (tracked by issue
    #1247), unifying the not-found contract across all four ``get()`` methods.

    The warning fires only on a *miss* (when the method is about to return
    ``None``); successful lookups stay silent. It is suppressible by setting
    ``NOTEBOOKLM_QUIET_DEPRECATIONS`` to a truthy value.

    Args:
        resource: Singular resource name for the message, e.g. ``"source"``,
            ``"artifact"``, or ``"note"``. Used to name the matching
            ``<Resource>NotFoundError`` in the migration hint.
        removal: Stated removal/flip version (default ``"0.8.0"``). Kept as a
            parameter so the message and the release-gate
            (``scripts/check_deprecation_targets.py``) share one source of
            truth.
        stacklevel: ``warnings.warn`` stacklevel. The default of ``3`` is correct
            when this helper is called *directly* from the public ``get()``
            (helper → ``get()`` → user). Callers that route through an extra
            wrapper frame — the shared ``_lookup.resolve_get`` bridge — pass
            ``4`` so the warning still points at the user's ``get()`` call site.
    """
    if _deprecations_quiet():
        return

    # PascalCase the resource so multi-word names map to the real class name
    # (e.g. "mind_map" -> "MindMapNotFoundError", not "Mind_mapNotFoundError").
    exc_stem = "".join(part.capitalize() for part in resource.split("_"))
    exc_name = f"{exc_stem}NotFoundError"
    # The matching <Resource>NotFoundError for every resource that warns today
    # (source / artifact / note) is already defined and importable, so the hint
    # names it directly. If a future resource warns before its exception lands,
    # qualify the hint so a caller who follows the migration advice immediately
    # doesn't hit an ImportError on a not-yet-defined class.
    exc_hint = (
        exc_name if _not_found_error_exists(exc_name) else f"{exc_name} (added in v{removal})"
    )
    message = (
        f"{resource}s.get() returning None for a missing {resource} is "
        f"deprecated and will be removed in v{removal}: in v{removal} it will "
        f"raise {exc_name} instead (issue "
        f"#{GET_RETURNS_NONE_FLIP_ISSUE}). To keep handling missing "
        f"{resource}s, wrap the call in try/except {exc_hint}."
    )
    # stacklevel=3 (default): warn_get_returns_none (1) -> the public get() (2)
    # -> the user's call site (3). Points the warning's filename/lineno at the
    # caller that wrote ``await client.<resource>s.get(...)``. Callers reaching
    # this helper through ``_lookup.resolve_get`` pass stacklevel=4 to account
    # for the extra bridge frame.
    warnings.warn(message, DeprecationWarning, stacklevel=stacklevel)


def deprecated_kwarg(
    old_value: _T | None,
    new_value: _T | None,
    *,
    old: str,
    new: str,
    owner: str,
    removal: str = DEFAULT_REMOVAL,
    sentinel: object = None,
    stacklevel: int = 3,
) -> _T | None:
    """Resolve a renamed keyword, warning if the deprecated name was used.

    Maps a deprecated keyword (``old``) onto its replacement (``new``) for a
    single public method. Returns the value that the method should actually
    use, after emitting a :class:`DeprecationWarning` when (and only when) the
    caller passed the deprecated name.

    Args:
        old_value: The value the caller passed for the deprecated keyword, or
            ``sentinel`` when the caller did not pass it.
        new_value: The value the caller passed for the canonical keyword, or
            ``sentinel`` when the caller did not pass it.
        old: Name of the deprecated keyword (for messages), e.g. ``"interval"``.
        new: Name of the canonical replacement keyword, e.g.
            ``"initial_interval"``.
        owner: Human-readable owner of the parameter for the warning message,
            e.g. ``"ResearchAPI.wait_for_completion"``.
        removal: Version in which the deprecated keyword is removed. Defaults
            to v0.8.0. Named in the warning text so the release gate can verify
            it is never the shipping version.
        sentinel: The "not provided" marker for both ``old_value`` and
            ``new_value``. Defaults to ``None``; pass a private sentinel object
            when ``None`` is itself a meaningful value.
        stacklevel: ``warnings.warn`` stacklevel. The default of ``3`` points
            the warning at the caller of the public method (caller →
            public method → this helper). Adjust when the helper is invoked
            through additional wrapper frames.

    Returns:
        ``new_value`` when the caller used the canonical keyword; ``old_value``
        when the caller used the deprecated keyword (after warning); otherwise
        ``sentinel`` (neither provided — the method keeps its own default).

    Raises:
        TypeError: If the caller passed BOTH the deprecated and the canonical
            keyword (they name the same concept, so two values is ambiguous);
            or, when ``NOTEBOOKLM_FUTURE_ERRORS`` is on, if the caller passed
            the deprecated keyword at all (previewing the v0.8.0 removal —
            issue #1254).
    """
    new_provided = new_value is not sentinel
    old_provided = old_value is not sentinel

    if old_provided and new_provided:
        raise TypeError(
            f"{owner}() received both {new!r} and the deprecated alias {old!r}; pass only {new!r}."
        )

    if old_provided:
        if _future_errors_enabled():
            # v0.8.0 preview: the deprecated keyword is removed, so passing it
            # is a TypeError (the same shape an unknown keyword raises once the
            # alias is gone). Precedence over the quiet gate is deliberate —
            # quiet only silences the warning that future mode replaces.
            raise TypeError(
                f"{owner}() got an unexpected keyword argument {old!r}; "
                f"use {new!r} instead (the {old!r} alias was removed in v{removal})."
            )
        if not _deprecations_quiet():
            warnings.warn(
                (
                    f"{owner}({old}=...) is deprecated and will be removed in "
                    f"v{removal}; use {new}=... instead (same behavior). "
                    f"Set {_QUIET_ENV_VAR}=1 to silence this warning."
                ),
                DeprecationWarning,
                stacklevel=stacklevel,
            )
        return old_value

    # Neither provided (``new_value`` already equals ``sentinel``) or only the
    # canonical keyword was passed: return it directly so the static type stays
    # ``_T | None`` rather than the widened ``object`` of ``sentinel``.
    return new_value


class MappingCompatMixin:
    """Give a dataclass deprecated, ``dict``-style read access (issue #1209).

    Several public methods historically returned a plain ``dict[str, Any]``
    (``research.poll`` / ``research.start`` / ``research.wait_for_completion``,
    ``artifacts.generate_mind_map``, ``sources.get_guide``). Those returns are
    being upgraded to typed dataclasses so callers can use attribute access
    (``result.status``) and static typing. To stay backward-compatible for one
    MINOR cycle, the dataclass mixes this in: every legacy ``result["status"]``
    / ``result.get("status")`` / ``result.keys()`` / ``"status" in result`` keeps
    working against the *historical dict shape*, emitting a single
    :class:`DeprecationWarning` on each *subscript* access (``__getitem__`` only).
    The rest of the read-mapping surface — ``get`` / ``keys`` / ``items`` /
    ``values`` / ``__len__`` / ``__contains__`` / ``__iter__`` — stays silent so
    callers can probe shape without a warning storm. (``dict(result)`` still
    works but warns, since the ``dict`` constructor reads each key via
    ``__getitem__``.) The warning names the **v0.8.0** removal and is
    suppressible via ``NOTEBOOKLM_QUIET_DEPRECATIONS``. In v0.8.0 the mixin is
    dropped and the dataclasses become attribute-only.

    The legacy values come from the subclass's ``to_public_dict()`` (the exact
    historical dict that method used to return) so nested access like
    ``result["sources"][0]["url"]`` keeps yielding the old dict-of-dicts shape
    rather than the new typed objects. Subclasses MUST implement
    ``to_public_dict() -> dict[str, Any]``.

    ``_COMPAT_KEYS`` is an optional key→attribute map used only to phrase the
    deprecation hint (``use the typed attribute .<attr>``). When a key is absent
    from the map the hint falls back to the key name itself.
    """

    # Optional legacy dict-key -> attribute-name map, used only for the warning
    # hint. Subclasses override when a dict key differs from its attribute name.
    _COMPAT_KEYS: ClassVar[dict[str, str]] = {}

    def to_public_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        """Return the historical ``dict`` shape. Subclasses must override."""
        raise NotImplementedError

    def __getitem__(self, key: str) -> Any:
        """Deprecated dict-style read; warns and returns the legacy dict value.

        When ``NOTEBOOKLM_FUTURE_ERRORS`` is on, this previews the v0.8.0 state
        in which the mixin is dropped and the return is an attribute-only
        dataclass: any subscript raises :class:`TypeError` (the exact error a
        plain dataclass raises — ``'<Type>' object is not subscriptable``),
        regardless of the quiet gate (issue #1251).
        """
        if _future_errors_enabled():
            raise TypeError(f"{type(self).__name__!r} object is not subscriptable")
        legacy = self.to_public_dict()
        if key not in legacy:
            raise KeyError(key)
        if not _deprecations_quiet():
            attr = self._COMPAT_KEYS.get(key, key)
            warnings.warn(
                (
                    f"{type(self).__name__}[{key!r}] dict-style access is "
                    f"deprecated and will be removed in v{DEFAULT_REMOVAL}; use "
                    f"the typed attribute .{attr} instead. "
                    f"Set {_QUIET_ENV_VAR}=1 to silence this warning."
                ),
                DeprecationWarning,
                stacklevel=2,
            )
        return legacy[key]

    def get(self, key: str, default: Any = None) -> Any:
        """Deprecated ``dict.get`` shim. Silent (no warning) like ``dict.get``.

        Returns the legacy dict value when ``key`` is present, otherwise
        ``default``. Unlike :meth:`__getitem__` this does not warn, so existing
        ``result.get("status", "")`` shape-probes stay quiet; the migration
        prompt is reserved for the subscript form.
        """
        return self.to_public_dict().get(key, default)

    def keys(self) -> KeysView[str]:
        """Return the legacy dict keys view (silent)."""
        return self.to_public_dict().keys()

    def items(self) -> ItemsView[str, Any]:
        """Return the legacy dict items view (silent)."""
        return self.to_public_dict().items()

    def values(self) -> ValuesView[Any]:
        """Return the legacy dict values view (silent)."""
        return self.to_public_dict().values()

    def __len__(self) -> int:
        """Return the number of legacy dict keys (silent)."""
        return len(self.to_public_dict())

    def __contains__(self, key: object) -> bool:
        """Support ``"key" in result`` against the legacy key set (silent)."""
        return key in self.to_public_dict()

    def __iter__(self) -> Iterator[str]:
        """Iterate the legacy dict keys (silent; mirrors ``dict`` iteration)."""
        return iter(self.to_public_dict())
