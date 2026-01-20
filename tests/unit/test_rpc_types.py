"""Unit tests for RPC types and constants."""

from notebooklm.rpc.types import (
    BATCHEXECUTE_URL,
    QUERY_URL,
    ArtifactStatus,
    RPCMethod,
    SourceStatus,
    StudioContentType,
    artifact_status_to_str,
    source_status_to_str,
)


class TestRPCConstants:
    def test_batchexecute_url(self):
        """Test batchexecute URL is correct."""
        assert (
            BATCHEXECUTE_URL == "https://notebooklm.google.com/_/LabsTailwindUi/data/batchexecute"
        )

    def test_query_url(self):
        """Test query URL for streaming chat."""
        assert "GenerateFreeFormStreamed" in QUERY_URL


class TestRPCMethod:
    def test_list_notebooks(self):
        """Test LIST_NOTEBOOKS RPC ID."""
        assert RPCMethod.LIST_NOTEBOOKS == "wXbhsf"

    def test_create_notebook(self):
        """Test CREATE_NOTEBOOK RPC ID."""
        assert RPCMethod.CREATE_NOTEBOOK == "CCqFvf"

    def test_get_notebook(self):
        """Test GET_NOTEBOOK RPC ID."""
        assert RPCMethod.GET_NOTEBOOK == "rLM1Ne"

    def test_delete_notebook(self):
        """Test DELETE_NOTEBOOK RPC ID."""
        assert RPCMethod.DELETE_NOTEBOOK == "WWINqb"

    def test_add_source(self):
        """Test ADD_SOURCE RPC ID."""
        assert RPCMethod.ADD_SOURCE == "izAoDd"

    def test_summarize(self):
        """Test SUMMARIZE RPC ID."""
        assert RPCMethod.SUMMARIZE == "VfAZjd"

    def test_create_audio(self):
        """Test CREATE_AUDIO RPC ID."""
        assert RPCMethod.CREATE_AUDIO == "AHyHrd"

    def test_create_video(self):
        """Test CREATE_VIDEO RPC ID."""
        assert RPCMethod.CREATE_VIDEO == "R7cb6c"

    def test_poll_studio(self):
        """Test POLL_STUDIO RPC ID."""
        assert RPCMethod.POLL_STUDIO == "gArtLc"

    def test_create_artifact(self):
        """Test CREATE_ARTIFACT RPC ID."""
        assert RPCMethod.CREATE_ARTIFACT == "xpWGLf"

    def test_rpc_method_is_string(self):
        """Test RPCMethod values are strings (for JSON serialization)."""
        assert isinstance(RPCMethod.LIST_NOTEBOOKS.value, str)


class TestStudioContentType:
    def test_audio_type(self):
        """Test AUDIO content type code."""
        assert StudioContentType.AUDIO == 1

    def test_video_type(self):
        """Test VIDEO content type code."""
        assert StudioContentType.VIDEO == 3

    def test_slide_deck_type(self):
        """Test SLIDE_DECK content type code."""
        assert StudioContentType.SLIDE_DECK == 8

    def test_report_type(self):
        """Test REPORT content type code (includes Briefing Doc, Study Guide, etc.)."""
        assert StudioContentType.REPORT == 2

    def test_studio_type_is_int(self):
        """Test StudioContentType values are integers."""
        assert isinstance(StudioContentType.AUDIO.value, int)


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

    def test_unknown_status_codes(self):
        """Test unknown status codes return 'unknown'."""
        assert artifact_status_to_str(0) == "unknown"
        assert artifact_status_to_str(4) == "unknown"
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
