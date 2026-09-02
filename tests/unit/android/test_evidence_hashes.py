"""Integrity checks for repository-local Android evidence identities."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
LEDGER = REPO_ROOT / "docs" / "android" / "proto-evidence-ledger.md"
LINKED_HASH = re.compile(
    r"\[`[^`]+`\]\((?P<target>[^)]+)\)"
    r"(?:\s*\|\s*|\s*\n?\(SHA-256\s+)`(?P<digest>[0-9a-f]{64})`",
    re.MULTILINE,
)


def test_every_linked_evidence_hash_matches_checked_in_bytes() -> None:
    rows = list(LINKED_HASH.finditer(LEDGER.read_text(encoding="utf-8")))
    assert len(rows) == 20

    for row in rows:
        target, _separator, _anchor = row.group("target").partition("#")
        evidence_path = (LEDGER.parent / target).resolve()
        assert evidence_path.is_relative_to(REPO_ROOT)
        assert evidence_path.is_file()
        assert hashlib.sha256(evidence_path.read_bytes()).hexdigest() == row.group("digest")
