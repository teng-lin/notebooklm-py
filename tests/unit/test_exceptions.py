"""Test exception hierarchy and attributes."""

import pytest

import notebooklm
from notebooklm._env import DEFAULT_BASE_URL
from notebooklm.exceptions import (
    ArtifactDownloadError,
    ArtifactError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    AuthError,
    ChatError,
    ClientError,
    ConfigurationError,
    DecodingError,
    NetworkError,
    NotebookError,
    NotebookLimitError,
    NotebookLMError,
    NotebookNotFoundError,
    RateLimitError,
    RPCError,
    RPCTimeoutError,
    ServerError,
    SourceAddError,
    SourceError,
    SourceNotFoundError,
    SourceProcessingError,
    SourceTimeoutError,
    UnknownRPCMethodError,
    ValidationError,
)
from notebooklm.types import AccountLimits, AccountTier


class TestExceptionHierarchy:
    """Test that all exceptions inherit from NotebookLMError."""

    def test_all_exceptions_inherit_from_base(self):
        """All library exceptions inherit from NotebookLMError."""
        exceptions = [
            ValidationError,
            ConfigurationError,
            NetworkError,
            RPCError,
            DecodingError,
            UnknownRPCMethodError,
            AuthError,
            RateLimitError,
            ServerError,
            ClientError,
            RPCTimeoutError,
            NotebookError,
            NotebookNotFoundError,
            NotebookLimitError,
            ChatError,
            SourceError,
            SourceAddError,
            SourceNotFoundError,
            SourceProcessingError,
            SourceTimeoutError,
            ArtifactError,
            ArtifactNotFoundError,
            ArtifactNotReadyError,
            ArtifactParseError,
            ArtifactDownloadError,
        ]
        for exc_class in exceptions:
            assert issubclass(exc_class, NotebookLMError), (
                f"{exc_class.__name__} should inherit from NotebookLMError"
            )

    def test_network_error_not_under_rpc(self):
        """NetworkError is NOT under RPCError (by design)."""
        assert not issubclass(NetworkError, RPCError)
        assert issubclass(NetworkError, NotebookLMError)

    def test_rpc_timeout_inherits_from_network_error(self):
        """RPCTimeoutError inherits from NetworkError (transport-level issue)."""
        assert issubclass(RPCTimeoutError, NetworkError)
        assert issubclass(RPCTimeoutError, NotebookLMError)

    def test_decoding_errors_inherit_from_rpc_error(self):
        """DecodingError and UnknownRPCMethodError inherit from RPCError."""
        assert issubclass(DecodingError, RPCError)
        assert issubclass(UnknownRPCMethodError, DecodingError)
        assert issubclass(UnknownRPCMethodError, RPCError)

    def test_domain_exceptions_have_correct_base(self):
        """Domain exceptions inherit from their domain base."""
        assert issubclass(NotebookNotFoundError, NotebookError)
        assert issubclass(SourceAddError, SourceError)
        assert issubclass(SourceNotFoundError, SourceError)
        assert issubclass(SourceProcessingError, SourceError)
        assert issubclass(SourceTimeoutError, SourceError)
        assert issubclass(ArtifactNotFoundError, ArtifactError)
        assert issubclass(ArtifactNotReadyError, ArtifactError)
        assert issubclass(ArtifactParseError, ArtifactError)
        assert issubclass(ArtifactDownloadError, ArtifactError)

    def test_notebook_limit_error_is_exported_from_package(self):
        """NotebookLimitError is available from the public package namespace."""
        assert notebooklm.NotebookLimitError is NotebookLimitError
        assert "NotebookLimitError" in notebooklm.__all__

    def test_account_types_are_exported_from_package(self):
        """Account limit and tier types are available from the public package namespace."""
        assert notebooklm.AccountLimits is AccountLimits
        assert notebooklm.AccountTier is AccountTier
        assert "AccountLimits" in notebooklm.__all__
        assert "AccountTier" in notebooklm.__all__


class TestRPCErrorAttributes:
    """Test RPCError attribute handling."""

    def test_rpc_error_stores_method_id(self):
        """RPCError stores method_id attribute."""
        e = RPCError("Failed", method_id="abc123")
        assert e.method_id == "abc123"

    def test_rpc_error_backward_compat_rpc_id(self):
        """RPCError supports backward-compatible rpc_id alias with deprecation warning."""
        import warnings

        e = RPCError("Failed", method_id="abc123")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert e.rpc_id == "abc123"  # Alias
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "rpc_id" in str(w[0].message)

    def test_rpc_error_stores_rpc_code(self):
        """RPCError stores rpc_code attribute."""
        e = RPCError("Failed", rpc_code=404)
        assert e.rpc_code == 404

    def test_rpc_error_backward_compat_code(self):
        """RPCError supports backward-compatible code alias with deprecation warning."""
        import warnings

        e = RPCError("Failed", rpc_code="NOT_FOUND")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert e.code == "NOT_FOUND"  # Alias
            assert len(w) == 1
            assert issubclass(w[0].category, DeprecationWarning)
            assert "code" in str(w[0].message)

    def test_rpc_error_truncates_raw_response(self, monkeypatch):
        """RPCError truncates raw_response to 80 chars + '...' by default."""
        monkeypatch.delenv("NOTEBOOKLM_DEBUG", raising=False)
        long_response = "x" * 1000
        e = RPCError("Failed", raw_response=long_response)
        assert e.raw_response is not None
        assert len(e.raw_response) == 83
        assert e.raw_response.endswith("...")
        assert e.raw_response[:-3] == "x" * 80

    def test_rpc_error_stores_found_ids(self):
        """RPCError stores found_ids list."""
        e = RPCError("Failed", found_ids=["id1", "id2"])
        assert e.found_ids == ["id1", "id2"]

    def test_rpc_error_found_ids_defaults_to_empty(self):
        """RPCError found_ids defaults to empty list."""
        e = RPCError("Failed")
        assert e.found_ids == []


class TestRateLimitError:
    """Test RateLimitError-specific attributes."""

    def test_rate_limit_error_has_retry_after(self):
        """RateLimitError stores retry_after attribute."""
        e = RateLimitError("Too fast", retry_after=30)
        assert e.retry_after == 30
        assert "Too fast" in str(e)

    def test_rate_limit_error_retry_after_optional(self):
        """RateLimitError retry_after is optional."""
        e = RateLimitError("Too fast")
        assert e.retry_after is None


class TestServerError:
    """Test ServerError-specific attributes."""

    def test_server_error_has_status_code(self):
        """ServerError stores status_code attribute."""
        e = ServerError("Internal error", status_code=500)
        assert e.status_code == 500


class TestClientError:
    """Test ClientError-specific attributes."""

    def test_client_error_has_status_code(self):
        """ClientError stores status_code attribute."""
        e = ClientError("Bad request", status_code=400)
        assert e.status_code == 400


class TestNetworkError:
    """Test NetworkError-specific attributes."""

    def test_network_error_stores_original_error(self):
        """NetworkError stores original_error attribute."""
        original = ConnectionError("Connection refused")
        e = NetworkError("Failed to connect", original_error=original)
        assert e.original_error is original

    def test_network_error_stores_method_id(self):
        """NetworkError stores method_id attribute."""
        e = NetworkError("Failed", method_id="abc123")
        assert e.method_id == "abc123"


class TestRPCTimeoutError:
    """Test RPCTimeoutError-specific attributes."""

    def test_timeout_error_has_timeout_seconds(self):
        """RPCTimeoutError stores timeout_seconds attribute."""
        e = RPCTimeoutError("Timed out", timeout_seconds=30.0)
        assert e.timeout_seconds == 30.0


class TestDomainExceptions:
    """Test domain-specific exception attributes."""

    def test_notebook_not_found_has_notebook_id(self):
        """NotebookNotFoundError stores notebook_id."""
        e = NotebookNotFoundError("nb_123")
        assert e.notebook_id == "nb_123"
        assert "nb_123" in str(e)

    def test_notebook_limit_error_has_count_and_limit(self):
        """NotebookLimitError stores quota context."""
        original = RPCError("create failed", method_id="CCqFvf", rpc_code=3)
        e = NotebookLimitError(499, limit=500, original_error=original)

        assert e.current_count == 499
        assert e.limit == 500
        assert e.known_limits == ()
        assert e.original_error is original
        assert "499/500" in str(e)
        assert "notebook limit" in str(e).lower()

    def test_notebook_limit_error_json_extra_includes_original_rpc_context(self):
        """NotebookLimitError exposes structured JSON metadata."""
        original = RPCError("create failed", method_id="CCqFvf", rpc_code=3)
        e = NotebookLimitError(499, limit=500, original_error=original)

        assert e.to_error_response_extra() == {
            "current_count": 499,
            "limit": 500,
            "method_id": "CCqFvf",
            "rpc_code": 3,
        }

    def test_notebook_limit_error_handles_empty_known_limits(self):
        """NotebookLimitError omits known-limit sentence when none are provided."""
        e = NotebookLimitError(499, limit=500, known_limits=())

        assert e.known_limits == ()
        assert "Known NotebookLM limits include" not in str(e)

    def test_notebook_limit_error_preserves_explicit_known_limits(self):
        """NotebookLimitError keeps explicit known limits for compatibility."""
        e = NotebookLimitError(499, limit=500, known_limits=(100, 500))

        assert e.known_limits == (100, 500)
        assert "Known NotebookLM limits include: 100, 500" in str(e)
        assert e.to_error_response_extra()["known_limits"] == [100, 500]

    def test_notebook_limit_error_tolerates_invalid_base_url_env(self, monkeypatch):
        """NotebookLimitError should preserve quota context even if env config is invalid."""
        monkeypatch.setenv("NOTEBOOKLM_BASE_URL", "https://evil.example.com")

        e = NotebookLimitError(499, limit=500)

        assert "499/500" in str(e)
        base_url = (
            str(e)
            .split("Delete old notebooks at ", 1)[1]
            .split(
                " and try again.",
                1,
            )[0]
        )
        assert base_url == DEFAULT_BASE_URL

    def test_source_not_found_has_source_id(self):
        """SourceNotFoundError stores source_id."""
        e = SourceNotFoundError("src_456")
        assert e.source_id == "src_456"
        assert "src_456" in str(e)

    def test_source_processing_error_has_status(self):
        """SourceProcessingError stores source_id and status."""
        e = SourceProcessingError("src_789", status=3)
        assert e.source_id == "src_789"
        assert e.status == 3

    def test_source_timeout_error_has_timeout(self):
        """SourceTimeoutError stores source_id, timeout, and last_status."""
        e = SourceTimeoutError("src_abc", timeout=60.0, last_status=1)
        assert e.source_id == "src_abc"
        assert e.timeout == 60.0
        assert e.last_status == 1

    def test_source_add_error_has_url(self):
        """SourceAddError stores url and cause."""
        cause = ConnectionError("Failed")
        e = SourceAddError("https://example.com", cause=cause)
        assert e.url == "https://example.com"
        assert e.cause is cause

    def test_artifact_not_found_has_artifact_id(self):
        """ArtifactNotFoundError stores artifact_id and artifact_type."""
        e = ArtifactNotFoundError("art_123", artifact_type="audio")
        assert e.artifact_id == "art_123"
        assert e.artifact_type == "audio"

    def test_artifact_not_ready_has_status(self):
        """ArtifactNotReadyError stores artifact_type, artifact_id, status."""
        e = ArtifactNotReadyError("video", artifact_id="art_456", status="processing")
        assert e.artifact_type == "video"
        assert e.artifact_id == "art_456"
        assert e.status == "processing"

    def test_artifact_parse_error_has_details(self):
        """ArtifactParseError stores details and cause."""
        cause = ValueError("Invalid JSON")
        e = ArtifactParseError("quiz", details="Malformed response", cause=cause)
        assert e.artifact_type == "quiz"
        assert e.details == "Malformed response"
        assert e.cause is cause

    def test_artifact_download_error_has_details(self):
        """ArtifactDownloadError stores details and cause."""
        e = ArtifactDownloadError("audio", details="404 Not Found", artifact_id="art_789")
        assert e.artifact_type == "audio"
        assert e.details == "404 Not Found"
        assert e.artifact_id == "art_789"


class TestCatchAllPattern:
    """Test that catching NotebookLMError catches all library exceptions."""

    def test_catch_all_rpc_errors(self):
        """Catching NotebookLMError catches all RPC exceptions."""
        for exc_class in [RPCError, AuthError, RateLimitError, ServerError, ClientError]:
            with pytest.raises(NotebookLMError):
                raise exc_class("test")

    def test_catch_all_network_errors(self):
        """Catching NotebookLMError catches all network exceptions."""
        for exc_class in [NetworkError, RPCTimeoutError]:
            with pytest.raises(NotebookLMError):
                raise exc_class("test")

    def test_catch_all_domain_errors(self):
        """Catching NotebookLMError catches all domain exceptions."""
        with pytest.raises(NotebookLMError):
            raise NotebookNotFoundError("nb_123")
        with pytest.raises(NotebookLMError):
            raise SourceNotFoundError("src_456")
        with pytest.raises(NotebookLMError):
            raise ArtifactNotReadyError("audio")
