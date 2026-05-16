"""Canonicalize keepalive storage path for in-process dedupe.

Regression test for keepalive-path canonicalization: the rotation throttle keys the
in-process dedupe (``_LAST_POKE_ATTEMPT_MONOTONIC`` /
``_POKE_LOCKS_BY_LOOP``) by the raw ``Path`` object stored on
``ClientCore._keepalive_storage_path``. Without canonicalization, two
clients constructed with different syntactic representations of the SAME
underlying file (e.g. a relative path and the absolute path; a
``~``-prefixed path and the expanded one; with or without a symlink
component) hash to distinct dict keys and bypass the dedupe entirely —
duplicate ``RotateCookies`` POSTs.

``auth.py:_fetch_tokens_with_refresh`` already canonicalizes via
``Path(p).expanduser().resolve()`` before keying the refresh
generation/lock. ``NotebookLMClient`` must do the same for the keepalive
path before it reaches ``_get_poke_lock`` / ``_try_claim_rotation`` /
``_rotation_lock_path``.

The public ``storage_path`` argument type (``str | Path | None``) is
preserved; only the internal-derived ``ClientCore._keepalive_storage_path``
is canonicalized.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens

# Mock-only tests (no real HTTP, no cassette) — opt out of the
# integration-tree enforcement hook in ``tests/integration/conftest.py``.
pytestmark = pytest.mark.allow_no_vcr


@pytest.fixture
def _auth_tokens() -> AuthTokens:
    """Auth with no storage_path baked in; tests supply their own per-case."""
    return AuthTokens(
        cookies={"SID": "test_sid"},
        csrf_token="test_csrf",
        session_id="test_session",
    )


def test_relative_and_absolute_paths_share_dedupe_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _auth_tokens: AuthTokens
) -> None:
    """Two clients built with relative vs absolute paths to the SAME file
    must hold the same canonical ``_keepalive_storage_path``.

    Hashing the canonical ``Path`` is what gates the
    ``_LAST_POKE_ATTEMPT_MONOTONIC`` dedupe key. If the two clients store
    different ``Path`` objects, the dedupe registry treats them as
    independent profiles and both fire ``RotateCookies`` inside the
    rate-limit window.
    """
    # Real on-disk target. ``.resolve()`` requires the path to exist on
    # macOS+strict for portability across runners, but ``Path.resolve()``
    # tolerates non-existent paths on Linux. Create the file so both
    # platforms agree.
    target = tmp_path / "storage_state.json"
    target.write_text("{}", encoding="utf-8")

    absolute_path = target.resolve()

    # Build a RELATIVE representation of the same file by chdir-ing into
    # tmp_path and using just the filename. This is the canonical
    # "relative vs absolute" footgun for the dedupe key.
    monkeypatch.chdir(tmp_path)
    relative_path = Path("storage_state.json")

    # Sanity: the two Path objects must differ as raw values, otherwise
    # the test below wouldn't actually be exercising canonicalization.
    assert relative_path != absolute_path
    assert str(relative_path) != str(absolute_path)

    client_rel = NotebookLMClient(_auth_tokens, storage_path=relative_path)
    client_abs = NotebookLMClient(_auth_tokens, storage_path=absolute_path)

    # The dedupe key seen by ``_get_poke_lock`` / ``_try_claim_rotation``
    # / ``_rotation_lock_path`` is exactly this internal field. Both must
    # be canonical and equal.
    rel_keepalive = client_rel._core._keepalive_storage_path
    abs_keepalive = client_abs._core._keepalive_storage_path
    assert rel_keepalive is not None
    assert abs_keepalive is not None
    assert rel_keepalive == abs_keepalive, (
        f"Keepalive dedupe key differs across path representations: "
        f"{rel_keepalive!r} != {abs_keepalive!r}"
    )
    # Canonical form: absolute, expanduser-applied, symlinks resolved.
    assert rel_keepalive.is_absolute()
    assert rel_keepalive == absolute_path


def test_tilde_path_is_expanded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _auth_tokens: AuthTokens
) -> None:
    """A ``~``-prefixed path must expand to the same canonical form as
    its expanded sibling — second representation flavor of the same file.
    """
    # Pretend HOME lives under tmp_path so ``~`` expansion is hermetic.
    # ``Path.expanduser`` consults different env vars per platform:
    #   - POSIX: ``$HOME``
    #   - Windows: ``$USERPROFILE`` (then ``$HOMEDRIVE`` + ``$HOMEPATH``).
    # Set both so the test is portable; clear the Windows fallback so a
    # CI runner with a real ``HOMEDRIVE``/``HOMEPATH`` doesn't redirect
    # ``~`` to the runner's actual profile (the failure mode that bit
    # PR #612's first push on windows-latest, Python 3.14).
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    monkeypatch.delenv("HOMEDRIVE", raising=False)
    monkeypatch.delenv("HOMEPATH", raising=False)

    target = tmp_path / "auth.json"
    target.write_text("{}", encoding="utf-8")

    tilde_path = Path("~/auth.json")
    expanded_path = (tmp_path / "auth.json").resolve()
    # Sanity: the override must actually take effect on this platform —
    # if expansion still points elsewhere, the test would silently pass
    # trivially via a different path. Skip rather than test-vacuously.
    if Path("~").expanduser() != tmp_path:
        pytest.skip(
            f"Cannot hermetically override ~ on this platform "
            f"(expanduser='{Path('~').expanduser()}', want='{tmp_path}')"
        )

    client_tilde = NotebookLMClient(_auth_tokens, storage_path=tilde_path)
    client_expanded = NotebookLMClient(_auth_tokens, storage_path=expanded_path)

    tilde_key = client_tilde._core._keepalive_storage_path
    expanded_key = client_expanded._core._keepalive_storage_path
    assert tilde_key is not None
    assert expanded_key is not None
    assert tilde_key == expanded_key
    assert tilde_key == expanded_path


def test_public_storage_path_argument_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _auth_tokens: AuthTokens
) -> None:
    """Canonicalization must preserve the public path argument type
    (``str | Path | None``) — only the internal-derived
    ``_keepalive_storage_path`` is
    canonicalized. The auth object's ``storage_path`` and the argument
    passed by the caller stay as-is so external observers (logs,
    serialization, downstream callers) see the original value.

    Uses a GENUINELY non-canonical path (relative-from-cwd) so the
    assertion isn't trivially satisfied by an already-resolved input —
    ``tmp_path`` is itself a resolved absolute path on every supported
    platform, so a naïve ``Path(str(tmp_path / "x"))`` would compare
    equal to its resolved form and the test would prove nothing.
    """
    target = tmp_path / "store.json"
    target.write_text("{}", encoding="utf-8")

    # Build a relative-from-cwd path that is decisively different from
    # its canonical absolute form. ``monkeypatch.chdir`` reverts on
    # teardown, so no cross-test contamination.
    monkeypatch.chdir(tmp_path)
    raw_path = Path("store.json")
    assert raw_path != target.resolve()
    assert str(raw_path) != str(target.resolve())

    client = NotebookLMClient(_auth_tokens, storage_path=raw_path)

    # auth.storage_path was normalized onto the auth object by __init__
    # (see client.py:153-154). The PUBLIC value remains the caller's
    # original (non-canonical) Path — not the canonicalized one.
    assert client.auth.storage_path == raw_path
    assert client.auth.storage_path != target.resolve()
    # And the internal keepalive field IS canonicalized.
    keepalive_key = client._core._keepalive_storage_path
    assert keepalive_key is not None
    assert keepalive_key == target.resolve()
    assert keepalive_key.is_absolute()


def test_symlink_is_resolved(tmp_path: Path, _auth_tokens: AuthTokens) -> None:
    """A symlinked path must resolve to its canonical target — the third
    representation flavor that callers can construct for the same file.
    CodeRabbit + Claude-bot both flagged this case as a coverage gap on
    the initial PR; this closes it. Without symlink resolution, a client
    opened via the symlink and
    another opened via the real path would each occupy a distinct
    dedupe slot and double-fire ``RotateCookies``.
    """
    target = tmp_path / "actual.json"
    target.write_text("{}", encoding="utf-8")

    link = tmp_path / "link.json"
    link.symlink_to(target)
    # Sanity: raw Path objects compare unequal; resolution is what
    # makes them coincide.
    assert link != target

    client_link = NotebookLMClient(_auth_tokens, storage_path=link)
    client_target = NotebookLMClient(_auth_tokens, storage_path=target)

    link_key = client_link._core._keepalive_storage_path
    target_key = client_target._core._keepalive_storage_path
    assert link_key is not None
    assert target_key is not None
    assert link_key == target_key
    assert link_key == target.resolve()


def test_none_storage_path_stays_none(_auth_tokens: AuthTokens) -> None:
    """Canonicalization must be a no-op when there is no storage path."""
    client = NotebookLMClient(_auth_tokens)
    assert client._core._keepalive_storage_path is None
