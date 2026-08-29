"""Unit tests for RPC types and constants."""

import ast
import re
import subprocess
import sys
from pathlib import Path

import pytest

from notebooklm.rpc.types import (
    BATCHEXECUTE_URL,
    QUERY_URL,
    ArtifactStatus,
    ArtifactTypeCode,
    DiscoveryMode,
    DriveSourceStatus,
    GrpcStatusCode,
    RPCMethod,
    SharePermission,
    SourceStatus,
    artifact_status_to_str,
    discovery_mode_to_str,
    drive_source_status_to_str,
    get_batchexecute_url,
    get_query_url,
    normalize_grpc_status,
    normalize_rpc_code,
    share_permission_to_str,
    source_status_to_str,
)


def test_rpc_types_does_not_own_runtime_override_policy() -> None:
    """Runtime override env parsing belongs in web wire, not rpc.types."""
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
                if (node.module, node.level) == ("_web.wire.overrides", 2):
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
        assert BATCHEXECUTE_URL == "https://notebook.google.com/_/LabsTailwindUi/data/batchexecute"

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


#: The backend ``ArtifactStatus`` enum, code by code, as recovered in
#: ``docs/android/enums.txt`` and corrected against live traces in #2127.
#: ``(wire code, member name, public status string)``.
_ARTIFACT_STATUS_TABLE = [
    (0, "UNKNOWN", "unknown"),
    (1, "PENDING", "pending"),
    (2, "PROCESSING", "in_progress"),
    (3, "COMPLETED", "completed"),
    (4, "FAILED", "failed"),
    (5, "SUGGESTED", "suggested"),
    (6, "PENDING_REVIEW", "pending_review"),
]


class TestArtifactStatusToStr:
    """Pin every backend ``ArtifactStatus`` code to its member and status string."""

    @pytest.mark.parametrize(
        ("code", "member_name", "expected"),
        _ARTIFACT_STATUS_TABLE,
        ids=[name for _code, name, _s in _ARTIFACT_STATUS_TABLE],
    )
    def test_code_maps_to_member_and_string(self, code, member_name, expected):
        member = ArtifactStatus(code)
        assert member.name == member_name
        assert artifact_status_to_str(code) == expected
        assert artifact_status_to_str(member) == expected

    def test_enum_covers_exactly_the_backend_codes(self):
        """No member is missing, and none was invented beyond the backend enum."""
        assert {member.value for member in ArtifactStatus} == {
            code for code, _name, _s in _ARTIFACT_STATUS_TABLE
        }

    def test_transitional_codes_are_not_transposed(self):
        """#2127 regression pin: 1 is queued, 2 is actively generating.

        Two independent live traces observed ``2 -> 3`` on a generating
        artifact, and every recorded CREATE_ARTIFACT row starts at 1.
        """
        assert ArtifactStatus.PENDING == 1
        assert ArtifactStatus.PROCESSING == 2

    def test_unrecognized_status_codes_degrade_to_unknown(self):
        """Codes outside the backend enum still fail closed rather than raise."""
        assert artifact_status_to_str(7) == "unknown"
        assert artifact_status_to_str(99) == "unknown"
        assert artifact_status_to_str(-1) == "unknown"


class TestSourceStatusToStr:
    """Tests for source_status_to_str helper function."""

    def test_all_status_codes(self):
        """Test all SourceStatus enum values map correctly."""
        assert source_status_to_str(SourceStatus.UNKNOWN) == "unknown"
        assert source_status_to_str(SourceStatus.PROCESSING) == "processing"
        assert source_status_to_str(1) == "processing"
        assert source_status_to_str(SourceStatus.READY) == "ready"
        assert source_status_to_str(2) == "ready"
        assert source_status_to_str(SourceStatus.ERROR) == "error"
        assert source_status_to_str(3) == "error"
        assert source_status_to_str(SourceStatus.PREPARING) == "preparing"
        assert source_status_to_str(5) == "preparing"

    def test_unknown_status_codes(self):
        """Test unknown status codes return 'unknown'."""
        assert source_status_to_str(0) == "unknown"
        assert source_status_to_str(4) == "unknown"
        assert source_status_to_str(6) == "unknown"
        assert source_status_to_str(99) == "unknown"
        assert source_status_to_str(-1) == "unknown"


class TestSharePermissionToStr:
    """Tests for the share_permission_to_str helper function."""

    def test_all_permission_codes(self):
        """Every displayable SharePermission member maps to its label."""
        assert share_permission_to_str(SharePermission.OWNER) == "owner"
        assert share_permission_to_str(1) == "owner"
        assert share_permission_to_str(SharePermission.EDITOR) == "editor"
        assert share_permission_to_str(2) == "editor"
        assert share_permission_to_str(SharePermission.VIEWER) == "viewer"
        assert share_permission_to_str(3) == "viewer"

    def test_remove_sentinel_is_not_a_label(self):
        """``_REMOVE`` is a write-only share-mutation sentinel, not a role.

        It must never surface as a displayable permission, so it degrades like
        any other unmapped code rather than leaking a private enum name.
        """
        assert share_permission_to_str(SharePermission._REMOVE) == "unknown"
        assert share_permission_to_str(4) == "unknown"

    def test_unknown_permission_codes(self):
        """Unrecognized codes return 'unknown' (future-proofing)."""
        assert share_permission_to_str(0) == "unknown"
        assert share_permission_to_str(5) == "unknown"
        assert share_permission_to_str(99) == "unknown"
        assert share_permission_to_str(-1) == "unknown"


class TestDriveSourceStatusToStr:
    """Tests for the drive_source_status_to_str helper function (#2111)."""

    def test_every_member_has_a_label(self):
        """No member falls through to the "unknown" default by accident."""
        assert {member: drive_source_status_to_str(member) for member in DriveSourceStatus} == {
            DriveSourceStatus.UNKNOWN: "unknown",
            DriveSourceStatus.INACCESSIBLE: "inaccessible",
            DriveSourceStatus.SYNCING: "syncing",
            DriveSourceStatus.ACTIVE: "active",
            DriveSourceStatus.DELETED: "deleted",
            DriveSourceStatus.GEN_AI_ACCESS_DENIED: "gen_ai_access_denied",
        }

    def test_accepts_raw_wire_codes(self):
        """The backend UserDriveSourceStatus integers map without an enum wrap."""
        assert drive_source_status_to_str(1) == "inaccessible"
        assert drive_source_status_to_str(2) == "syncing"
        assert drive_source_status_to_str(3) == "active"
        assert drive_source_status_to_str(4) == "deleted"
        assert drive_source_status_to_str(5) == "gen_ai_access_denied"

    def test_unknown_codes_degrade(self):
        """Unrecognized codes return 'unknown' (future-proofing)."""
        # 0 is the backend UNSPECIFIED, deliberately unmodelled: the decoder
        # normalizes it to None before a label is ever asked for.
        assert drive_source_status_to_str(0) == "unknown"
        assert drive_source_status_to_str(6) == "unknown"
        assert drive_source_status_to_str(99) == "unknown"
        assert drive_source_status_to_str(-2) == "unknown"


class TestDiscoveryModeToStr:
    """Tests for the discovery_mode_to_str helper function (#2122)."""

    def test_every_member_has_a_label(self):
        """No member falls through to the "unknown" default by accident."""
        assert {member: discovery_mode_to_str(member) for member in DiscoveryMode} == {
            DiscoveryMode.UNKNOWN: "unknown",
            DiscoveryMode.DEFAULT_LLM_SEARCH: "default_llm_search",
            DiscoveryMode.RAW_SEARCH: "raw_search",
            DiscoveryMode.CURIOUS_SEARCH: "curious_search",
            DiscoveryMode.CURIOUS_RAW_SEARCH: "curious_raw_search",
            DiscoveryMode.DEEP_RESEARCH: "deep_research",
            DiscoveryMode.LITE_LLM_SEARCH: "lite_llm_search",
        }

    def test_accepts_raw_wire_codes(self):
        """The backend DiscoveryMode integers map without an enum wrap."""
        assert discovery_mode_to_str(1) == "default_llm_search"
        assert discovery_mode_to_str(5) == "deep_research"

    def test_unknown_codes_degrade(self):
        """Unrecognized codes return 'unknown' (future-proofing)."""
        # 0 is the backend UNSPECIFIED, deliberately unmodelled: the decoder
        # normalizes it to None before a label is ever asked for.
        assert discovery_mode_to_str(0) == "unknown"
        assert discovery_mode_to_str(7) == "unknown"
        assert discovery_mode_to_str(-2) == "unknown"


class TestGrpcStatusCode:
    """The canonical gRPC status table and its two coercion helpers."""

    def test_values_match_the_google_rpc_code_table(self):
        # Wire contract: these numbers come from google.rpc.Code and are what
        # the backend embeds at index 5 of a wrb.fr entry.
        assert GrpcStatusCode.NOT_FOUND == 5
        assert GrpcStatusCode.PERMISSION_DENIED == 7
        assert GrpcStatusCode.OK == 0
        assert GrpcStatusCode.UNAUTHENTICATED == 16

    def test_is_a_separate_namespace_from_rpc_error_code(self):
        """``NOT_FOUND`` means 5 here and 404 in the HTTP-style enum.

        The two enums share member names, so anything comparing a wire status
        has to say which namespace it means. Pins that they did not get merged.
        """
        from notebooklm._web.wire.decoder import RPCErrorCode

        assert RPCErrorCode.NOT_FOUND == 404
        assert GrpcStatusCode.NOT_FOUND == 5

    def test_decoder_status_labels_cover_every_member(self):
        """Every status has wording; a new member cannot go unlabelled."""
        from notebooklm._web.wire.decoder import _GRPC_STATUS_MESSAGES

        assert set(_GRPC_STATUS_MESSAGES) == {int(code) for code in GrpcStatusCode}

    def test_normalize_grpc_status_accepts_both_wire_forms(self):
        assert normalize_grpc_status(5) is GrpcStatusCode.NOT_FOUND
        assert normalize_grpc_status("5") is GrpcStatusCode.NOT_FOUND
        assert normalize_grpc_status(7) is GrpcStatusCode.PERMISSION_DENIED

    def test_normalize_grpc_status_rejects_non_statuses(self):
        # rpc_code also carries non-numeric labels and may be absent entirely;
        # neither may raise, and neither is a status.
        assert normalize_grpc_status(None) is None
        assert normalize_grpc_status("USER_DISPLAYABLE_ERROR") is None
        assert normalize_grpc_status(999) is None
        # An HTTP status is numeric but is not a gRPC code.
        assert normalize_grpc_status(500) is None

    def test_bool_is_not_a_status(self):
        """``True`` must not normalize to CANCELLED (1) via the int subclass."""
        assert normalize_grpc_status(True) is None
        assert normalize_rpc_code(True) is None

    def test_normalize_rpc_code_keeps_http_statuses(self):
        """The wider helper passes 5xx through — the transient check needs it.

        Narrowing this one to the gRPC table would silently drop every HTTP
        status to ``None`` and disable the ``500 <= code < 600`` branch in the
        neutral error classifier.
        """
        assert normalize_rpc_code(500) == 500
        assert normalize_rpc_code("503") == 503
        assert normalize_rpc_code(5) == 5
        assert normalize_rpc_code(None) is None
        assert normalize_rpc_code("USER_DISPLAYABLE_ERROR") is None
