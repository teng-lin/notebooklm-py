"""Tests for the open-time snapshot + dirty-flag merge in
``save_cookies_to_storage`` — the fix for issue #361 (stale in-memory
cookies clobbering fresh disk state) and the side-effect closure of
``docs/auth-keepalive.md`` §3.4.2 (path collapse).

The canonical race that motivated this code (#361):

    Process A and Process B both share the same ``storage_state.json``.
    A loads ``*PSIDTS=OLD`` at open time and never rotates.
    B rotates ``*PSIDTS`` to ``NEW`` and writes to disk.
    A's ``close()`` reads disk under flock, sees its in-memory ``OLD``
    differs from disk's ``NEW``, and "merges" by writing ``OLD`` —
    silently undoing B's rotation.

The fix is an open-time snapshot per ``ClientCore`` instance, plus a
``save_cookies_to_storage`` mode that writes only the deltas relative to
that snapshot. Cookies the in-process code never touched are left to
disk; sibling-process writes survive.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from notebooklm.auth import (
    AuthTokens,
    CookieSaveResult,
    CookieSnapshotKey,
    CookieSnapshotValue,
    advance_cookie_snapshot_after_save,
    build_httpx_cookies_from_storage,
    save_cookies_to_storage,
    snapshot_cookie_jar,
)


def _read_cookies(storage_path: Path) -> list[dict]:
    """Helper: read the cookies array from a Playwright storage_state.json."""
    return json.loads(storage_path.read_text(encoding="utf-8"))["cookies"]


def _cookie_value(storage_path: Path, name: str, domain: str, path: str = "/") -> str | None:
    """Helper: extract a single cookie's value from disk by (name, domain, path)."""
    for c in _read_cookies(storage_path):
        if c.get("name") == name and c.get("domain") == domain and (c.get("path") or "/") == path:
            return c.get("value")
    return None


def _write_storage(storage_path: Path, cookies: list[dict]) -> None:
    """Helper: write a Playwright-shaped storage_state.json."""
    storage_path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")


def _stored_cookie(name: str, value: str, **overrides) -> dict:
    """Build a Playwright storage_state cookie dict with sensible defaults.

    Defaults match the manual ``httpx.Cookies().set(...)`` jars used by these
    tests (a non-Secure, HttpOnly, root-path cookie on ``.google.com``). Pass any field as a
    keyword override (e.g. ``domain="accounts.google.com"``, ``path="/u/0/"``,
    ``http_only=False``, ``expires=1_000_000``).
    """
    return {
        "name": name,
        "value": value,
        "domain": overrides.get("domain", ".google.com"),
        "path": overrides.get("path", "/"),
        "expires": overrides.get("expires", -1),
        "httpOnly": overrides.get("http_only", True),
        "secure": overrides.get("secure", False),
        "sameSite": overrides.get("same_site", "None"),
    }


def _set_cookie_value(jar: httpx.Cookies, name: str, value) -> None:
    """Mutate the in-memory cookie object's value in place.

    Mirrors the ``for cookie in jar.jar: if cookie.name == name: cookie.value = X``
    idiom the tests use to simulate Set-Cookie rotation without round-tripping
    through ``jar.set`` (which would also re-normalize domain attrs).
    """
    for cookie in jar.jar:
        if cookie.name == name:
            cookie.value = value


class TestSnapshotKey:
    """The ``CookieSnapshotKey`` NamedTuple is the path-aware key used by
    the snapshot/delta machinery. It must be a NamedTuple (so it's
    hashable + structurally typed) and not collapse different paths."""

    def test_named_tuple_fields(self):
        key = CookieSnapshotKey("SID", ".google.com", "/")
        assert key.name == "SID"
        assert key.domain == ".google.com"
        assert key.path == "/"

    def test_path_distinguishes_keys(self):
        a = CookieSnapshotKey("OSID", "accounts.google.com", "/")
        b = CookieSnapshotKey("OSID", "accounts.google.com", "/u/0/")
        assert a != b
        assert hash(a) != hash(b) or a != b  # tuple-hash, distinct via inequality

    def test_named_tuple_is_hashable(self):
        key = CookieSnapshotKey("SID", ".google.com", "/")
        assert {key: "value"}[key] == "value"


class TestSnapshotCookieJar:
    """``snapshot_cookie_jar`` captures the path-aware ``(name, domain, path)
    -> value`` map that downstream merges depend on."""

    def test_captures_basic_cookie(self):
        jar = httpx.Cookies()
        jar.set("SID", "abc", domain=".google.com", path="/")
        snap = snapshot_cookie_jar(jar)
        key = CookieSnapshotKey("SID", ".google.com", "/")
        assert set(snap) == {key}
        assert snap[key].value == "abc"

    def test_path_aware_keys_do_not_collapse(self):
        """Two cookies with the same name+domain but different paths are
        distinct entries in the snapshot — closes §3.4.2."""
        jar = httpx.Cookies()
        jar.set("OSID", "root", domain="accounts.google.com", path="/")
        jar.set("OSID", "scoped", domain="accounts.google.com", path="/u/0/")
        snap = snapshot_cookie_jar(jar)
        assert snap[CookieSnapshotKey("OSID", "accounts.google.com", "/")].value == "root"
        assert snap[CookieSnapshotKey("OSID", "accounts.google.com", "/u/0/")].value == "scoped"

    def test_normalizes_missing_path_to_root(self):
        """Cookies without an explicit path default to ``/`` in the snapshot key."""
        jar = httpx.Cookies()
        # http.cookiejar normalizes empty path to "/", but verify the
        # snapshot helper agrees.
        jar.set("SID", "abc", domain=".google.com")
        snap = snapshot_cookie_jar(jar)
        assert CookieSnapshotKey("SID", ".google.com", "/") in snap


# NOTE on "two simulated processes" framing in this file: all the multi-
# writer scenarios below run serially in one Python thread. They prove the
# delta math is sound under each timeline, not that the implementation
# handles real interleavings under flock — the latter is covered by the
# ``subprocess.Popen`` test in test_client_keepalive.py
# (test_save_cookies_to_storage_acquires_file_lock).


class TestStaleOverwriteFreshRace:
    """The §3.4.1 / #361 failure timeline as a unit test.

    Two simulated processes share one ``storage_state.json``. Process A
    holds a stale in-memory jar (snapshot captured at open time, never
    rotated). Process B rotates ``*PSIDTS`` to ``NEW`` between A's open
    and A's close, and writes to disk. When A's ``close()`` save runs,
    the snapshot/delta merge must NOT clobber B's fresh value.
    """

    def test_stale_in_memory_does_not_clobber_fresh_disk(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        # t=0: disk has *PSIDTS=OLD
        _write_storage(
            storage,
            [
                _stored_cookie("__Secure-1PSIDTS", "OLD"),
                _stored_cookie("SID", "sid-A"),
            ],
        )

        # Process A opens: builds jar from disk, captures snapshot.
        jar_a = httpx.Cookies()
        jar_a.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        jar_a.set("SID", "sid-A", domain=".google.com", path="/")
        snapshot_a = snapshot_cookie_jar(jar_a)

        # Process B (simulated): loaded OLD at its own open, rotates
        # *PSIDTS to NEW, and writes to disk via the same save API any
        # other writer would use.
        jar_b = httpx.Cookies()
        jar_b.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        jar_b.set("SID", "sid-A", domain=".google.com", path="/")
        snapshot_b = snapshot_cookie_jar(jar_b)
        jar_b.set("__Secure-1PSIDTS", "NEW", domain=".google.com", path="/")
        save_cookies_to_storage(jar_b, storage, original_snapshot=snapshot_b)

        assert (
            _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "NEW"
        ), "Process B's rotation should land on disk before A closes"

        # Process A closes: jar still holds OLD (it never rotated). Without
        # the fix, this save would write OLD over NEW. With the fix, A's
        # delta vs its snapshot is empty — nothing is written.
        save_cookies_to_storage(jar_a, storage, original_snapshot=snapshot_a)

        # Verify: disk still has B's fresh NEW value.
        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "NEW", (
            "Process A's close must NOT clobber the fresh *PSIDTS=NEW that "
            "Process B rotated; A never rotated so its snapshot/delta is empty"
        )
        # And SID (which neither process touched) is intact.
        assert _cookie_value(storage, "SID", ".google.com") == "sid-A"


class TestCookieDeletionPropagation:
    """When httpx auto-deletes a cookie (e.g. ``Max-Age=0`` from a
    Set-Cookie response), the deletion must propagate to disk on the
    next save — otherwise the disk remembers a cookie the wire-side
    has explicitly invalidated."""

    def test_deletion_propagates_to_disk(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("A", "a"),
                _stored_cookie("B", "b"),
                _stored_cookie("C", "c"),
            ],
        )

        # Open: jar has A, B, C.
        jar = httpx.Cookies()
        jar.set("A", "a", domain=".google.com", path="/")
        jar.set("B", "b", domain=".google.com", path="/")
        jar.set("C", "c", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        # Simulate httpx auto-deleting B mid-session (Max-Age=0 evict).
        jar.delete("B", domain=".google.com", path="/")

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        cookies = _read_cookies(storage)
        names = {c["name"] for c in cookies}
        assert "A" in names
        assert "C" in names
        assert "B" not in names, (
            "Cookie deleted from the in-memory jar must be removed from disk; "
            "without deletion propagation, the disk keeps a wire-side-invalidated cookie"
        )


class TestPathAwareKeyRegression:
    """§3.4.2: two storage entries with the same ``(name, domain)`` but
    different paths are distinct cookies. A snapshot/save round trip
    must not collapse them."""

    def test_storage_loader_preserves_same_name_domain_path_variants(self, tmp_path):
        """The path-aware save path only works if load keeps all path variants."""
        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "sid"),
                _stored_cookie("__Secure-1PSIDTS", "psidts"),
                _stored_cookie("OSID", "root", domain="accounts.google.com"),
                _stored_cookie("OSID", "scoped", domain="accounts.google.com", path="/u/0/"),
            ],
        )

        jar = build_httpx_cookies_from_storage(storage)
        snapshot = snapshot_cookie_jar(jar)

        assert snapshot[CookieSnapshotKey("OSID", "accounts.google.com", "/")].value == "root"
        assert snapshot[CookieSnapshotKey("OSID", "accounts.google.com", "/u/0/")].value == "scoped"

    def test_two_paths_survive_round_trip(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("OSID", "root", domain="accounts.google.com"),
                _stored_cookie("OSID", "scoped", domain="accounts.google.com", path="/u/0/"),
            ],
        )

        # Build a jar that mirrors disk; rotate the root variant only.
        jar = httpx.Cookies()
        jar.set("OSID", "root", domain="accounts.google.com", path="/")
        jar.set("OSID", "scoped", domain="accounts.google.com", path="/u/0/")
        snapshot = snapshot_cookie_jar(jar)

        # Rotate only the root-path cookie.
        jar.set("OSID", "rotated_root", domain="accounts.google.com", path="/")

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        # Both path variants must survive; the unrotated /u/0/ entry is
        # untouched, and the rotated root entry has the new value.
        assert _cookie_value(storage, "OSID", "accounts.google.com", "/") == "rotated_root"
        assert _cookie_value(storage, "OSID", "accounts.google.com", "/u/0/") == "scoped"


class TestLegitimateRotationFirstFreshWriterWins:
    """When two processes legitimately rotate the same cookie, the **first**
    writer to land wins: the second writer's value-update CAS guard observes
    disk ≠ snapshot and backs off, preserving the first writer's fresh state.
    Symmetric with the deletion CAS guard."""

    def test_second_writer_preserves_first_writers_fresh_value(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD")])

        # A opens, rotates *PSIDTS to NEW_A in its own jar (not yet saved).
        jar_a = httpx.Cookies()
        jar_a.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot_a = snapshot_cookie_jar(jar_a)
        jar_a.set("__Secure-1PSIDTS", "NEW_A", domain=".google.com", path="/")

        # B writes NEW_B to disk first.
        jar_b = httpx.Cookies()
        jar_b.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot_b = snapshot_cookie_jar(jar_b)
        jar_b.set("__Secure-1PSIDTS", "NEW_B", domain=".google.com", path="/")
        save_cookies_to_storage(jar_b, storage, original_snapshot=snapshot_b)

        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "NEW_B"

        # A's save runs after B. A's snapshot recorded OLD; disk now has
        # NEW_B. CAS guard fires → A preserves NEW_B rather than clobber it
        # with NEW_A.
        save_cookies_to_storage(jar_a, storage, original_snapshot=snapshot_a)
        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "NEW_B", (
            "Concurrent legitimate rotations resolve by first-fresh-writer-wins; "
            "the CAS guard preserves the value that landed while disk still "
            "matched the snapshot"
        )


class TestSiblingWrittenCookieSurvives:
    """A cookie that a sibling process wrote to disk while we were
    holding the jar — and that was never in our snapshot — must survive
    our save unchanged. This is the inverse-side of the §3.4.1 fix:
    not just "don't clobber rotated values" but also "don't drop
    sibling-only entries"."""

    def test_sibling_only_cookie_is_left_alone(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        # Process A opens with just SID.
        jar_a = httpx.Cookies()
        jar_a.set("SID", "sid-A", domain=".google.com", path="/")
        snapshot_a = snapshot_cookie_jar(jar_a)

        # Sibling process B writes a cookie A has never seen
        # (e.g. a per-product OSID it minted while doing its own work).
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "sid-A"),
                _stored_cookie("OSID", "sibling-only", domain="accounts.google.com"),
            ],
        )

        # A saves; nothing rotated, snapshot/delta is empty.
        save_cookies_to_storage(jar_a, storage, original_snapshot=snapshot_a)

        # Sibling's OSID must still be on disk.
        assert _cookie_value(storage, "OSID", "accounts.google.com") == "sibling-only", (
            "A cookie a sibling process wrote that A never saw must NOT be "
            "dropped from disk by A's save"
        )


class TestLegacyCallerCompatibility:
    """External callers that don't pass ``original_snapshot`` still get the
    legacy full-merge behavior — but emit a ``DeprecationWarning`` so the
    silent legacy-fallback hazard surfaces in caller logs. The kwarg is
    optional purely as a public-API back-compat shim; every in-tree caller
    passes it.
    """

    def test_legacy_call_writes_in_memory_value_and_warns(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "old", http_only=False)])

        jar = httpx.Cookies()
        jar.set("SID", "new", domain=".google.com", path="/")

        # No original_snapshot → legacy mode → in-memory wins on differing
        # values, AND a DeprecationWarning is emitted to surface the unsafe
        # path in caller logs.
        with pytest.warns(DeprecationWarning, match="original_snapshot"):
            save_cookies_to_storage(jar, storage)

        assert _cookie_value(storage, "SID", ".google.com") == "new"


class TestRefreshAuthOnBoundSessionIsNoOp:
    """``NotebookLMClient.refresh_auth`` does only a homepage GET. For a
    bound (Playwright-minted) session, that GET does NOT rotate
    ``*PSIDTS`` (per docs/auth-keepalive.md §5.4). With snapshot
    semantics the resulting save must be a no-op — closing the
    "bound-session refresh broadcasts stale state" reachability path
    listed in #361.
    """

    @pytest.mark.asyncio
    async def test_refresh_auth_does_not_clobber_when_nothing_rotated(self, tmp_path, httpx_mock):
        from notebooklm.client import NotebookLMClient

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("__Secure-1PSIDTS", "ONDISK"),
                _stored_cookie("SID", "sid-bound"),
            ],
        )

        # The client is opened with a stale in-memory copy: *PSIDTS=STALE,
        # mirroring the §3.4.1 timeline where another process has already
        # rotated to ONDISK on disk.
        auth = AuthTokens(
            cookies={
                ("__Secure-1PSIDTS", ".google.com"): "STALE",
                ("SID", ".google.com"): "sid-bound",
            },
            csrf_token="csrf-old",
            session_id="sid-old",
            storage_path=storage,
        )

        # Bound-session homepage GET: no Set-Cookie header, so no rotation.
        httpx_mock.add_response(
            url="https://notebooklm.google.com/",
            content=(
                b"<html><script>window.WIZ_global_data="
                b'{"SNlM0e":"new_csrf","FdrFJe":"new_sid"};</script></html>'
            ),
        )

        client = NotebookLMClient(auth)
        async with client:
            await client.refresh_auth()

        # *PSIDTS on disk must still be ONDISK — refresh_auth's save must
        # NOT have broadcast the stale STALE value back over it.
        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "ONDISK", (
            "refresh_auth on a bound session that didn't rotate must not "
            "clobber the disk value with the in-memory stale value"
        )


class TestAttributeOnlyRefresh:
    """A ``Set-Cookie`` with the same value but new ``expires`` /
    ``secure`` / ``httpOnly`` must still propagate to disk.
    ``CookieSnapshotValue`` is a 4-tuple precisely so attribute-only
    refreshes register as a delta — keying on value alone would silently
    drop session-extension Set-Cookies."""

    def test_expires_extension_writes_through(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "abc", expires=1_000_000)])

        # Build a jar whose SID has the same value but a longer expiry.
        # Mirrors Google extending a session cookie's lifetime via 302
        # redirect without rotating the value.
        jar = httpx.Cookies()
        jar.set("SID", "abc", domain=".google.com", path="/")
        for cookie in jar.jar:
            if cookie.name == "SID":
                cookie.expires = 1_000_000
        snapshot_pre = snapshot_cookie_jar(jar)

        # Simulate the in-memory expiry refresh by reconstructing the jar
        # entry with a later ``expires``.
        for cookie in jar.jar:
            if cookie.name == "SID":
                cookie.expires = 2_000_000

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot_pre)

        on_disk = next(c for c in _read_cookies(storage) if c["name"] == "SID")
        assert on_disk["expires"] == 2_000_000, (
            "An attribute-only refresh (same value, new expires) must reach "
            "disk — the legacy path persisted this and the snapshot path "
            "regressed it"
        )

    def test_attribute_only_sibling_refresh_does_not_block_value_rotation(self, tmp_path):
        """Same-value sibling metadata drift must not wedge value rotations."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "abc", expires=1_000_000)])

        jar = httpx.Cookies()
        jar.set("SID", "abc", domain=".google.com", path="/")
        for cookie in jar.jar:
            if cookie.name == "SID":
                cookie.expires = 1_000_000
        snapshot = snapshot_cookie_jar(jar)

        # Sibling process extends the same value further than our local jar.
        cookies = _read_cookies(storage)
        cookies[0]["expires"] = 3_000_000
        _write_storage(storage, cookies)

        for cookie in jar.jar:
            if cookie.name == "SID":
                cookie.expires = 2_000_000

        _set_cookie_value(jar, "SID", "rotated")

        result = save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        on_disk = next(c for c in _read_cookies(storage) if c["name"] == "SID")
        assert result is True
        assert on_disk["value"] == "rotated"
        assert on_disk["expires"] == 2_000_000


class TestSnapshotRefreshedAfterSave:
    """``ClientCore._loaded_cookie_snapshot`` is refreshed after every
    successful save. Without this, the open-time snapshot stays frozen
    and a second save from the same client re-applies the first save's
    delta — silently clobbering any sibling-process write that landed
    between two of our own saves (the keepalive + close common case).
    """

    @pytest.mark.asyncio
    async def test_second_save_does_not_replay_first_delta(self, tmp_path, httpx_mock):
        from notebooklm.client import NotebookLMClient

        storage = tmp_path / "storage_state.json"
        # Initial disk state: PSIDTS=OPEN at process-open time.
        _write_storage(
            storage,
            [
                _stored_cookie("__Secure-1PSIDTS", "OPEN"),
                _stored_cookie("SID", "sid"),
            ],
        )

        auth = AuthTokens(
            cookies={
                ("__Secure-1PSIDTS", ".google.com"): "OPEN",
                ("SID", ".google.com"): "sid",
            },
            csrf_token="csrf",
            session_id="sid",
            storage_path=storage,
        )

        # Two homepage responses — refresh_auth is called twice.
        for _ in range(2):
            httpx_mock.add_response(
                url="https://notebooklm.google.com/",
                content=(
                    b"<html><script>window.WIZ_global_data="
                    b'{"SNlM0e":"csrf","FdrFJe":"sid"};</script></html>'
                ),
            )

        client = NotebookLMClient(auth)
        async with client:
            # First save: rotates *PSIDTS in-process to A1, then save propagates.
            _set_cookie_value(client._core._http_client.cookies, "__Secure-1PSIDTS", "A1")
            await client.refresh_auth()
            assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "A1"

            # Sibling process B writes B1 to disk between A's two saves.
            cookies = _read_cookies(storage)
            for c in cookies:
                if c["name"] == "__Secure-1PSIDTS":
                    c["value"] = "B1"
            _write_storage(storage, cookies)

            # Second save: A's jar still has A1 (no rotation since save 1).
            # Without the post-save snapshot refresh, A's delta would
            # remain {PSIDTS: A1 vs OPEN} and A would write A1 over B1.
            # With the fix, A's baseline now reflects A1, so no delta
            # is computed and B1 is preserved.
            await client.refresh_auth()

        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "B1", (
            "A's second save must NOT replay the first save's delta over "
            "a sibling process's intervening write"
        )


class TestDeletionCASGuard:
    """The deletion path compares the on-disk value to the snapshot
    value before dropping. If they differ a sibling process has rewritten
    the row and our local eviction (e.g. an expired ``Max-Age=0``) must
    not erase their fresh state."""

    def test_deletion_skipped_when_disk_value_differs(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD")])

        jar = httpx.Cookies()
        jar.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        # B writes a fresh value to disk between our snapshot and our save.
        cookies = _read_cookies(storage)
        cookies[0]["value"] = "B-NEW"
        _write_storage(storage, cookies)

        # Locally we evict the cookie (httpx eviction on Max-Age=0). Our
        # jar no longer carries the entry → it becomes a deletion candidate.
        jar.delete("__Secure-1PSIDTS", domain=".google.com", path="/")

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "B-NEW", (
            "Deletion of a snapshot key must not drop the disk row when "
            "the disk value has rotated since our snapshot (CAS guard)"
        )

    def test_deletion_applied_when_only_disk_attributes_differ(self, tmp_path):
        """Value-only CAS allows deletion when only sibling metadata drifted."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD", expires=1_000_000)])

        jar = httpx.Cookies()
        jar.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        for cookie in jar.jar:
            if cookie.name == "__Secure-1PSIDTS":
                cookie.expires = 1_000_000
        snapshot = snapshot_cookie_jar(jar)

        cookies = _read_cookies(storage)
        cookies[0]["expires"] = 2_000_000
        _write_storage(storage, cookies)

        jar.delete("__Secure-1PSIDTS", domain=".google.com", path="/")

        result = save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert result is True
        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") is None

    def test_deletion_applied_when_disk_value_matches(self, tmp_path):
        """The CAS-guarded deletion still fires when no sibling write has
        intervened: snapshot value == disk value, so the drop is safe."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD")])

        jar = httpx.Cookies()
        jar.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        jar.delete("__Secure-1PSIDTS", domain=".google.com", path="/")
        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") is None


class TestSnapshotValueIncludesAttributes:
    """``CookieSnapshotValue`` is a tuple of
    ``(value, expires, secure, http_only)``. The widening exists so that
    attribute-only refreshes register as deltas — see
    ``TestAttributeOnlyRefresh``."""

    def test_value_tuple_fields(self):
        v = CookieSnapshotValue(value="abc", expires=12345, secure=True, http_only=False)
        assert v.value == "abc"
        assert v.expires == 12345
        assert v.secure is True
        assert v.http_only is False

    def test_distinct_attributes_yield_distinct_values(self):
        a = CookieSnapshotValue(value="abc", expires=10, secure=True, http_only=True)
        b = CookieSnapshotValue(value="abc", expires=20, secure=True, http_only=True)
        assert a != b


class TestSaveReturnsBoolSuccess:
    """``save_cookies_to_storage`` returns ``True`` when the disk now
    reflects the in-memory state (successful write or no-op-because-equal)
    and ``False`` when an I/O error prevented the write. ``ClientCore``
    uses this signal to decide whether to advance ``_loaded_cookie_snapshot``;
    a silent disk-write failure must NOT advance the baseline, otherwise
    the failed delta is permanently lost on the next save.
    """

    def test_returns_true_on_successful_write(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "old")])
        jar = httpx.Cookies()
        jar.set("SID", "old", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)
        _set_cookie_value(jar, "SID", "new")

        assert save_cookies_to_storage(jar, storage, original_snapshot=snapshot) is True

    def test_returns_true_when_nothing_to_write(self, tmp_path):
        """No deltas, no deletions → caller can safely advance the baseline."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "same", http_only=False)])
        jar = httpx.Cookies()
        jar.set("SID", "same", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        assert save_cookies_to_storage(jar, storage, original_snapshot=snapshot) is True

    def test_returns_false_when_write_fails(self, tmp_path, monkeypatch):
        """Disk-write failure (ENOSPC, EROFS, permission denied, etc.) must
        be observable to the caller so the baseline snapshot is not advanced
        and the failed delta gets retried on the next save."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "old", http_only=False)])
        jar = httpx.Cookies()
        jar.set("SID", "old", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)
        _set_cookie_value(jar, "SID", "new")

        # Simulate ENOSPC at the temp-file write step.
        import tempfile

        real_namedtemp = tempfile.NamedTemporaryFile

        def boom_namedtemp(*args, **kwargs):
            handle = real_namedtemp(*args, **kwargs)
            handle.write = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated ENOSPC"))
            return handle

        monkeypatch.setattr(tempfile, "NamedTemporaryFile", boom_namedtemp)
        monkeypatch.setattr("notebooklm.auth.tempfile.NamedTemporaryFile", boom_namedtemp)

        assert save_cookies_to_storage(jar, storage, original_snapshot=snapshot) is False
        # And the original on-disk value must still be intact.
        assert _cookie_value(storage, "SID", ".google.com") == "old"

    def test_returns_false_when_read_fails(self, tmp_path):
        """Corrupted-JSON read failure must also surface as ``False`` so the
        baseline isn't advanced before the next retry."""
        storage = tmp_path / "storage_state.json"
        storage.write_text("not json {")
        jar = httpx.Cookies()
        jar.set("SID", "new", domain=".google.com", path="/")
        snapshot: dict = {}

        assert save_cookies_to_storage(jar, storage, original_snapshot=snapshot) is False

    def test_returns_false_when_file_missing(self, tmp_path):
        """Storage file vanished between snapshot capture and save (e.g. an
        ongoing atomic rename from a sibling). Don't advance the baseline."""
        storage = tmp_path / "storage_state.json"
        # File deliberately not created.
        jar = httpx.Cookies()
        jar.set("SID", "new", domain=".google.com", path="/")
        snapshot: dict = {}

        assert save_cookies_to_storage(jar, storage, original_snapshot=snapshot) is False


class TestValueUpdateCASGuard:
    """The CAS guard extends to value updates, not just deletions. If we
    have a delta ``{K: X}`` but disk has already rotated ``K`` to ``Y``
    (a sibling process wrote between our open and our save), preserve
    ``Y`` rather than clobber it with our local ``X``. This closes the
    asymmetry the deletion-only CAS guard left open: ``refresh_auth`` on
    a long-lived client whose homepage GET rotates a cookie would
    otherwise still clobber a fresher sibling write on the same key.
    """

    def test_value_update_skipped_when_disk_value_differs_from_snapshot(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD")])

        # Our snapshot captures OLD.
        jar = httpx.Cookies()
        jar.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        # A sibling rotates the disk row to SIBLING_NEW while we hold the
        # stale OLD snapshot.
        cookies = _read_cookies(storage)
        cookies[0]["value"] = "SIBLING_NEW"
        _write_storage(storage, cookies)

        # Locally we rotate too (e.g. refresh_auth's homepage GET emits a
        # Set-Cookie). Our delta is {key: OURS_NEW vs snapshot OLD}.
        _set_cookie_value(jar, "__Secure-1PSIDTS", "OURS_NEW")

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "SIBLING_NEW", (
            "Value update of a delta key must be skipped when the disk value "
            "has rotated since our snapshot — the sibling's fresh write must "
            "survive (CAS guard symmetric with the deletion guard)"
        )

    def test_value_update_applied_when_disk_value_matches_snapshot(self, tmp_path):
        """Negative control: the CAS guard does not fire when no sibling
        write has intervened (disk value == snapshot value). The local
        rotation lands on disk normally."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD")])

        jar = httpx.Cookies()
        jar.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        _set_cookie_value(jar, "__Secure-1PSIDTS", "OURS_NEW")

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "OURS_NEW"

    def test_newly_acquired_cookie_writes_through_when_no_disk_entry(self, tmp_path):
        """New cookies (not in the snapshot) without a same-key disk entry
        are appended normally — CAS doesn't apply because there's nothing
        to compare against."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [])

        jar = httpx.Cookies()
        empty = snapshot_cookie_jar(jar)
        jar.set("__Secure-1PSIDTS", "NEW", domain=".google.com", path="/")

        save_cookies_to_storage(jar, storage, original_snapshot=empty)

        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "NEW"

    def test_newly_acquired_cookie_does_not_overwrite_sibling_same_key(self, tmp_path):
        """Expected-absent CAS: if disk has the row, a sibling acquired it first."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "SIBLING")])

        jar = httpx.Cookies()
        empty = snapshot_cookie_jar(jar)
        jar.set("__Secure-1PSIDTS", "OURS", domain=".google.com", path="/")

        result = save_cookies_to_storage(jar, storage, original_snapshot=empty)

        assert result is False
        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "SIBLING"


class TestRefreshCmdResnapshot:
    """When ``NOTEBOOKLM_REFRESH_CMD`` runs and wholesale-replaces the
    cookie jar, the pre-fetch snapshot no longer describes the baseline.
    ``AuthTokens.from_storage`` and ``fetch_tokens_with_domains`` must
    re-snapshot the jar so the subsequent save computes deltas against the
    refreshed state, not the stale pre-refresh state. Without this, every
    rotated cookie would look like a process-local delta and clobber any
    sibling-process write that landed in the refresh window.
    """

    @pytest.mark.asyncio
    async def test_fetch_tokens_with_domains_re_snapshots_after_refresh(
        self, tmp_path, monkeypatch
    ):
        from notebooklm import auth as auth_mod

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "pre"),
                _stored_cookie("__Secure-1PSIDTS", "pre"),
            ],
        )

        # Stub the token fetch to return refreshed=True and mutate the jar
        # in place (mirroring _replace_cookie_jar after NOTEBOOKLM_REFRESH_CMD).
        async def fake_fetch_with_refresh(cookie_jar, storage_path, profile, *, authuser=0):
            # Simulate the wholesale jar swap: clear & repopulate with new values.
            cookie_jar.jar.clear()
            cookie_jar.set("SID", "post", domain=".google.com", path="/")
            cookie_jar.set("__Secure-1PSIDTS", "post_refresh", domain=".google.com", path="/")
            # Return the post-replace snapshot as the 4th element, matching
            # the real function's contract.
            return ("csrf", "sid", True, snapshot_cookie_jar(cookie_jar))

        monkeypatch.setattr(auth_mod, "_fetch_tokens_with_refresh", fake_fetch_with_refresh)

        captured_snapshots: list = []
        real_save = auth_mod.save_cookies_to_storage

        def capture_save(jar, path, *, original_snapshot=None, **kwargs):
            captured_snapshots.append(original_snapshot)
            return real_save(jar, path, original_snapshot=original_snapshot, **kwargs)

        monkeypatch.setattr(auth_mod, "save_cookies_to_storage", capture_save)

        await auth_mod.fetch_tokens_with_domains(path=storage)

        assert len(captured_snapshots) == 1
        snapshot = captured_snapshots[0]
        # The snapshot passed to save must describe the POST-refresh jar
        # state (so deltas come out empty/minimal). If the re-snapshot line
        # is missing, the snapshot would still hold the pre-refresh ``pre``
        # values and the resulting delta would mass-rewrite disk.
        key = CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
        assert key in snapshot, "snapshot must include the post-refresh PSIDTS key"
        assert (
            snapshot[key].value == "post_refresh"
        ), f"snapshot must reflect the post-refresh jar state, got {snapshot[key].value!r}"

    @pytest.mark.asyncio
    async def test_auth_tokens_from_storage_re_snapshots_after_refresh(self, tmp_path, monkeypatch):
        from notebooklm import auth as auth_mod

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "pre"),
                _stored_cookie("__Secure-1PSIDTS", "pre"),
            ],
        )

        async def fake_fetch_with_refresh(cookie_jar, storage_path, profile, *, authuser=0):
            cookie_jar.jar.clear()
            cookie_jar.set("SID", "post", domain=".google.com", path="/")
            cookie_jar.set("__Secure-1PSIDTS", "post_refresh", domain=".google.com", path="/")
            return ("csrf", "sid", True, snapshot_cookie_jar(cookie_jar))

        monkeypatch.setattr(auth_mod, "_fetch_tokens_with_refresh", fake_fetch_with_refresh)

        captured_snapshots: list = []
        real_save = auth_mod.save_cookies_to_storage

        def capture_save(jar, path, *, original_snapshot=None, **kwargs):
            captured_snapshots.append(original_snapshot)
            return real_save(jar, path, original_snapshot=original_snapshot, **kwargs)

        monkeypatch.setattr(auth_mod, "save_cookies_to_storage", capture_save)

        await auth_mod.AuthTokens.from_storage(path=storage)

        assert len(captured_snapshots) == 1
        snapshot = captured_snapshots[0]
        key = CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
        assert key in snapshot
        assert snapshot[key].value == "post_refresh", (
            "AuthTokens.from_storage must re-snapshot after NOTEBOOKLM_REFRESH_CMD "
            "fires; otherwise the post-refresh save sees a pre-refresh baseline "
            "and treats every refreshed cookie as a process-local delta"
        )


class TestNoneValuedCookieIsTreatedAsDeletion:
    """An in-jar cookie whose ``cookie.value is None`` must NOT be written
    to disk as ``"value": null`` — that would yield a malformed
    ``storage_state.json`` row that subsequent loads reject. The
    snapshot/index filter coalesces ``None`` to "missing" so a value-less
    cookie falls through the deletion path (and the deletion CAS-guard
    governs whether the disk row drops).
    """

    def test_none_value_cookie_does_not_write_null_to_disk(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "real")])

        jar = httpx.Cookies()
        jar.set("SID", "real", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        # Mutate the live cookie's value to None — simulates httpx accepting
        # a malformed upstream Set-Cookie or programmatic clearing that
        # leaves the cookie object in the jar with no value.
        _set_cookie_value(jar, "SID", None)  # type: ignore[arg-type]

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        # Disk must not carry any ``"value": null`` row.
        for stored in _read_cookies(storage):
            assert (
                stored.get("value") is not None
            ), f"None-valued cookie must never be persisted as value:null, got: {stored}"

    def test_none_value_cookie_treated_as_deletion_under_cas(self, tmp_path):
        """Coalescing None → missing means the key becomes a deletion
        candidate. Because disk still matches our snapshot value, the
        deletion CAS guard permits the drop."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "real")])

        jar = httpx.Cookies()
        jar.set("SID", "real", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        _set_cookie_value(jar, "SID", None)  # type: ignore[arg-type]

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert _cookie_value(storage, "SID", ".google.com") is None, (
            "None-valued cookie should engage the deletion path; with disk "
            "value matching the snapshot, the CAS guard permits the drop"
        )


class TestFlockUnavailableWarning:
    """When the cross-process file lock degrades silently (NFS without
    flock support, read-only parent dir, fd exhaustion), production
    operators need a visible WARNING. Without the lock, the snapshot/delta
    CAS is the only line of defense against lost updates — and operators
    have no way to know they're in that mode if the degradation logs only
    at DEBUG.

    The warning is emitted at most once per process so steady-state NFS
    deployments don't flood logs.
    """

    def test_warning_emitted_when_lock_unavailable(self, tmp_path, monkeypatch, caplog):
        import contextlib as _contextlib
        import logging as _logging

        from notebooklm import auth as auth_mod

        # Reset the one-shot guard so this test isn't dependent on test order.
        monkeypatch.setattr(auth_mod, "_FLOCK_UNAVAILABLE_WARNED", False)

        @_contextlib.contextmanager
        def unavailable_lock(lock_path, *, blocking, log_prefix):
            yield "unavailable"

        monkeypatch.setattr(auth_mod, "_file_lock", unavailable_lock)

        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "v", http_only=False)])
        jar = httpx.Cookies()
        jar.set("SID", "v", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        with caplog.at_level(_logging.WARNING, logger="notebooklm.auth"):
            save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        unavailable_warnings = [
            r for r in caplog.records if "lock unavailable" in r.message.lower()
        ]
        assert len(unavailable_warnings) == 1, (
            "First observation of lock unavailable must emit exactly one WARNING; "
            f"got {len(unavailable_warnings)}: {[r.message for r in unavailable_warnings]}"
        )

    def test_warning_emitted_only_once_per_process(self, tmp_path, monkeypatch, caplog):
        """Steady-state NFS deployment: don't flood logs once the operator
        knows. After the first WARNING the guard suppresses further ones."""
        import contextlib as _contextlib
        import logging as _logging

        from notebooklm import auth as auth_mod

        monkeypatch.setattr(auth_mod, "_FLOCK_UNAVAILABLE_WARNED", False)

        @_contextlib.contextmanager
        def unavailable_lock(lock_path, *, blocking, log_prefix):
            yield "unavailable"

        monkeypatch.setattr(auth_mod, "_file_lock", unavailable_lock)

        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "v", http_only=False)])
        jar = httpx.Cookies()
        jar.set("SID", "v", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        with caplog.at_level(_logging.WARNING, logger="notebooklm.auth"):
            for _ in range(3):
                save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        unavailable_warnings = [
            r for r in caplog.records if "lock unavailable" in r.message.lower()
        ]
        assert len(unavailable_warnings) == 1, (
            f"Lock-unavailable WARNING must be one-shot; got "
            f"{len(unavailable_warnings)} warnings across 3 saves"
        )


class TestBaselineNotAdvancedOnSaveFailure:
    """``ClientCore.save_cookies`` only advances ``_loaded_cookie_snapshot``
    when the underlying ``save_cookies_to_storage`` call succeeded. This
    is the load-bearing invariant: on save failure the next save must
    retry the same delta against the original baseline.
    """

    @pytest.mark.asyncio
    async def test_baseline_unchanged_when_save_returns_false(self, tmp_path, monkeypatch):
        from notebooklm.client import NotebookLMClient

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "sid"),
                _stored_cookie("__Secure-1PSIDTS", "psidts"),
            ],
        )

        auth = AuthTokens(
            cookies={
                ("SID", ".google.com"): "sid",
                ("__Secure-1PSIDTS", ".google.com"): "psidts",
            },
            csrf_token="csrf",
            session_id="sid",
            storage_path=storage,
        )

        client = NotebookLMClient(auth)

        # Make every save_cookies_to_storage call return False (silent failure).
        def silent_fail(jar, path, **kwargs):
            return False

        monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", silent_fail)

        async with client:
            baseline_before = client._core._loaded_cookie_snapshot
            assert client._core._http_client is not None
            await client._core.save_cookies(client._core._http_client.cookies)
            baseline_after = client._core._loaded_cookie_snapshot

        assert baseline_after is baseline_before, (
            "save_cookies must NOT advance _loaded_cookie_snapshot when the "
            "underlying save returned False (silent disk-write failure)"
        )

    @pytest.mark.asyncio
    async def test_auth_tokens_from_storage_carries_failed_save_baseline(
        self, tmp_path, monkeypatch
    ):
        """Pre-client fetch rotations must be retried if their save fails."""
        from notebooklm import auth as auth_mod
        from notebooklm._core import ClientCore

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "sid"),
                _stored_cookie("__Secure-1PSIDTS", "old"),
            ],
        )

        async def fake_fetch_with_refresh(cookie_jar, storage_path, profile, *, authuser=0):
            _set_cookie_value(cookie_jar, "__Secure-1PSIDTS", "mutated")
            return ("csrf", "session", False, None)

        def failed_save(jar, path, *, original_snapshot=None, return_result=False):
            result = CookieSaveResult(False)
            return result if return_result else result.ok

        monkeypatch.setattr(auth_mod, "_fetch_tokens_with_refresh", fake_fetch_with_refresh)
        monkeypatch.setattr(auth_mod, "save_cookies_to_storage", failed_save)

        auth = await auth_mod.AuthTokens.from_storage(path=storage)
        core = ClientCore(auth)
        await core.open()
        try:
            key = CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
            assert auth.cookie_snapshot is not None
            assert auth.cookie_snapshot[key].value == "old"
            assert core._loaded_cookie_snapshot is not None
            assert core._loaded_cookie_snapshot[key].value == "old", (
                "ClientCore must inherit the pre-fetch baseline so the mutated "
                "cookie remains a delta after the failed pre-client save"
            )
        finally:
            await core.close()


class TestSchemaRejectReturnsFalse:
    """Storage file with a JSON dict that lacks a ``cookies`` key returns
    False (so caller doesn't advance baseline) and emits a WARNING so
    operators see the schema mismatch — otherwise rotation silently
    no-ops forever on a hand-edited storage_state.json."""

    def test_returns_false_and_warns_when_schema_invalid(self, tmp_path, caplog):
        import logging as _logging

        storage = tmp_path / "storage_state.json"
        # Valid JSON, but no 'cookies' key — common after a hand-edit that
        # kept only ``origins`` or after a Playwright format change.
        storage.write_text(json.dumps({"origins": []}))

        jar = httpx.Cookies()
        jar.set("SID", "new", domain=".google.com", path="/")
        snapshot: dict = {}

        with caplog.at_level(_logging.WARNING, logger="notebooklm.auth"):
            result = save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert result is False, "schema-reject must surface as False to gate baseline advance"
        assert any(
            "'cookies' key" in r.message for r in caplog.records
        ), "schema-reject must emit a WARNING describing the cause"


class TestNoTempFileLeakOnWriteFailure:
    """When the temp-file write fails (ENOSPC, EROFS), the partial temp
    file must be unlinked. A leak would, over time, fill the storage
    parent dir with .storage_state.json.<rand>.tmp debris — common in
    keepalive deployments where the same failure recurs every save.
    """

    def test_temp_file_unlinked_when_write_raises(self, tmp_path, monkeypatch):
        import tempfile

        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("SID", "old", http_only=False)])
        jar = httpx.Cookies()
        jar.set("SID", "old", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)
        _set_cookie_value(jar, "SID", "new")

        real_namedtemp = tempfile.NamedTemporaryFile

        def boom_namedtemp(*args, **kwargs):
            handle = real_namedtemp(*args, **kwargs)
            handle.write = lambda *a, **k: (_ for _ in ()).throw(OSError("simulated ENOSPC"))
            return handle

        monkeypatch.setattr("notebooklm.auth.tempfile.NamedTemporaryFile", boom_namedtemp)

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        leftover = list(tmp_path.glob(".storage_state.json.*.tmp"))
        assert (
            leftover == []
        ), f"temp file must be cleaned up when write fails; found leftovers: {leftover}"


class TestCASRejectReturnsFalse:
    """A CAS-rejected value update means disk does NOT reflect our intent.
    save_cookies_to_storage must return False so the caller does not
    advance its baseline to a state that disagrees with disk. Returning
    True after CAS-reject would permanently lose the local delta —
    every subsequent save would compute its delta against ``post`` (the
    state we wanted to write) but disk would still hold the sibling's
    value, and the in-memory rotation would never reach disk again.
    """

    def test_returns_false_when_value_update_cas_rejects(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD")])

        jar = httpx.Cookies()
        jar.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        # Sibling writes a different value.
        cookies = _read_cookies(storage)
        cookies[0]["value"] = "SIBLING"
        _write_storage(storage, cookies)

        # We rotate locally; our delta would clobber SIBLING but CAS rejects.
        _set_cookie_value(jar, "__Secure-1PSIDTS", "OURS")

        result = save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert result is False, (
            "CAS rejection means disk does not reflect our intent — must return "
            "False so caller does not advance baseline to a state disagreeing "
            "with disk"
        )

    def test_returns_false_when_deletion_cas_rejects(self, tmp_path):
        """Same invariant for the deletion CAS arm."""
        storage = tmp_path / "storage_state.json"
        _write_storage(storage, [_stored_cookie("__Secure-1PSIDTS", "OLD")])

        jar = httpx.Cookies()
        jar.set("__Secure-1PSIDTS", "OLD", domain=".google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        # Sibling rewrites the disk row.
        cookies = _read_cookies(storage)
        cookies[0]["value"] = "SIBLING"
        _write_storage(storage, cookies)

        # We evict locally (deletion candidate).
        jar.delete("__Secure-1PSIDTS", domain=".google.com", path="/")

        result = save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert result is False, (
            "Deletion CAS rejection means disk row was not dropped as we "
            "intended; must return False"
        )

    @pytest.mark.asyncio
    async def test_partial_cas_advances_successful_keys_for_next_save(self, tmp_path):
        """A mixed save should not replay successful deltas on later saves."""
        from notebooklm._core import ClientCore

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "sid0"),
                _stored_cookie("__Secure-1PSIDTS", "psidts0"),
            ],
        )
        auth = AuthTokens(
            cookies={
                ("SID", ".google.com"): "sid0",
                ("__Secure-1PSIDTS", ".google.com"): "psidts0",
            },
            csrf_token="t",
            session_id="s",
            storage_path=storage,
        )
        core = ClientCore(auth)
        await core.open()

        def jar_with(sid_value: str) -> httpx.Cookies:
            jar = httpx.Cookies()
            jar.set("SID", sid_value, domain=".google.com", path="/")
            jar.set("__Secure-1PSIDTS", "ours", domain=".google.com", path="/")
            return jar

        try:
            cookies = _read_cookies(storage)
            for cookie in cookies:
                if cookie["name"] == "__Secure-1PSIDTS":
                    cookie["value"] = "sibling"
            _write_storage(storage, cookies)

            await core.save_cookies(jar_with("sid1"))
            assert _cookie_value(storage, "SID", ".google.com") == "sid1"
            assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "sibling"

            await core.save_cookies(jar_with("sid2"))
            assert _cookie_value(storage, "SID", ".google.com") == "sid2", (
                "The successful SID delta from the partial save must advance "
                "baseline; otherwise the next SID rotation CAS-rejects against "
                "the stale open-time baseline"
            )
            assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "sibling"
        finally:
            await core.close()


class TestCASVariantAware:
    """The CAS lookup must use leading-dot variants of the matched delta
    key, mirroring how the delta itself is matched against stored entries.
    Otherwise, a snapshot keyed on ``accounts.google.com`` and a delta
    keyed on ``.accounts.google.com`` (or vice versa) would see
    ``original_snapshot.get(matched_delta_key)`` return None, the CAS
    would be silently bypassed, and our delta would clobber any sibling
    value on disk.
    """

    def test_cas_protects_across_leading_dot_variant(self, tmp_path):
        storage = tmp_path / "storage_state.json"
        # Disk has the bare-host variant (no leading dot).
        _write_storage(
            storage,
            [_stored_cookie("OSID", "OLD", domain="accounts.google.com")],
        )

        # Our snapshot also captures the bare-host variant.
        jar = httpx.Cookies()
        jar.set("OSID", "OLD", domain="accounts.google.com", path="/")
        snapshot = snapshot_cookie_jar(jar)

        # Sibling rewrites disk.
        cookies = _read_cookies(storage)
        cookies[0]["value"] = "SIBLING"
        _write_storage(storage, cookies)

        # Locally we rotate AND the cookie ends up keyed with a leading dot
        # (simulating httpx normalization variance). Reset + re-set forces
        # the variant.
        jar.jar.clear()
        jar.set("OSID", "OURS", domain=".accounts.google.com", path="/")

        save_cookies_to_storage(jar, storage, original_snapshot=snapshot)

        assert _cookie_value(storage, "OSID", "accounts.google.com") == "SIBLING", (
            "CAS must be variant-aware: even when the matched_delta_key uses a "
            "leading-dot domain and the snapshot uses the bare host (or vice "
            "versa), the lookup must find the snapshot entry and apply CAS"
        )

    def test_rejected_variant_preserves_original_baseline_variant(self):
        """Baseline advancement must mirror the variant-aware CAS lookup."""
        bare_key = CookieSnapshotKey("OSID", "accounts.google.com", "/")
        dotted_key = CookieSnapshotKey("OSID", ".accounts.google.com", "/")
        original_snapshot = {
            bare_key: CookieSnapshotValue(
                value="OLD",
                expires=None,
                secure=False,
                http_only=True,
            )
        }
        post_save_snapshot = {
            dotted_key: CookieSnapshotValue(
                value="OURS",
                expires=None,
                secure=False,
                http_only=True,
            )
        }

        advanced = advance_cookie_snapshot_after_save(
            original_snapshot,
            post_save_snapshot,
            frozenset({dotted_key}),
        )

        assert advanced == original_snapshot, (
            "A CAS rejection reported for a leading-dot variant must preserve "
            "the original bare-host baseline, not drop the key and later treat "
            "the cookie as newly acquired"
        )

    @pytest.mark.asyncio
    async def test_variant_aware_cas_rejection_then_recovery_through_real_plumbing(
        self, tmp_path, monkeypatch
    ):
        """Composition of variant-aware CAS + variant-aware baseline through real plumbing.

        Wires the full ``AuthTokens.from_storage`` -> ``ClientCore`` ->
        ``save_cookies`` plumbing rather than driving the helpers directly,
        so this complements the unit-level coverage in
        ``test_rejected_variant_preserves_original_baseline_variant`` and
        ``test_cas_protects_across_leading_dot_variant``.

        Timeline:

        1. Disk row for ``OSID`` uses the bare-host domain
           ``accounts.google.com``.
        2. ``AuthTokens.from_storage`` loads the jar, then the mocked token
           fetch rotates ``OSID`` and re-keys it on the leading-dot variant
           ``.accounts.google.com`` (the domain variance the CAS / baseline
           code must paper over). Inside the same mock, a sibling process
           rewrites disk's bare-host row to a third value.
        3. The pre-client save runs through real
           ``save_cookies_to_storage``: the variant-aware CAS lookup spots
           the disk drift via the bare-host snapshot entry and rejects the
           dotted delta. ``from_storage`` then runs the real
           ``advance_cookie_snapshot_after_save``, which must preserve the
           bare-host baseline rather than dropping the key.
        4. A Set-Cookie aligns the in-memory jar to disk (``OSID`` reset to
           the sibling's value) and a second ``ClientCore.save_cookies``
           runs. With the variant-aware baseline preserved by step 3, the
           second save recognizes convergence, advances cleanly, and a later
           rotation can persist without re-clobbering the sibling write.
        """
        from notebooklm import auth as auth_mod
        from notebooklm._core import ClientCore

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "sid"),
                _stored_cookie("__Secure-1PSIDTS", "psidts"),
                _stored_cookie("OSID", "OLD", domain="accounts.google.com"),
            ],
        )

        async def fake_fetch_with_refresh(cookie_jar, storage_path, profile, *, authuser=0):
            # Drop the bare-host OSID from the jar and re-key it on the
            # leading-dot variant so the in-memory jar diverges from disk on
            # domain shape — the exact variance the variant-aware CAS lookup
            # has to bridge.
            cookie_jar.delete("OSID", domain="accounts.google.com")
            cookie_jar.set("OSID", "OURS", domain=".accounts.google.com", path="/")
            # Sibling-process write between snapshot and our save.
            cookies = _read_cookies(storage_path)
            for cookie in cookies:
                if cookie["name"] == "OSID":
                    cookie["value"] = "SIBLING"
            _write_storage(storage_path, cookies)
            return ("csrf", "session", False, None)

        monkeypatch.setattr(auth_mod, "_fetch_tokens_with_refresh", fake_fetch_with_refresh)

        # Pre-client save runs through the real save_cookies_to_storage; the
        # CAS rejection must keep SIBLING on disk and the variant-aware
        # baseline-preservation must end up with the bare-host snapshot.
        auth = await auth_mod.AuthTokens.from_storage(path=storage)

        assert _cookie_value(storage, "OSID", "accounts.google.com") == "SIBLING", (
            "First save must CAS-reject via the variant-aware lookup so the "
            "sibling-process write survives on disk"
        )
        bare_key = CookieSnapshotKey("OSID", "accounts.google.com", "/")
        dotted_key = CookieSnapshotKey("OSID", ".accounts.google.com", "/")
        assert auth.cookie_snapshot is not None
        assert auth.cookie_snapshot.get(bare_key) is not None
        assert auth.cookie_snapshot[bare_key].value == "OLD", (
            "advance_cookie_snapshot_after_save must preserve the bare-host "
            "baseline entry that originally covered the CAS-rejected dotted "
            "delta — otherwise the next save would treat OSID as newly "
            "acquired and the variant lookup would silently fail"
        )
        assert dotted_key not in auth.cookie_snapshot, (
            "The post-save snapshot's dotted variant must be popped when the "
            "bare-host baseline is restored, so the rejected delta isn't "
            "absorbed into the new baseline"
        )

        # Stand up the real ClientCore so the second save flows through
        # ClientCore.save_cookies (lock + to_thread + baseline advance), not
        # straight into save_cookies_to_storage.
        core = ClientCore(auth)
        await core.open()
        try:
            assert core._loaded_cookie_snapshot is not None
            assert core._loaded_cookie_snapshot[bare_key].value == "OLD", (
                "ClientCore.open must inherit the variant-aware preserved "
                "baseline from AuthTokens.cookie_snapshot"
            )

            # Set-Cookie aligns the in-memory dotted OSID with what disk now
            # holds. Run the second save through the real ClientCore plumbing.
            assert core._http_client is not None
            _set_cookie_value(core._http_client.cookies, "OSID", "SIBLING")
            await core.save_cookies(core._http_client.cookies)

            assert _cookie_value(storage, "OSID", "accounts.google.com") == "SIBLING", (
                "Second save must not re-clobber the sibling write — the "
                "variant-aware CAS lookup must still see the disk/baseline "
                "divergence through the leading-dot variant"
            )
            assert core._loaded_cookie_snapshot is not None
            assert core._loaded_cookie_snapshot.get(dotted_key) is not None
            assert core._loaded_cookie_snapshot[dotted_key].value == "SIBLING", (
                "After the second save, disk already matches the current "
                "dotted-variant jar value, so the save must recover from the "
                "prior CAS rejection and advance baseline to the converged "
                "value instead of keeping the stale OLD baseline forever"
            )

            _set_cookie_value(core._http_client.cookies, "OSID", "NEXT")
            await core.save_cookies(core._http_client.cookies)

            assert _cookie_value(storage, "OSID", "accounts.google.com") == "NEXT", (
                "After convergence advances the baseline, a later OSID "
                "rotation must persist through the variant-aware lookup"
            )
            assert core._loaded_cookie_snapshot is not None
            assert core._loaded_cookie_snapshot[dotted_key].value == "NEXT", (
                "The successful follow-up rotation should advance the dotted "
                "baseline to the value now reflected on disk"
            )
        finally:
            await core.close()


class TestSaveCookiesSeesLatestBaselineUnderContention:
    """``ClientCore.save_cookies`` captures ``original_snapshot`` on the
    loop thread BEFORE the worker thread acquires ``_save_lock``. If two
    saves are issued in rapid succession, the second can capture a stale
    baseline (the first hasn't completed its baseline-advance yet) — and
    if CAS then rejects the second's delta, the second would still
    return True (CAS-reject is suppressed in updated_count=0) and advance
    its own baseline to a state that disagrees with disk.

    The fix: capture ``original_snapshot`` INSIDE the worker thread under
    the lock, so each save sees the latest baseline. Combined with the
    CAS-rejected-returns-False fix, this closes the lost-update window
    on rapid back-to-back saves (close() racing a mid-flight keepalive
    save is the common case).
    """

    @pytest.mark.asyncio
    async def test_concurrent_saves_each_see_latest_baseline(self, tmp_path, monkeypatch):
        """Two saves submitted via ``asyncio.gather`` overlap on the loop
        thread before either worker runs. If ``original_snapshot`` is
        captured on the loop thread (the bug), both workers see the same
        stale baseline. If captured inside the lock (the fix), the
        second worker observes the first's advance.
        """
        import asyncio

        from notebooklm import auth as auth_mod
        from notebooklm._core import ClientCore

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "sid"),
                _stored_cookie("__Secure-1PSIDTS", "v0"),
            ],
        )

        auth = AuthTokens(
            cookies={
                ("SID", ".google.com"): "sid",
                ("__Secure-1PSIDTS", ".google.com"): "v0",
            },
            csrf_token="t",
            session_id="s",
            storage_path=storage,
        )
        core = ClientCore(auth)
        await core.open()

        captured_calls: list[tuple[str, dict | None]] = []
        real_save = auth_mod.save_cookies_to_storage

        def capture_save(jar, path, *, original_snapshot=None, **kwargs):
            psidts_value = next(
                cookie.value for cookie in jar.jar if cookie.name == "__Secure-1PSIDTS"
            )
            captured_calls.append(
                (
                    psidts_value,
                    dict(original_snapshot) if original_snapshot is not None else None,
                )
            )
            return real_save(jar, path, original_snapshot=original_snapshot, **kwargs)

        monkeypatch.setattr("notebooklm._core.save_cookies_to_storage", capture_save)

        # Explicit barrier: each coroutine records its submission and the
        # second arrival sets ``both_submitted``; both then ``await`` the
        # event before running ``func``. Because asyncio coroutines on one
        # loop don't preempt, the ``append``/``set`` pair is already atomic
        # from each coroutine's POV — no extra lock needed. The barrier is
        # an ``asyncio.Event`` (suspending wait) rather than ``threading.Event``
        # (busy-poll) so the test doesn't depend on the loop preferring to
        # resume a particular task. Once ``both_submitted`` is set, both
        # coroutines proceed regardless of resume order — free-threaded
        # CPython, uvloop, or any future loop implementation must honour
        # this barrier.
        submitted: list[tuple] = []
        both_submitted = asyncio.Event()

        async def fake_to_thread(func, /, *args, **kwargs):
            submitted.append((func, args, kwargs))
            if len(submitted) == 2:
                both_submitted.set()
            await both_submitted.wait()
            return func(*args, **kwargs)

        monkeypatch.setattr("notebooklm._core.asyncio.to_thread", fake_to_thread)

        # Two jars representing distinct post-rotation states. The save that
        # acquires ``_save_lock`` first rotates *PSIDTS away from v0; the
        # second must then observe that rotation as its baseline. Built with
        # fresh jar.set() calls so the Cookie objects are NOT aliased between
        # jars (httpx's ``Cookies(other)`` copy-constructor re-uses Cookie
        # object refs). Submitted via gather so both save_cookies()
        # coroutines reach their asyncio.to_thread before either worker
        # advances the baseline.
        #
        # Note on naming: ``jar_a`` / ``jar_b`` are intentionally
        # call-order-agnostic. After the barrier releases, either jar may end
        # up as ``captured_calls[0]`` depending on which coroutine the loop
        # resumes first — that ordering is precisely what we DON'T want this
        # test to depend on. The assertion below uses positional names
        # (first/second by worker execution order, not by gather argument
        # order) to stay robust across schedulers.
        assert core._http_client is not None

        def _fresh_jar(psidts_value: str) -> httpx.Cookies:
            j = httpx.Cookies()
            j.set("SID", "sid", domain=".google.com", path="/")
            j.set("__Secure-1PSIDTS", psidts_value, domain=".google.com", path="/")
            return j

        jar_a = _fresh_jar("v1")
        jar_b = _fresh_jar("v2")

        try:
            await asyncio.gather(
                core.save_cookies(jar_a),
                core.save_cookies(jar_b),
            )
        finally:
            await core.close()

        psidts_key = CookieSnapshotKey("__Secure-1PSIDTS", ".google.com", "/")
        captured_pairs = [
            (jar_value, baseline[psidts_key].value)
            for jar_value, baseline in captured_calls
            if baseline is not None
        ]
        first_jar, first_baseline = captured_pairs[0]
        _second_jar, second_baseline = captured_pairs[1]
        assert first_baseline == "v0" and second_baseline == first_jar, (
            "When two saves serialize through _save_lock, the second worker "
            "must observe the baseline advanced by the first. Observed "
            f"(jar, baseline) pairs for *PSIDTS: {captured_pairs}. "
            "If both baselines are v0, the snapshot was captured on the "
            "loop thread before the lock — a stale-baseline race risking "
            "lost updates."
        )


class TestRefreshCmdSnapshotCapturedBeforeRetryFetch:
    """When ``NOTEBOOKLM_REFRESH_CMD`` runs, the post-replace jar is the
    new baseline — NOT the post-retry-fetch jar. The retry call to
    ``_fetch_tokens_with_jar`` can mutate the jar with redirect Set-Cookies,
    and those rotations must end up in the save's delta (so they reach
    disk), not in the baseline (where they would be silently dropped).
    """

    @pytest.mark.asyncio
    async def test_retry_fetch_rotations_persist_to_disk(self, tmp_path, monkeypatch):
        from notebooklm import auth as auth_mod

        storage = tmp_path / "storage_state.json"
        _write_storage(
            storage,
            [
                _stored_cookie("SID", "stale"),
                _stored_cookie("__Secure-1PSIDTS", "stale"),
            ],
        )

        monkeypatch.setenv(auth_mod.NOTEBOOKLM_REFRESH_CMD_ENV, "dummy-refresh")
        auth_mod._REFRESH_GENERATIONS.clear()

        async def fake_run_refresh_cmd(storage_path, profile):
            _write_storage(
                storage_path,
                [
                    _stored_cookie("SID", "from_refresh_cmd"),
                    _stored_cookie("__Secure-1PSIDTS", "from_refresh_cmd"),
                ],
            )

        fetch_calls = 0

        async def fake_fetch_tokens_with_jar(cookie_jar, storage_path, *, authuser=0):
            nonlocal fetch_calls
            fetch_calls += 1
            if fetch_calls == 1:
                raise ValueError("Authentication expired. Run 'notebooklm login'.")
            _set_cookie_value(cookie_jar, "__Secure-1PSIDTS", "post_retry_rotation")
            return ("csrf", "sid")

        monkeypatch.setattr(auth_mod, "_run_refresh_cmd", fake_run_refresh_cmd)
        monkeypatch.setattr(auth_mod, "_fetch_tokens_with_jar", fake_fetch_tokens_with_jar)

        await auth_mod.fetch_tokens_with_domains(path=storage)

        assert fetch_calls == 2
        assert _cookie_value(storage, "__Secure-1PSIDTS", ".google.com") == "post_retry_rotation", (
            "Rotations the retry fetch added to the jar must reach disk — they "
            "would be dropped if the baseline snapshot is captured after the "
            "retry instead of after _replace_cookie_jar"
        )
