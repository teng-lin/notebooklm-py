"""Phase 2 P2.5 (slice) — pin ArtifactTypeCode usage consistency.

After P2.2 replaced inline `N,  # ArtifactTypeCode.FOO` literals with
`ArtifactTypeCode.FOO.value`, this test fails if anyone adds a new
literal-with-comment pattern (suggesting the migration regressed).
"""

import re
from pathlib import Path

ARTIFACTS_PATH = Path(__file__).resolve().parents[2] / "src" / "notebooklm" / "_artifacts.py"

# Forbid `N,  # ArtifactTypeCode.NAME` (integer + comment naming the enum).
# The replacement is `ArtifactTypeCode.NAME.value`.
FORBIDDEN_PATTERN = re.compile(
    r"^\s*\d+\s*,\s*#\s*ArtifactTypeCode\.[A-Z][A-Z0-9_]*\s*$",
    re.M,
)


def test_no_inline_artifact_type_code_literals_in_artifacts_py():
    text = ARTIFACTS_PATH.read_text(encoding="utf-8")
    matches = FORBIDDEN_PATTERN.findall(text)
    assert not matches, (
        f"Found {len(matches)} inline ArtifactTypeCode literal(s) in "
        f"_artifacts.py; replace with `ArtifactTypeCode.FOO.value`. "
        f"Matches:\n" + "\n".join(matches)
    )
