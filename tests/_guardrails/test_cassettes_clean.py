"""Run the cassette-cleanliness guard inside ``pytest`` (defense-in-depth).

The strict cassette guard already runs in CI
(``.github/workflows/test.yml``: ``check_cassettes_clean.py --strict
--recursive`` plus a ``--secrets-only`` scan of ``tests/fixtures``), but it is
**not** part of the ``pytest`` suite. A contributor running ``uv run pytest``
locally — the common workflow — would not catch a recorded credential leak
until CI. These tests run the *same script* with the *same arguments* as CI,
so the local ``pytest`` run enforces the identical gate and the two cannot
drift (issue #1292).

The guard is invoked as a **subprocess** (not imported) on purpose:
``check_cassettes_clean.py`` bootstraps ``sys.path`` and imports the
``tests.*`` namespace for its pattern registry; doing that in-process would
register a ``tests`` package in ``sys.modules`` and break sibling tests that
spawn their own in-process pytest runs (e.g. ``test_tier_enforcement_hook``).
A child process keeps that bootstrap fully isolated.

``is_clean()`` is necessary-not-sufficient — it is name/shape-anchored, so a
credential family it doesn't yet know about can pass silently. This guard is
one layer; GitHub secret-scanning remains the backstop (see ADR-0006 and
``tests/cassette_patterns.py``).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from tests.cassette_patterns import find_credential_leaks, is_clean

pytestmark = pytest.mark.repo_lint

REPO_ROOT = Path(__file__).resolve().parents[2]
CHECKER = REPO_ROOT / "tests" / "scripts" / "check_cassettes_clean.py"
#: Every directory that holds committed response fixtures. ``tests/fixtures``
#: was the only one scanned until #2120 landed live-captured payloads in
#: ``tests/unit/fixtures`` — a directory the CI secrets step did not cover, and
#: whose first new fixture arrived carrying signed Google download URLs and a
#: Drive capability token. Adding a fixture directory means adding it here.
FIXTURE_DIRS = (
    REPO_ROOT / "tests" / "fixtures",
    REPO_ROOT / "tests" / "unit" / "fixtures",
)
FIXTURES_DIR = FIXTURE_DIRS[0]
# Deliberately-unscrubbed fixture used as a positive control (a real-looking
# ``SID`` cookie value beginning with ``S`` — the exact shape the legacy
# "starts with S" heuristic missed; see the file's own header comment).
KNOWN_BAD_CASSETTE = FIXTURES_DIR / "bad_cassettes" / "bad_sid_starting_with_s.yaml"


def _run_guard(*args: str) -> subprocess.CompletedProcess[str]:
    """Invoke ``check_cassettes_clean.py`` in a child process (exit 0 == clean)."""
    return subprocess.run(
        [sys.executable, str(CHECKER), *args],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_all_cassettes_clean_strict_recursive() -> None:
    """Every recorded cassette is clean under ``--strict --recursive``.

    Mirrors the blocking CI step verbatim. ``--strict`` additionally requires
    the repair allowlist (``tests/scripts/cassette_repair_allowlist.txt``) to be
    empty, so this also fails if the allowlist quietly regrows.
    """
    result = _run_guard("--strict", "--recursive")
    assert result.returncode == 0, (
        f"cassette guard reported leaks:\n{result.stdout}\n{result.stderr}"
    )


@pytest.mark.parametrize("fixture_dir", FIXTURE_DIRS, ids=lambda d: d.name and str(d.parent.name))
def test_fixture_dirs_have_no_credential_shapes(fixture_dir: Path) -> None:
    """No Google auth-token / API-key shapes in any committed fixture directory.

    Mirrors the CI ``--secrets-only --recursive`` step. This catches a
    credential shape smuggled into a non-cassette fixture (golden ``.json`` /
    ``.html`` / ``.txt``) that the cassette-only ``.yaml`` scan above would
    miss.

    ``tests/unit/fixtures`` joined the sweep in #2120: it had never been
    scanned, and the live-captured source payload added there arrived carrying
    signed ``usercontent.google.com`` download URLs and a ``drive.google.com``
    capability token in its descriptor row. Those are scrubbed now, but the
    reason they survived review is that nothing was looking.
    """
    assert fixture_dir.is_dir(), f"fixture directory missing: {fixture_dir}"
    result = _run_guard("--secrets-only", "--recursive", str(fixture_dir))
    assert result.returncode == 0, (
        f"credential shape found under {fixture_dir}:\n{result.stdout}\n{result.stderr}"
    )


def test_guard_detects_a_known_bad_cassette() -> None:
    """Positive control: the guard must actually flag a known leak.

    Without this, the clean-scan assertions above could pass even if the
    scanner silently regressed into a no-op (e.g. a broken pattern import).
    Scanning the deliberately-unscrubbed fixture must return a non-zero exit
    **and** name the file in the leak report — a crash in the checker also
    exits non-zero, so the exit code alone can't prove a leak was detected.
    """
    assert KNOWN_BAD_CASSETTE.exists(), f"missing positive-control fixture: {KNOWN_BAD_CASSETTE}"
    result = _run_guard(str(KNOWN_BAD_CASSETTE))
    assert result.returncode == 1, (
        f"guard failed to flag a known leak (exit {result.returncode}):\n{result.stdout}"
    )
    assert KNOWN_BAD_CASSETTE.name in result.stdout, (
        f"exit 1 but the leak was not reported — the guard may have crashed "
        f"rather than detected:\n{result.stdout}\n{result.stderr}"
    )


def test_guard_detects_credential_shapes(tmp_path: Path) -> None:
    """Positive control for the ``--secrets-only`` path, across every shape.

    Covers all five known credential-shape detectors (the Google API key plus
    the ``aas_et/`` / ``g.a000-`` / ``sidts-`` / ``ya29.`` auth-token shapes), so a single
    dead detector is caught. Shapes are assembled at runtime so this source file
    carries no static credential-shaped literal that the repo-wide secrets scan
    would flag. Asserting one reported leak *per shape* (not merely a non-zero
    exit, which a scanner crash also produces) keeps the control robust.
    """
    shapes = (
        "AIza" + "B" * 35,  # Google API key
        "aas_" + "et/" + "M" * 20,  # durable master token
        "g.a" + "000-" + "D" * 20,  # g.a000- SID token
        "sid" + "ts-" + "E" * 20,  # sidts- rotation token
        "ya2" + "9." + "C" * 40,  # ya29. OAuth access token
    )
    leaky = tmp_path / "leaky.txt"
    leaky.write_text(
        "".join(f"token_{i}: {shape}\n" for i, shape in enumerate(shapes)),
        encoding="utf-8",
    )
    result = _run_guard("--secrets-only", str(leaky))
    assert result.returncode == 1, f"shape detector missed a synthetic shape:\n{result.stdout}"
    reported = [line for line in result.stdout.splitlines() if "Leak (" in line]
    assert len(reported) >= len(shapes), (
        f"expected >= {len(shapes)} reported leaks (one per shape), got {len(reported)} — "
        f"a shape detector may be dead or the guard crashed:\n{result.stdout}\n{result.stderr}"
    )


def test_guard_detects_novel_high_entropy_token(tmp_path: Path) -> None:
    """Positive control for the generic high-entropy scan (issue #1382).

    A NOVEL credential shape — one matching none of the known prefixes above —
    riding in an unknown quoted JSON field must still be flagged by the
    field-agnostic entropy backstop. The token is assembled at runtime (a mixed
    base64 run, 40+ chars, high entropy) so it matches no static secret pattern
    in this source file. Asserting the detector fires here is what proves the
    entropy scan is live in the ``--secrets-only`` path, not merely defined.
    """
    novel = "kJ8sLm2NpQr5" + "TvWxYz0AbCdEf" + "GhIjKlMnOpQrSt" + "UvWxYz12345678"
    leaky = tmp_path / "novel.json"
    leaky.write_text(f'{{"unknownField":"{novel}"}}\n', encoding="utf-8")
    result = _run_guard("--secrets-only", str(leaky))
    assert result.returncode == 1, (
        f"entropy scan missed a planted novel high-entropy token:\n{result.stdout}"
    )
    assert "high-entropy token" in result.stdout, (
        f"exit 1 but no high-entropy leak reported — the entropy detector may be "
        f"dead or the guard crashed:\n{result.stdout}\n{result.stderr}"
    )


# --- Signed blob-capability URLs (#2120 / #2215) ---------------------------
# These must be caught in BOTH scan modes. ``--secrets-only`` reaches the
# detector via ``_CREDENTIAL_DETECTORS``; the full cassette scan routes through
# ``is_clean`` instead, so a detector registered in only one place leaves the
# other gate blind. The negative cases pin the query-delimiter anchoring: a
# ``\b`` boundary would match ``c=`` inside a *value* (``?redirect=-c=1``) and
# reject valid content.
_CAPABILITY_POSITIVE = [
    pytest.param(
        "https://contribution.usercontent.google.com/download?c=AIP70Bshort&filename=x.md",
        id="download-capability",
    ),
    pytest.param("https://drive.google.com/viewer/upload?ck=a&ds=1&p=2", id="viewer-wrapper"),
    pytest.param(
        "/blobstore/prod/contrib_service/blobrefs/notebooklm/nos_files/x", id="blobrefs-path"
    ),
    pytest.param(
        "https://contribution.usercontent.google.com/download?x=1&amp;c=AIP70B",
        id="amp-escaped-delimiter",
    ),
]

_CAPABILITY_NEGATIVE = [
    pytest.param(
        "https://contribution.usercontent.google.com/download?redirect=-c=1",
        id="c-inside-a-value-not-a-key",
    ),
    pytest.param(
        "https://drive.google.com/viewer/upload?redirect=-ds=1", id="ds-inside-a-value-not-a-key"
    ),
    pytest.param(
        "https://drive.google.com/file/d/1FDW_u2QKNxpYBBvs-k03F4n039Ufuiv2/view",
        id="benign-public-file-id",
    ),
]


@pytest.mark.parametrize("text", _CAPABILITY_POSITIVE)
def test_capability_urls_are_caught_in_both_scan_modes(text: str) -> None:
    """A signed capability must fail --secrets-only AND the full scan."""
    assert find_credential_leaks(text), "missed by --secrets-only (find_credential_leaks)"
    assert not is_clean(text)[0], "missed by the full cassette scan (is_clean)"


@pytest.mark.parametrize("text", _CAPABILITY_NEGATIVE)
def test_capability_detector_does_not_fire_on_lookalikes(text: str) -> None:
    """Parameter-like text inside a value is not a capability."""
    assert not find_credential_leaks(text), "false positive in --secrets-only"
    assert is_clean(text)[0], "false positive in the full cassette scan"
