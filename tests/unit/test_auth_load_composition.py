"""ADR-0033 PR 2.1: one load -> validate -> heal -> retry composition.

The sequence "strict load, heal on a routing failure, retry name-only" used to be
written twice — once in ``cookies.build_httpx_cookies_from_storage`` and once in
``tokens.load_auth_from_storage`` — and the second copy had decayed into two
byte-identical branches nobody read. It now has one owner,
``psidts_recovery.load_with_recovery``, with the heal decision expressed as a
:class:`~notebooklm._auth.psidts_recovery.HealPolicy` rather than as the loaders'
private ``require_routable`` boolean.

These tests pin what the merge is allowed to be: both wrappers route through the
one composition, each policy arm keeps its contract, and ``require_routable``
stays internal.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest
from pytest_httpx import HTTPXMock

import notebooklm._auth.cookies as _auth_cookies
import notebooklm._auth.psidts_recovery as _auth_psidts_recovery
import notebooklm._auth.tokens as _auth_tokens
from notebooklm._auth.psidts_recovery import HealPolicy

# Present by name, but scoped to an app host, so it never routes to
# ``accounts.google.com/RotateCookies`` — the exact condition the inline heal
# exists for, and the only one that reaches the composition's except arm.
_UNROUTABLE_PSIDTS = [
    {"name": "SID", "value": "sid", "domain": ".google.com", "path": "/"},
    {"name": "APISID", "value": "apisid", "domain": ".google.com", "path": "/"},
    {"name": "SAPISID", "value": "sapisid", "domain": ".google.com", "path": "/"},
    {
        "name": "__Secure-1PSIDTS",
        "value": "unroutable",
        "domain": "notebooklm.google.com",
        "path": "/",
    },
]


@pytest.fixture
def storage_file(tmp_path: Path) -> Path:
    path = tmp_path / "storage_state.json"
    path.write_text(json.dumps({"cookies": _UNROUTABLE_PSIDTS}), encoding="utf-8")
    return path


class TestPolicyArms:
    def test_name_only_never_heals(
        self, storage_file: Path, httpx_mock: HTTPXMock, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``NAME_ONLY`` is the no-heal contract the passive probes depend on."""

        def _must_not_run(_path: object) -> bool:
            raise AssertionError("NAME_ONLY fired a heal")

        jar = _auth_psidts_recovery.load_with_recovery(
            storage_file,
            HealPolicy.NAME_ONLY,
            load=_auth_cookies._load_cookies_pure,
            heal=_must_not_run,
        )

        assert "__Secure-1PSIDTS" in {c.name for c in jar.jar}
        assert httpx_mock.get_requests() == []

    def test_declined_heal_retries_name_only(
        self, storage_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """#2061: a heal that cannot run must not harden a loadable state."""
        calls: list[object] = []

        def _decline(path: object) -> bool:
            calls.append(path)
            return False

        # injected, not patched — see load_with_recovery's heal seam

        jar = _auth_psidts_recovery.load_with_recovery(
            storage_file,
            HealPolicy.HEAL_THEN_NAME_ONLY,
            load=_auth_cookies._load_cookies_pure,
            heal=_decline,
        )

        assert calls == [storage_file]
        assert "__Secure-1PSIDTS" in {c.name for c in jar.jar}

    def test_successful_heal_retries_exactly_once(
        self, storage_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The one retry uses the name-only contract and cannot recurse."""
        heals = 0

        def _heal(_path: object) -> bool:
            nonlocal heals
            heals += 1
            return True

        # injected, not patched — see load_with_recovery's heal seam

        strict_passes = 0
        real_loader = _auth_cookies._load_cookies_pure

        def _counting(path: Path | None = None, *, require_routable: bool = True) -> Any:
            nonlocal strict_passes
            if require_routable:
                strict_passes += 1
            return real_loader(path, require_routable=require_routable)

        monkeypatch.setattr(_auth_cookies, "_load_cookies_pure", _counting)
        _auth_psidts_recovery.load_with_recovery(
            storage_file,
            HealPolicy.HEAL_THEN_NAME_ONLY,
            load=_auth_cookies._load_cookies_pure,
            heal=_heal,
        )

        assert heals == 1
        assert strict_passes == 1, "the post-heal retry must not re-ask the routing question"


class TestBothWrappersUseTheOneComposition:
    """The point of the merge: neither wrapper may grow its own copy back."""

    def test_jar_wrapper_delegates(
        self, storage_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[object, object]] = []
        real = _auth_psidts_recovery.load_with_recovery

        def _spy(path: object, policy: object, **kwargs: Any) -> Any:
            seen.append((path, policy))
            # Inject the declining heal rather than patching the module
            # attribute — the delegate under test only needs the heal not to
            # fire, and this PR should not add private patch sites to a codebase
            # whose acceptance criterion is removing them.
            kwargs.setdefault("heal", lambda _p: False)
            return real(path, policy, **kwargs)

        monkeypatch.setattr(_auth_psidts_recovery, "load_with_recovery", _spy)

        _auth_cookies.build_httpx_cookies_from_storage(storage_file)

        assert seen == [(storage_file, HealPolicy.HEAL_THEN_NAME_ONLY)]

    def test_flat_wrapper_delegates(
        self, storage_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[tuple[object, object]] = []
        real = _auth_psidts_recovery.load_with_recovery

        def _spy(path: object, policy: object, **kwargs: Any) -> Any:
            seen.append((path, policy))
            # Inject the declining heal rather than patching the module
            # attribute — the delegate under test only needs the heal not to
            # fire, and this PR should not add private patch sites to a codebase
            # whose acceptance criterion is removing them.
            kwargs.setdefault("heal", lambda _p: False)
            return real(path, policy, **kwargs)

        monkeypatch.setattr(_auth_psidts_recovery, "load_with_recovery", _spy)

        cookies = _auth_tokens.load_auth_from_storage(storage_file)

        assert seen == [(storage_file, HealPolicy.HEAL_THEN_NAME_ONLY)]
        assert cookies["__Secure-1PSIDTS"] == "unroutable"

    def test_passive_jar_loader_selects_name_only(
        self, storage_file: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``fetch_tokens_passive``'s loader picks the policy, not a second body."""
        seen: list[object] = []
        real = _auth_psidts_recovery.load_with_recovery

        def _spy(path: object, policy: object, **kwargs: Any) -> Any:
            seen.append(policy)
            return real(path, policy, **kwargs)

        monkeypatch.setattr(_auth_psidts_recovery, "load_with_recovery", _spy)

        _auth_cookies._build_httpx_cookies_from_storage_strict(storage_file)

        assert seen == [HealPolicy.NAME_ONLY]


def test_require_routable_is_internal() -> None:
    """The loaders' private knob must not surface on any load entry point.

    Callers select a :class:`HealPolicy`; ``require_routable`` stays on the pure
    loaders, where "pass it only where a heal follows" is a local rule rather
    than a public contract someone can get wrong.
    """
    import notebooklm.auth as auth_facade

    entry_points = [
        auth_facade.load_auth_from_storage,
        auth_facade.build_httpx_cookies_from_storage,
        _auth_cookies.build_httpx_cookies_from_storage,
        _auth_tokens.load_auth_from_storage,
        _auth_psidts_recovery.load_with_recovery,
    ]
    for fn in entry_points:
        params = inspect.signature(fn).parameters
        assert "require_routable" not in params, (
            f"{fn.__module__}.{fn.__qualname__} exposes require_routable; "
            "callers must select a HealPolicy instead"
        )
    assert not hasattr(_auth_psidts_recovery, "load_session_jar")
