"""Run the cassette-cleanliness guard inside ``pytest`` (defense-in-depth).

The strict cassette guard already runs in CI
(``.github/workflows/test.yml``: ``check_cassettes_clean.py --strict
--recursive`` plus a ``--secrets-only`` scan of ``tests/fixtures``), but it is
**not** part of the ``pytest`` suite. A contributor running ``uv run pytest``
locally — the common workflow — would not catch a recorded credential leak
until CI. These tests invoke the *same* entrypoint
(:func:`tests.scripts.check_cassettes_clean.main`) with the *same* arguments,
so the local ``pytest`` run enforces the identical gate and the two cannot
drift (issue #1292).

``is_clean()`` is necessary-not-sufficient — it is name/shape-anchored, so a
credential family it doesn't yet know about can pass silently. This guard is
one layer; GitHub secret-scanning remains the backstop (see ADR-0006 and
``tests/cassette_patterns.py``).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# The cassette guard lives under ``tests/`` (not an installed package). Put the
# repo root on ``sys.path`` so the ``tests.*`` imports below resolve regardless
# of pytest's import mode — mirroring the bootstrap that
# ``tests/scripts/check_cassettes_clean.py`` performs for its own
# ``tests.cassette_patterns`` import.
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tests.cassette_patterns import find_credential_leaks  # noqa: E402
from tests.scripts.check_cassettes_clean import main  # noqa: E402

pytestmark = pytest.mark.repo_lint

FIXTURES_DIR = REPO_ROOT / "tests" / "fixtures"
# Deliberately-unscrubbed fixture used as a positive control (a real-looking
# ``SID`` cookie value beginning with ``S`` — the exact shape the legacy
# "starts with S" heuristic missed; see the file's own header comment).
KNOWN_BAD_CASSETTE = FIXTURES_DIR / "bad_cassettes" / "bad_sid_starting_with_s.yaml"


def test_all_cassettes_clean_strict_recursive() -> None:
    """Every recorded cassette is clean under ``--strict --recursive``.

    Mirrors the blocking CI step verbatim. ``--strict`` additionally requires
    the repair allowlist (``tests/scripts/cassette_repair_allowlist.txt``) to be
    empty, so this also fails if the allowlist quietly regrows.
    """
    assert main(["--strict", "--recursive"]) == 0


def test_fixture_dirs_have_no_credential_shapes() -> None:
    """No Google auth-token / API-key shapes anywhere under ``tests/fixtures``.

    Mirrors the CI ``--secrets-only --recursive tests/fixtures`` step. This
    catches a credential shape smuggled into a non-cassette fixture (golden
    ``.json`` / ``.html`` / ``.txt``) that the cassette-only ``.yaml`` scan
    above would miss.
    """
    assert main(["--secrets-only", "--recursive", str(FIXTURES_DIR)]) == 0


def test_guard_detects_a_known_bad_cassette() -> None:
    """Positive control: the guard must actually flag a known leak.

    Without this, the clean-scan assertions above could pass even if the
    scanner silently regressed into a no-op (e.g. a broken pattern import).
    Scanning the deliberately-unscrubbed fixture must return a non-zero exit.
    """
    assert KNOWN_BAD_CASSETTE.exists(), f"missing positive-control fixture: {KNOWN_BAD_CASSETTE}"
    assert main([str(KNOWN_BAD_CASSETTE)]) == 1


def test_credential_shape_detector_is_live() -> None:
    """Positive control for the ``--secrets-only`` path (find_credential_leaks).

    The shape strings are assembled at runtime so this source file carries no
    static credential-shaped literal (which the repo-wide secrets scan would
    otherwise flag).
    """
    assert find_credential_leaks("AIza" + "B" * 35), "Google API-key shape not detected"
    assert find_credential_leaks("ya2" + "9." + "C" * 40), "OAuth access-token shape not detected"
