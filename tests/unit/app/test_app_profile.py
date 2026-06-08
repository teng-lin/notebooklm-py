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
from typing import Any
from unittest.mock import MagicMock

from notebooklm._app.profile import (
    ProfileEntry,
    gather_profile_list,
    is_protected_profile,
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
