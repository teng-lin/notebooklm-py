"""Tests for ``notebooklm._app.profile`` — the profile-management core.

Covers the Click-free profile logic backing ``profile list`` / ``delete`` /
``switch`` / ``rename``:

* :func:`gather_profile_list` — name → active/authenticated/account rows, with
  the profile/storage/account collaborators injected (no ``notebooklm.paths`` /
  ``notebooklm.auth`` reach-in); the empty-profiles case; account-email coercion.
* :func:`is_protected_profile` — the active/configured-default delete guard.
* :func:`set_default_profile_mutator` / :func:`retarget_default_profile_mutator`
  — the ``config.json`` mutator closures, the "missing default ⇒ 'default'" rule,
  the ``was_updated`` predicate, and corrupt-config recovery (the mutator runs
  under the adapter's lock against whatever dict it is handed).

Direct ``_app`` calls only — collaborators injected as plain callables /
``MagicMock``s, no Click / CliRunner.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import notebooklm.auth as auth_module
from notebooklm._app.profile import (
    ProfileAccountObservation,
    ProfileEntry,
    ProfileRepairOutcome,
    ProfileRepairRequest,
    ProfileTargetRequest,
    gather_profile_accounts,
    gather_profile_list,
    is_protected_profile,
    observe_profile_account,
    profile_account_email,
    profiles_by_account_email,
    repair_playwright_account,
    resolve_all_accounts_target,
    retarget_default_profile_mutator,
    set_default_profile_mutator,
)


def _fake_storage(*, exists: bool) -> MagicMock:
    storage = MagicMock(spec=Path)
    storage.exists.return_value = exists
    return storage


# ---------------------------------------------------------------------------
# gather_profile_list
# ---------------------------------------------------------------------------


def test_gather_profile_list_builds_rows() -> None:
    storages = {
        "default": _fake_storage(exists=True),
        "work": _fake_storage(exists=False),
    }
    accounts = {
        "default": {"email": "me@example.com"},
        "work": {},
    }

    def _get_storage_path(*, profile: str) -> Path:
        return storages[profile]

    def _read_account_metadata(path: Path) -> dict[str, Any]:
        # Look up by identity to map the injected storage back to its account.
        for name, storage in storages.items():
            if storage is path:
                return accounts[name]
        raise AssertionError("unexpected storage path")

    entries, active = gather_profile_list(
        list_profiles=lambda: ["default", "work"],
        resolve_profile=lambda: "default",
        get_storage_path=_get_storage_path,
        read_account_metadata=_read_account_metadata,
    )

    assert active == "default"
    assert entries == [
        ProfileEntry(name="default", active=True, authenticated=True, account="me@example.com"),
        ProfileEntry(name="work", active=False, authenticated=False, account=None),
    ]


def test_gather_profile_list_empty() -> None:
    """No profiles → empty rows but the active name still resolves."""
    entries, active = gather_profile_list(
        list_profiles=lambda: [],
        resolve_profile=lambda: "default",
        get_storage_path=lambda *, profile: _fake_storage(exists=False),
        read_account_metadata=lambda path: {},
    )

    assert entries == []
    assert active == "default"


def test_gather_profile_list_coerces_non_str_email_to_none() -> None:
    """A non-string ``email`` field is coerced to ``None`` (defensive)."""
    entries, _ = gather_profile_list(
        list_profiles=lambda: ["default"],
        resolve_profile=lambda: "default",
        get_storage_path=lambda *, profile: _fake_storage(exists=True),
        read_account_metadata=lambda path: {"email": 12345},
    )

    assert entries[0].account is None


def test_gather_profile_list_get_storage_called_per_profile() -> None:
    get_storage_path = MagicMock(side_effect=lambda *, profile: _fake_storage(exists=True))

    gather_profile_list(
        list_profiles=lambda: ["a", "b", "c"],
        resolve_profile=lambda: "a",
        get_storage_path=get_storage_path,
        read_account_metadata=lambda path: {},
    )

    assert get_storage_path.call_count == 3
    get_storage_path.assert_any_call(profile="a")
    get_storage_path.assert_any_call(profile="c")


def test_gather_profile_accounts_preserves_order_and_reads_before_exists(monkeypatch) -> None:
    events: list[object] = []
    storage = MagicMock(spec=Path)
    storage.exists.side_effect = lambda: events.append("exists") or True

    def _read(path: Path) -> dict[str, Any]:
        events.append(("read", path))
        return {"email": "alice@example.com"}

    monkeypatch.setattr(auth_module, "read_account_metadata", _read)

    entries, active = gather_profile_accounts(
        list_profiles=lambda: events.append("list") or ["work"],
        resolve_profile=lambda: events.append("resolve") or "work",
        get_storage_path=lambda profile=None: events.append(("path", profile)) or storage,
    )

    assert events == ["list", "resolve", ("path", "work"), ("read", storage), "exists"]
    assert entries == [
        ProfileEntry(
            name="work",
            active=True,
            authenticated=True,
            account="alice@example.com",
        )
    ]
    assert active == "work"


@pytest.mark.parametrize(
    ("metadata", "expected"),
    [
        (
            {"authuser": 0, "email": "alice@example.com"},
            ProfileAccountObservation("alice@example.com", "alice@example.com", True),
        ),
        ({"authuser": 1, "email": ""}, ProfileAccountObservation("", None, False)),
        ({"authuser": 2, "email": "   "}, ProfileAccountObservation("   ", "   ", False)),
        ({"authuser": True, "email": None}, ProfileAccountObservation(None, None, False)),
        ({"authuser": -1, "email": None}, ProfileAccountObservation(None, None, False)),
        ({"authuser": 0, "email": 7}, ProfileAccountObservation(None, None, False)),
        ({"authuser": 0}, ProfileAccountObservation(None, None, True)),
    ],
)
def test_observe_profile_account_projects_exact_rules(
    monkeypatch, metadata: dict[str, Any], expected: ProfileAccountObservation
) -> None:
    storage = Path("profile.json")
    original = dict(metadata)
    reader = MagicMock(return_value=metadata)
    monkeypatch.setattr(auth_module, "read_account_metadata", reader)

    assert observe_profile_account(storage) == expected
    reader.assert_called_once_with(storage)
    assert metadata == original


def test_profiles_by_account_email_first_casefolded_winner(monkeypatch) -> None:
    metadata = {
        "first": {"email": "Alice@Example.com"},
        "duplicate": {"email": "alice@example.COM"},
        "empty": {"email": ""},
        "other": {"email": "bob@example.com"},
    }
    path_calls: list[str | None] = []
    read_calls: list[Path] = []

    def _path(profile: str | None = None) -> Path:
        path_calls.append(profile)
        return Path(str(profile))

    def _read(path: Path) -> dict[str, Any]:
        read_calls.append(path)
        return metadata[str(path)]

    monkeypatch.setattr(auth_module, "read_account_metadata", _read)

    assert profiles_by_account_email(
        ["first", "duplicate", "empty", "other"], get_storage_path=_path
    ) == {
        "alice@example.com": "first",
        "bob@example.com": "other",
    }
    assert path_calls == ["first", "duplicate", "empty", "other"]
    assert read_calls == [Path(name) for name in path_calls]


def test_profile_account_email_preserves_nonempty_string(monkeypatch) -> None:
    reader = MagicMock(return_value={"email": "  Alice@Example.com  "})
    monkeypatch.setattr(auth_module, "read_account_metadata", reader)

    assert (
        profile_account_email("work", get_storage_path=lambda profile=None: Path(str(profile)))
        == "  Alice@Example.com  "
    )
    reader.assert_called_once_with(Path("work"))


@pytest.mark.parametrize(
    ("email", "expected"),
    [
        (None, "alice"),
        ("ALICE@example.com", "alice"),
        ("other@example.com", "alice-3"),
    ],
)
def test_resolve_all_accounts_target_positive_update_reads_once(
    monkeypatch, email: str | None, expected: str
) -> None:
    reader = MagicMock(return_value={} if email is None else {"email": email})
    path = MagicMock(return_value=Path("alice.json"))
    monkeypatch.setattr(auth_module, "read_account_metadata", reader)

    result = resolve_all_accounts_target(
        ProfileTargetRequest(
            base_name="alice",
            account_email="alice@example.com",
            existing_profiles=frozenset({"alice"}),
            unavailable=frozenset({"alice", "alice-2"}),
            claimed=frozenset(),
            update=True,
        ),
        get_storage_path=path,
    )

    assert result == expected
    path.assert_called_once_with(profile="alice")
    reader.assert_called_once_with(Path("alice.json"))


@pytest.mark.parametrize(
    ("update", "existing", "claimed"),
    [
        (False, frozenset({"alice"}), frozenset()),
        (True, frozenset(), frozenset()),
        (True, frozenset({"alice"}), frozenset({"alice"})),
    ],
)
def test_resolve_all_accounts_target_negative_arms_do_not_read(
    monkeypatch, update: bool, existing: frozenset[str], claimed: frozenset[str]
) -> None:
    reader = MagicMock(side_effect=AssertionError("metadata read must short-circuit"))
    path = MagicMock(side_effect=AssertionError("path read must short-circuit"))
    monkeypatch.setattr(auth_module, "read_account_metadata", reader)

    result = resolve_all_accounts_target(
        ProfileTargetRequest(
            base_name="alice",
            account_email="alice@example.com",
            existing_profiles=existing,
            unavailable=frozenset({"alice", "alice-2"}),
            claimed=claimed,
            update=update,
        ),
        get_storage_path=path,
    )

    assert result == "alice-3"
    path.assert_not_called()
    reader.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("public_result", "expected"),
    [
        (
            SimpleNamespace(written=True, email="alice@example.com", error=None),
            ProfileRepairOutcome(status="WRITTEN", email="alice@example.com"),
        ),
        (
            SimpleNamespace(written=False, email=None, error="network", ambiguity_reason=None),
            ProfileRepairOutcome(status="ERROR", detail="network"),
        ),
        (
            SimpleNamespace(written=False, email=None, error=None, ambiguity_reason="many"),
            ProfileRepairOutcome(status="AMBIGUOUS", detail="many"),
        ),
    ],
)
async def test_repair_playwright_account_projects_public_result_at_call_time(
    monkeypatch, public_result: object, expected: ProfileRepairOutcome
) -> None:
    calls: list[tuple[Path, str | None]] = []

    async def _repair(storage_path: Path, *, page_html: str | None = None):
        calls.append((storage_path, page_html))
        return public_result

    monkeypatch.setattr(auth_module, "repair_account_metadata_from_playwright_storage", _repair)
    request = ProfileRepairRequest(Path("profile.json"), page_html="<html>active</html>")

    assert await repair_playwright_account(request) == expected
    assert calls == [(Path("profile.json"), "<html>active</html>")]


@pytest.mark.asyncio
async def test_repair_playwright_account_preserves_unexpected_error_and_scrubs_frame(
    monkeypatch,
) -> None:
    error = KeyboardInterrupt("stop")

    async def _repair(storage_path: Path, *, page_html: str | None = None):
        raise error

    monkeypatch.setattr(auth_module, "repair_account_metadata_from_playwright_storage", _repair)

    with pytest.raises(KeyboardInterrupt) as raised:
        await repair_playwright_account(
            ProfileRepairRequest(Path("profile.json"), page_html="<html>active</html>")
        )

    assert raised.value is error
    frames = []
    traceback = raised.value.__traceback__
    while traceback is not None:
        if traceback.tb_frame.f_code.co_name == "repair_playwright_account":
            frames.append(traceback.tb_frame.f_locals)
        traceback = traceback.tb_next
    assert len(frames) == 1
    assert not {
        "request",
        "storage_path",
        "page_html",
        "repair_coroutine",
        "public_result",
        "outcome",
    } & set(frames[0])


# ---------------------------------------------------------------------------
# is_protected_profile
# ---------------------------------------------------------------------------


def test_is_protected_profile_blocks_active() -> None:
    assert (
        is_protected_profile("work", configured_default="default", effective_active="work") is True
    )


def test_is_protected_profile_blocks_configured_default() -> None:
    assert (
        is_protected_profile("default", configured_default="default", effective_active="work")
        is True
    )


def test_is_protected_profile_allows_other() -> None:
    assert (
        is_protected_profile("old", configured_default="default", effective_active="work") is False
    )


# ---------------------------------------------------------------------------
# set_default_profile_mutator
# ---------------------------------------------------------------------------


def test_set_default_profile_mutator_sets_key() -> None:
    mutator = set_default_profile_mutator("work")
    out = mutator({"default_profile": "default", "other": 1})
    assert out["default_profile"] == "work"
    # Other keys are preserved.
    assert out["other"] == 1


def test_set_default_profile_mutator_on_empty_config() -> None:
    mutator = set_default_profile_mutator("work")
    assert mutator({}) == {"default_profile": "work"}


# ---------------------------------------------------------------------------
# retarget_default_profile_mutator
# ---------------------------------------------------------------------------


def test_retarget_rewrites_matching_default() -> None:
    mutator, was_updated = retarget_default_profile_mutator(old_name="work", new_name="work-old")
    out = mutator({"default_profile": "work"})
    assert out["default_profile"] == "work-old"
    assert was_updated() is True


def test_retarget_missing_default_treated_as_default() -> None:
    """A config with no ``default_profile`` key implies the literal 'default'."""
    mutator, was_updated = retarget_default_profile_mutator(old_name="default", new_name="primary")
    out = mutator({})  # missing key ⇒ implicit "default"
    assert out["default_profile"] == "primary"
    assert was_updated() is True


def test_retarget_blank_default_treated_as_default() -> None:
    """An empty-string ``default_profile`` is also treated as 'default'."""
    mutator, was_updated = retarget_default_profile_mutator(old_name="default", new_name="primary")
    out = mutator({"default_profile": ""})
    assert out["default_profile"] == "primary"
    assert was_updated() is True


def test_retarget_noop_when_default_is_other() -> None:
    """Renaming a non-default profile leaves the config + predicate untouched."""
    mutator, was_updated = retarget_default_profile_mutator(old_name="work", new_name="work-old")
    out = mutator({"default_profile": "primary"})
    assert out["default_profile"] == "primary"
    assert was_updated() is False


def test_retarget_was_updated_false_before_run() -> None:
    """The predicate reports False until the mutator has actually fired."""
    _mutator, was_updated = retarget_default_profile_mutator(old_name="work", new_name="w2")
    assert was_updated() is False


def test_retarget_recovers_against_corrupt_recovered_dict() -> None:
    """Under corrupt-config recovery the adapter hands the mutator a fresh ``{}``.

    The locked writer recovers a corrupt config to an empty dict before applying
    the mutator; the missing-key ⇒ 'default' rule then still retargets a renamed
    'default' profile.
    """
    mutator, was_updated = retarget_default_profile_mutator(old_name="default", new_name="renamed")
    recovered: dict[str, Any] = {}  # what _atomic_write_config(recover_from_corrupt=True) passes
    out = mutator(recovered)
    assert out["default_profile"] == "renamed"
    assert was_updated() is True
