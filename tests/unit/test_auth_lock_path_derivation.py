"""Byte-identity pins for the four credential lock-file paths (ADR-0033 PR 1.3).

PR 1.3 unified the *derivation code* for the four lock paths onto one helper
(``_auth.paths._lock_sibling``). It must not have moved a single lock file.
A lock-filename change is not cosmetic: during a mixed-version window an old
CLI and a new server sharing one profile would take **different** lock files
and lose each other's updates — the class ADR-0029's canonical writer closed.

So the expectations below are **hard-coded literals**, never a second call into
the implementation. A test that compared the new derivation against itself
would pass no matter which filename it produced, which is exactly the failure
this file exists to prevent.

Two things are pinned per case:

1. the **leaf filename** of each of the four locks, and
2. the **base path** each one derives from — only ``_bootstrap_lock_path``
   canonicalizes (``expanduser().resolve()``); the other three take the
   caller's path exactly as given. That asymmetry is load-bearing (#2103,
   #1215) and silently reversible, so every case asserts both halves.

The cases cover the spellings that make the two policies diverge: a relative
path, a symlinked path, a ``~`` path, a trailing slash, spaces, and the
``NOTEBOOKLM_AUTH_JSON`` env-auth case where the resolver hands the helpers
``None``.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from notebooklm._auth import master_token as mt
from notebooklm._auth import storage as auth_storage
from notebooklm._auth.paths import (
    NOTEBOOKLM_AUTH_JSON_ENV,
    _bootstrap_lock_path,
    _refresh_lock_path,
    _rotation_lock_path,
    _storage_state_lock_path,
    canonical_storage_key,
)

# The four filenames, spelled out. These strings ARE the cross-version contract.
STORAGE_LOCK_NAME = ".storage_state.json.lock"
ROTATE_LOCK_NAME = ".storage_state.json.rotate.lock"
REFRESH_LOCK_NAME = ".storage_state.json.refresh.lock"
BOOTSTRAP_LOCK_NAME = ".storage_state.json.lock.bootstrap"


def _uncanonicalized_three(storage_path: Path) -> tuple[Path, Path | None, Path | None]:
    """The three locks that derive from the caller's path exactly as given."""
    return (
        _storage_state_lock_path(storage_path),
        _rotation_lock_path(storage_path),
        _refresh_lock_path(storage_path),
    )


# --- The four names, and the fact that they are four ---------------------------


def test_the_four_lock_names_are_exactly_these_four_strings(tmp_path: Path) -> None:
    storage = tmp_path / "storage_state.json"

    rotate = _rotation_lock_path(storage)
    refresh = _refresh_lock_path(storage)
    assert rotate is not None and refresh is not None

    assert _storage_state_lock_path(storage).name == ".storage_state.json.lock"
    assert rotate.name == ".storage_state.json.rotate.lock"
    assert refresh.name == ".storage_state.json.refresh.lock"
    assert _bootstrap_lock_path(storage).name == ".storage_state.json.lock.bootstrap"


def test_four_distinct_lock_files_never_collapse_to_fewer(tmp_path: Path) -> None:
    """Collapsing any two of these is a deadlock, not a contention nuisance.

    ``bootstrap_storage_from_master_token`` holds the bootstrap lock across the
    mint, and the mint's ``persist_minted_jar`` acquires the storage lock
    INSIDE that critical section. See
    ``test_storage_lock_is_free_inside_the_bootstrap_critical_section`` below
    for the executable half of the argument.
    """
    storage = tmp_path / "storage_state.json"
    derived = [
        _storage_state_lock_path(storage),
        _rotation_lock_path(storage),
        _refresh_lock_path(storage),
        _bootstrap_lock_path(storage),
    ]

    assert None not in derived
    assert len({os.fspath(path) for path in derived if path is not None}) == 4


def test_master_token_reuses_the_shared_derivation(tmp_path: Path) -> None:
    """``master_token`` no longer hand-rolls its own sibling computation.

    Its module attribute stays a live white-box seam for the suites that reach
    ``mt._bootstrap_lock_path``; it is now the same object as the ``paths``
    helper rather than a fourth private copy of the arithmetic.
    """
    assert mt._bootstrap_lock_path is _bootstrap_lock_path

    storage = tmp_path / "storage_state.json"
    assert mt._bootstrap_lock_path(storage) == tmp_path.resolve() / BOOTSTRAP_LOCK_NAME


# --- Case: a relative path -----------------------------------------------------


def test_relative_path_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relative in, relative out — for three of the four."""
    monkeypatch.chdir(tmp_path)
    storage = Path("profiles/default/storage_state.json")

    assert _storage_state_lock_path(storage) == Path("profiles/default/.storage_state.json.lock")
    assert _rotation_lock_path(storage) == Path("profiles/default/.storage_state.json.rotate.lock")
    assert _refresh_lock_path(storage) == Path("profiles/default/.storage_state.json.refresh.lock")
    # The bootstrap lock alone resolves against the CWD, so two processes with
    # different working directories still serialize their first-time mint.
    assert (
        _bootstrap_lock_path(storage)
        == tmp_path.resolve() / "profiles" / "default" / BOOTSTRAP_LOCK_NAME
    )


# --- Case: a symlinked path ----------------------------------------------------


def test_symlinked_path_locks(tmp_path: Path) -> None:
    """The divergence in one picture: three locks live under the LINK, the
    bootstrap lock under the link's TARGET."""
    real = tmp_path / "real-profile"
    real.mkdir()
    link = tmp_path / "linked-profile"
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform w/o symlinks
        pytest.skip("platform cannot create directory symlinks")
    storage = link / "storage_state.json"

    assert _storage_state_lock_path(storage) == link / STORAGE_LOCK_NAME
    assert _rotation_lock_path(storage) == link / ROTATE_LOCK_NAME
    assert _refresh_lock_path(storage) == link / REFRESH_LOCK_NAME
    assert _bootstrap_lock_path(storage) == real.resolve() / BOOTSTRAP_LOCK_NAME

    # ...which is why the bootstrap lock (and only it) is alias-proof.
    direct = real / "storage_state.json"
    assert _bootstrap_lock_path(storage) == _bootstrap_lock_path(direct)
    assert _storage_state_lock_path(storage) != _storage_state_lock_path(direct)


# --- Case: a ``~`` path --------------------------------------------------------


def test_tilde_path_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The literal ``~`` survives into three of the four lock paths."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    storage = Path("~/nb/storage_state.json")

    assert _storage_state_lock_path(storage) == Path("~/nb/.storage_state.json.lock")
    assert _rotation_lock_path(storage) == Path("~/nb/.storage_state.json.rotate.lock")
    assert _refresh_lock_path(storage) == Path("~/nb/.storage_state.json.refresh.lock")
    assert _bootstrap_lock_path(storage) == home.resolve() / "nb" / BOOTSTRAP_LOCK_NAME


# --- Case: a trailing slash ----------------------------------------------------


def test_trailing_slash_is_indistinguishable_from_no_slash(tmp_path: Path) -> None:
    slashed = Path(f"{tmp_path}/profile/storage_state.json/")
    plain = tmp_path / "profile" / "storage_state.json"

    assert _storage_state_lock_path(slashed) == tmp_path / "profile" / STORAGE_LOCK_NAME
    assert _rotation_lock_path(slashed) == tmp_path / "profile" / ROTATE_LOCK_NAME
    assert _refresh_lock_path(slashed) == tmp_path / "profile" / REFRESH_LOCK_NAME
    assert _bootstrap_lock_path(slashed) == tmp_path.resolve() / "profile" / BOOTSTRAP_LOCK_NAME
    assert _uncanonicalized_three(slashed) == _uncanonicalized_three(plain)


# --- Case: a path containing spaces --------------------------------------------


def test_spaces_are_carried_through_verbatim(tmp_path: Path) -> None:
    """No quoting, escaping or normalization anywhere in the derivation."""
    profile = tmp_path / "my work profile"
    profile.mkdir()
    storage = profile / "storage_state.json"

    assert _storage_state_lock_path(storage) == profile / ".storage_state.json.lock"
    assert _rotation_lock_path(storage) == profile / ".storage_state.json.rotate.lock"
    assert _refresh_lock_path(storage) == profile / ".storage_state.json.refresh.lock"
    assert _bootstrap_lock_path(storage) == profile.resolve() / ".storage_state.json.lock.bootstrap"
    assert " " in os.fspath(_storage_state_lock_path(storage))


# --- Case: env auth (``NOTEBOOKLM_AUTH_JSON``), where the path is ``None`` ------


def test_env_auth_none_path_yields_no_sentinel(monkeypatch: pytest.MonkeyPatch) -> None:
    """With inline env auth there is no file to anchor a sentinel to.

    The two helpers reachable with ``None`` return ``None``, which their callers
    read as "no cross-process flock; fall back to the in-process claim". The
    other two are typed non-optional and their callers short-circuit before
    reaching them (``save_cookies_to_storage`` returns early on env auth;
    bootstrap runs only for a real profile file).
    """
    monkeypatch.setenv(NOTEBOOKLM_AUTH_JSON_ENV, '{"cookies": []}')

    assert canonical_storage_key(None) is None
    assert _rotation_lock_path(None) is None
    assert _refresh_lock_path(None) is None


# --- Non-goal 2, executable: the nesting still holds ---------------------------


def test_storage_lock_is_free_inside_the_bootstrap_critical_section(tmp_path: Path) -> None:
    """The storage sentinel must be acquirable while the bootstrap lock is held.

    ``MasterTokenBootstrapper.bootstrap_storage`` holds
    ``filelock.FileLock(bootstrap)`` across ``_run_remint_to_settlement`` ->
    ``remint_from_stored_token`` -> ``ProfileStore.replace_minted_session``,
    which takes the storage lock. The second assertion shows what "collapse
    the paths" would actually cost: the same acquire against the bootstrap path
    is reported CONTENDED — guaranteed-unavailable inside the section that
    holds it, not merely slow.
    """
    from filelock import FileLock

    storage = tmp_path / "storage_state.json"
    bootstrap_lock = _bootstrap_lock_path(storage)
    storage_lock = _storage_state_lock_path(storage)

    with FileLock(str(bootstrap_lock), timeout=5):
        with auth_storage._file_lock(
            storage_lock, blocking=False, log_prefix="test-nesting"
        ) as state:
            assert state == "held"

        with auth_storage._file_lock(
            bootstrap_lock, blocking=False, log_prefix="test-collapsed"
        ) as collapsed_state:
            assert collapsed_state == "contended"
