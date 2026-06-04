"""Unit tests for RPC types and constants."""

import ast
import re
import subprocess
import sys
from pathlib import Path

from notebooklm.rpc.types import (
    BATCHEXECUTE_URL,
    QUERY_URL,
    ArtifactStatus,
    ArtifactTypeCode,
    RPCMethod,
    SourceStatus,
    artifact_status_to_str,
    get_batchexecute_url,
    get_query_url,
    source_status_to_str,
)


def test_rpc_types_does_not_own_runtime_override_policy() -> None:
    """Runtime override env parsing belongs in rpc.overrides, not rpc.types."""
    path = Path(__file__).parents[2] / "src/notebooklm/rpc/types.py"
    tree = ast.parse(path.read_text())

    imported_os: list[int] = []
    environ_access: list[int] = []
    direct_override_defs: list[int] = []
    override_aliases: set[str] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "os":
                    imported_os.append(node.lineno)
        elif isinstance(node, ast.ImportFrom):
            if node.module == "os":
                imported_os.append(node.lineno)
            for alias in node.names:
                if (node.module, node.level) == ("overrides", 1):
                    override_aliases.add(alias.asname or alias.name)
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == "environ"
            and isinstance(node.value, ast.Name)
            and node.value.id == "os"
        ):
            environ_access.append(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in {
            "_parse_rpc_overrides",
            "_load_rpc_overrides",
        }:
            direct_override_defs.append(node.lineno)

    assert imported_os == []
    assert environ_access == []
    assert direct_override_defs == []
    assert {
        "_load_rpc_overrides",
        "_logged_override_hashes",
        "_parse_rpc_overrides",
        "resolve_rpc_id",
    } <= override_aliases


def test_rpc_override_import_order_smoke() -> None:
    """Both public and compatibility import orders must resolve cleanly."""
    snippets = [
        "from notebooklm.rpc import RPCMethod, resolve_rpc_id; "
        "assert resolve_rpc_id(RPCMethod.LIST_NOTEBOOKS.name, RPCMethod.LIST_NOTEBOOKS.value)",
        "from notebooklm.rpc.types import RPCMethod, resolve_rpc_id, _parse_rpc_overrides; "
        "assert resolve_rpc_id(RPCMethod.LIST_NOTEBOOKS.name, RPCMethod.LIST_NOTEBOOKS.value); "
        "assert hasattr(_parse_rpc_overrides, 'cache_clear')",
    ]
    for snippet in snippets:
        subprocess.run([sys.executable, "-c", snippet], check=True)


class TestRPCConstants:
    def test_batchexecute_url(self):
        """Test batchexecute URL is correct."""
        assert (
            BATCHEXECUTE_URL == "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
        )

    def test_query_url(self):
        """Test query URL for streaming chat."""
        assert "GenerateFreeFormStreamed" in QUERY_URL

    def test_endpoint_helpers_honor_env_after_import(self, monkeypatch):
        """Test lazy endpoint helpers are not locked to import-time env."""
        monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")

        assert get_batchexecute_url().startswith("https://notebooklm.cloud.google.com/")
        assert get_query_url().startswith("https://notebooklm.cloud.google.com/")


# Shape every ``RPCMethod`` value must satisfy. These are Google's obfuscated
# batchexecute method IDs — short, case-sensitive alphanumeric tokens. The
# length bound is deliberately loose: the real values are 5-6 chars today, and
# {4,12} leaves rotation headroom on both sides so a routine Google ID rotation
# does NOT force a test edit; the invariant catches the failure modes that
# actually matter: an empty/whitespace value, a structurally wrong token, or a
# duplicate ID silently aliasing two methods.
_RPC_ID_SHAPE = re.compile(r"^[A-Za-z0-9]{4,12}$")


class TestRPCMethod:
    """Structural invariant over the whole ``RPCMethod`` enum.

    Replaces a wall of per-method ``== "literal"`` value-pins that merely
    re-stated ``rpc/types.py`` (zero behavioral value, a mechanical re-edit on
    every ID rotation). This is strictly stronger: it holds for ALL members and
    catches empties / malformed tokens / cross-enum duplicate IDs that the
    individual pins never checked. ``rpc/types.py`` remains the single source of
    truth for the literal values.
    """

    def test_every_value_matches_the_obfuscated_id_shape(self):
        """Every member's value is a non-empty, well-formed obfuscated ID."""
        offenders = {
            member.name: member.value
            for member in RPCMethod
            if not _RPC_ID_SHAPE.fullmatch(member.value)
        }
        assert offenders == {}, (
            f"RPCMethod value(s) do not match the obfuscated-ID shape "
            f"{_RPC_ID_SHAPE.pattern}: {offenders}"
        )

    def test_values_are_unique_across_the_enum(self):
        """No two distinct method names may share an obfuscated ID (silent aliasing)."""
        # MUST iterate ``__members__.items()``, not ``RPCMethod`` directly:
        # ``Enum.__iter__`` yields only *canonical* members and silently hides
        # aliases (a second member declared with a duplicate value), so
        # ``for member in RPCMethod`` would never see the collision and this
        # test would pass with a duplicate present. ``__members__`` includes the
        # alias names, which is exactly what we want to catch.
        seen: dict[str, str] = {}
        collisions: dict[str, list[str]] = {}
        for name, member in RPCMethod.__members__.items():
            if member.value in seen:
                collisions.setdefault(member.value, [seen[member.value]]).append(name)
            else:
                seen[member.value] = name
        assert collisions == {}, f"Duplicate RPCMethod ID(s) alias multiple methods: {collisions}"

    def test_enum_is_non_empty(self):
        """A behavioral floor: the enum must actually define methods."""
        assert len(list(RPCMethod)) > 0

    def test_rpc_method_is_string(self):
        """Test RPCMethod values are strings (for JSON serialization)."""
        assert isinstance(RPCMethod.LIST_NOTEBOOKS.value, str)
        assert all(isinstance(member.value, str) for member in RPCMethod)


class TestArtifactTypeCode:
    def test_audio_type(self):
        """Test AUDIO content type code."""
        assert ArtifactTypeCode.AUDIO == 1

    def test_video_type(self):
        """Test VIDEO content type code."""
        assert ArtifactTypeCode.VIDEO == 3

    def test_slide_deck_type(self):
        """Test SLIDE_DECK content type code."""
        assert ArtifactTypeCode.SLIDE_DECK == 8

    def test_report_type(self):
        """Test REPORT content type code (includes Briefing Doc, Study Guide, etc.)."""
        assert ArtifactTypeCode.REPORT == 2

    def test_artifact_type_code_is_int(self):
        """Test ArtifactTypeCode values are integers."""
        assert isinstance(ArtifactTypeCode.AUDIO.value, int)


class TestArtifactStatusToStr:
    """Tests for artifact_status_to_str helper function."""

    def test_processing_status(self):
        """Test status code 1 (PROCESSING) returns 'in_progress'."""
        assert artifact_status_to_str(ArtifactStatus.PROCESSING) == "in_progress"
        assert artifact_status_to_str(1) == "in_progress"

    def test_pending_status(self):
        """Test status code 2 (PENDING) returns 'pending'."""
        assert artifact_status_to_str(ArtifactStatus.PENDING) == "pending"
        assert artifact_status_to_str(2) == "pending"

    def test_completed_status(self):
        """Test status code 3 (COMPLETED) returns 'completed'."""
        assert artifact_status_to_str(ArtifactStatus.COMPLETED) == "completed"
        assert artifact_status_to_str(3) == "completed"

    def test_failed_status(self):
        """Test status code 4 (FAILED) returns 'failed'."""
        assert artifact_status_to_str(ArtifactStatus.FAILED) == "failed"
        assert artifact_status_to_str(4) == "failed"

    def test_unknown_status_codes(self):
        """Test unknown status codes return 'unknown'."""
        assert artifact_status_to_str(0) == "unknown"
        assert artifact_status_to_str(5) == "unknown"
        assert artifact_status_to_str(99) == "unknown"
        assert artifact_status_to_str(-1) == "unknown"


class TestSourceStatusToStr:
    """Tests for source_status_to_str helper function."""

    def test_all_status_codes(self):
        """Test all SourceStatus enum values map correctly."""
        assert source_status_to_str(SourceStatus.PROCESSING) == "processing"
        assert source_status_to_str(1) == "processing"
        assert source_status_to_str(SourceStatus.READY) == "ready"
        assert source_status_to_str(2) == "ready"
        assert source_status_to_str(SourceStatus.ERROR) == "error"
        assert source_status_to_str(3) == "error"
        assert source_status_to_str(SourceStatus.PREPARING) == "preparing"
        assert source_status_to_str(5) == "preparing"

    def test_gap_status_code(self):
        """Test gap status code 4 returns 'unknown'."""
        assert source_status_to_str(4) == "unknown"

    def test_unknown_status_codes(self):
        """Test unknown status codes return 'unknown'."""
        assert source_status_to_str(0) == "unknown"
        assert source_status_to_str(6) == "unknown"
        assert source_status_to_str(99) == "unknown"
        assert source_status_to_str(-1) == "unknown"
