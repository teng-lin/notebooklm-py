"""Test exception hierarchy and attributes."""

import pytest

import notebooklm
from notebooklm._env import DEFAULT_BASE_URL
from notebooklm.exceptions import (
    ArtifactDownloadError,
    ArtifactError,
    ArtifactFeatureUnavailableError,
    ArtifactInProgressTimeoutError,
    ArtifactNotFoundError,
    ArtifactNotReadyError,
    ArtifactParseError,
    ArtifactPendingTimeoutError,
    ArtifactTimeoutError,
    AuthError,
    AuthExtractionError,
    ChatError,
    ClientError,
    ConfigurationError,
    DecodingError,
    NetworkError,
    NotebookError,
    NotebookLimitError,
    NotebookLMError,
    NotebookNotFoundError,
    NotFoundError,
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
from notebooklm.types import AccountLimits, AccountTier, GenerationStatus


class TestExceptionHierarchy:
    """Test that all exceptions inherit from NotebookLMError."""

    def test_all_exceptions_inherit_from_base(self):
        """All library exceptions inherit from NotebookLMError."""
        exceptions = [
            ValidationError,
            ConfigurationError,
            NetworkError,
            NotFoundError,
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
            ArtifactFeatureUnavailableError,
            ArtifactTimeoutError,
            ArtifactPendingTimeoutError,
            ArtifactInProgressTimeoutError,
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
        assert issubclass(ArtifactFeatureUnavailableError, ArtifactError)
        assert issubclass(ArtifactTimeoutError, ArtifactError)
        assert issubclass(ArtifactTimeoutError, TimeoutError)
        assert issubclass(ArtifactPendingTimeoutError, ArtifactTimeoutError)
        assert issubclass(ArtifactInProgressTimeoutError, ArtifactTimeoutError)

    def test_not_found_errors_are_rpc_errors(self):
        """All ``*NotFoundError`` classes mix in :class:`RPCError`.

        v0.6.0 restored symmetry across the three "not found" error types so
        ``except RPCError`` catches all of them at transport-level call sites.
        Before v0.6.0, only :class:`NotebookNotFoundError` mixed in
        :class:`RPCError`; :class:`SourceNotFoundError` and
        :class:`ArtifactNotFoundError` did not. This test pins the symmetry so
        a regression cannot silently re-introduce the asymmetry.
        """
        assert issubclass(NotebookNotFoundError, RPCError)
        assert issubclass(SourceNotFoundError, RPCError)
        assert issubclass(ArtifactNotFoundError, RPCError)

    def test_not_found_errors_have_canonical_mro(self):
        """The MRO ordering ``(<self>, NotFoundError, RPCError, <domain-error>,
        ...)`` is load-bearing — it ensures the cross-domain umbrella matches
        first, the RPCError-keyed catches see the ``*NotFoundError`` types
        before the domain-error base does, and ``isinstance(e, RPCError)``
        short-circuits via the RPC parent chain. Pinning the order guards
        against accidentally swapping bases in a future refactor.
        """
        assert NotebookNotFoundError.__mro__[1:4] == (NotFoundError, RPCError, NotebookError)
        assert SourceNotFoundError.__mro__[1:4] == (NotFoundError, RPCError, SourceError)
        assert ArtifactNotFoundError.__mro__[1:4] == (NotFoundError, RPCError, ArtifactError)

    def test_not_found_errors_caught_by_except_rpc_error(self):
        """End-to-end ``try/except RPCError`` exercise — a direct regression
        pin for the stated v0.6.0 contract that ``except RPCError`` catches
        each of the three "not found" types. ``issubclass`` is sufficient for
        MRO inspection, but a real ``raise``/``except`` round-trip exercises
        the runtime catch path and is what the BREAKING CHANGE migration
        guidance actually promises users."""
        with pytest.raises(RPCError):
            raise NotebookNotFoundError("nb_x")
        with pytest.raises(RPCError):
            raise SourceNotFoundError("src_x")
        with pytest.raises(RPCError):
            raise ArtifactNotFoundError("art_x")

    def test_source_not_found_is_rpc_error(self):
        """``SourceNotFoundError`` is catchable as ``RPCError`` (v0.6.0).

        Restores symmetry with :class:`NotebookNotFoundError` (which has
        inherited from :class:`RPCError` since the 0.5.x series). This is a
        v0.6.0 BREAKING CHANGE for code that catches ``RPCError`` before
        ``SourceNotFoundError``.
        """
        assert issubclass(SourceNotFoundError, RPCError)
        err = SourceNotFoundError("src_x", method_id="rwIQyf")
        assert err.source_id == "src_x"
        assert err.method_id == "rwIQyf"
        # Catchable as both RPCError and SourceError.
        assert isinstance(err, RPCError)
        assert isinstance(err, SourceError)

    def test_artifact_not_found_is_rpc_error(self):
        """``ArtifactNotFoundError`` is catchable as ``RPCError`` (v0.6.0).

        Restores symmetry with :class:`NotebookNotFoundError`. This is a
        v0.6.0 BREAKING CHANGE for code that catches ``RPCError`` before
        ``ArtifactNotFoundError``.
        """
        assert issubclass(ArtifactNotFoundError, RPCError)
        err = ArtifactNotFoundError("art_x", artifact_type="audio", method_id="abc")
        assert err.artifact_id == "art_x"
        assert err.artifact_type == "audio"
        assert err.method_id == "abc"
        # Catchable as both RPCError and ArtifactError.
        assert isinstance(err, RPCError)
        assert isinstance(err, ArtifactError)

    def test_notebook_limit_error_is_exported_from_package(self):
        """NotebookLimitError is available from the public package namespace."""
        assert notebooklm.NotebookLimitError is NotebookLimitError
        assert "NotebookLimitError" in notebooklm.__all__

    def test_artifact_timeout_errors_are_exported_from_package(self):
        """Structured artifact timeout errors are public API exceptions."""
        assert notebooklm.ArtifactTimeoutError is ArtifactTimeoutError
        assert notebooklm.ArtifactPendingTimeoutError is ArtifactPendingTimeoutError
        assert notebooklm.ArtifactInProgressTimeoutError is ArtifactInProgressTimeoutError
        assert "ArtifactTimeoutError" in notebooklm.__all__
        assert "ArtifactPendingTimeoutError" in notebooklm.__all__
        assert "ArtifactInProgressTimeoutError" in notebooklm.__all__

    def test_artifact_timeout_accepts_sequence_history(self):
        """Manual exception construction normalizes status history to a tuple."""
        err = ArtifactTimeoutError(
            "nb_123",
            "task_123",
            30.0,
            last_status="in_progress",
            status_history=["pending", "in_progress"],
        )

        assert err.status_history == ("pending", "in_progress")
        assert "notebook nb_123" in str(err)
        assert "pending -> in_progress" in str(err)

    def test_artifact_timeout_accepts_sequence_transitions(self):
        """Manual exception construction normalizes status snapshots to a tuple."""
        transitions = [
            GenerationStatus(task_id="task_123", status="pending"),
            GenerationStatus(task_id="task_123", status="in_progress"),
        ]
        err = ArtifactTimeoutError("nb_123", "task_123", 30.0, status_transitions=transitions)

        assert err.status_transitions == tuple(transitions)
        assert err.status_history == ("pending", "in_progress")
        assert "pending -> in_progress" in str(err)

    def test_artifact_pending_timeout_without_history_reports_no_status(self):
        """The defensive no-history message branch is part of the public repr."""
        err = ArtifactPendingTimeoutError("nb_123", "task_123", 30.0)

        assert err.status_history == ()
        assert err.status_transitions == ()
        assert err.stalled_phase == "pending"
        assert "no status" in str(err)

    def test_account_types_are_exported_from_package(self):
        """Account limit and tier types are available from the public package namespace."""
        assert notebooklm.AccountLimits is AccountLimits
        assert notebooklm.AccountTier is AccountTier
        assert "AccountLimits" in notebooklm.__all__
        assert "AccountTier" in notebooklm.__all__


class TestNotFoundErrorUmbrella:
    """The NotFoundError umbrella catches every *NotFoundError across domains.

    Catch semantics for the existing per-type bases (NotebookError /
    SourceError / ArtifactError) MUST remain unchanged — adding the umbrella
    itself was purely additive in the original PR #1035. v0.6.0 then
    restored RPCError symmetry across all three concrete subclasses (see
    ``TestExceptionHierarchy.test_not_found_errors_are_rpc_errors`` and
    ``TestDomainExceptions.test_source_not_found_is_rpc_error`` /
    ``test_artifact_not_found_is_rpc_error``); the umbrella class itself
    deliberately stays OUT of the RPCError subtree (see
    ``test_not_found_error_itself_is_not_an_rpc_error``).
    """

    def test_not_found_error_is_subclass_of_notebooklm_error(self):
        """NotFoundError lives under the top-level NotebookLMError umbrella."""
        assert issubclass(NotFoundError, NotebookLMError)

    def test_not_found_error_itself_is_not_an_rpc_error(self):
        """The umbrella class itself must NOT inherit from RPCError.

        The umbrella sits next to (not under) :class:`RPCError` in the
        hierarchy — it catches "missing resource" regardless of whether the
        underlying signal was an RPC degenerate-payload (the three concrete
        subclasses also mix in :class:`RPCError` as of v0.6.0) or a future
        non-RPC source of missing-resource signals. Pinning ``RPCError not
        in NotFoundError.__mro__`` guards against accidentally collapsing
        the umbrella into the RPC subtree.
        """
        assert not issubclass(NotFoundError, RPCError)
        assert RPCError not in NotFoundError.__mro__

    def test_not_found_error_catches_notebook_not_found(self):
        """`except NotFoundError` catches NotebookNotFoundError."""
        assert issubclass(NotebookNotFoundError, NotFoundError)
        with pytest.raises(NotFoundError):
            raise NotebookNotFoundError("nb-123")

    def test_not_found_error_catches_source_not_found(self):
        """`except NotFoundError` catches SourceNotFoundError."""
        assert issubclass(SourceNotFoundError, NotFoundError)
        with pytest.raises(NotFoundError):
            raise SourceNotFoundError("src-123")

    def test_not_found_error_catches_artifact_not_found(self):
        """`except NotFoundError` catches ArtifactNotFoundError."""
        assert issubclass(ArtifactNotFoundError, NotFoundError)
        with pytest.raises(NotFoundError):
            raise ArtifactNotFoundError("art-123", "audio")

    def test_existing_catches_still_work(self):
        """Adding NotFoundError must not break existing domain catches.

        Regression guard: each *NotFoundError must still be caught by its
        legacy domain base(s).
        """
        # Notebook side: still RPCError + NotebookError.
        with pytest.raises(NotebookError):
            raise NotebookNotFoundError("nb-1")
        with pytest.raises(RPCError):
            raise NotebookNotFoundError("nb-2")

        # Source side: still SourceError.
        with pytest.raises(SourceError):
            raise SourceNotFoundError("src-1")

        # Artifact side: still ArtifactError.
        with pytest.raises(ArtifactError):
            raise ArtifactNotFoundError("art-1", "audio")

    # Note: prior tests `test_source_not_found_does_not_gain_rpc_error` and
    # `test_artifact_not_found_does_not_gain_rpc_error` (added with the
    # NotFoundError umbrella) explicitly pinned the asymmetry where only
    # NotebookNotFoundError inherits from RPCError. v0.6.0 deliberately
    # widened the symmetry — see ``TestExceptionHierarchy.
    # test_not_found_errors_are_rpc_errors`` and
    # ``test_not_found_errors_have_canonical_mro`` (positive-assertion
    # replacements) plus the ``TestDomainExceptions.
    # test_source_not_found_is_rpc_error`` /
    # ``test_artifact_not_found_is_rpc_error`` instance-level proofs.

    def test_not_found_error_is_exported_from_package(self):
        """NotFoundError is reachable via ``from notebooklm import NotFoundError``."""
        assert notebooklm.NotFoundError is NotFoundError
        assert "NotFoundError" in notebooklm.__all__

    def test_not_found_error_catches_all_three_in_one_clause(self):
        """The motivating use case: one `except NotFoundError` clause
        replaces a 3-tuple ``except (NotebookNotFoundError, SourceNotFoundError,
        ArtifactNotFoundError):``."""
        caught: list[type] = []
        for exc in (
            NotebookNotFoundError("nb"),
            SourceNotFoundError("src"),
            ArtifactNotFoundError("art", "audio"),
        ):
            try:
                raise exc
            except NotFoundError as e:
                caught.append(type(e))
        assert caught == [
            NotebookNotFoundError,
            SourceNotFoundError,
            ArtifactNotFoundError,
        ]


class TestRPCErrorAttributes:
    """Test RPCError attribute handling."""

    def test_rpc_error_stores_method_id(self):
        """RPCError stores method_id attribute."""
        e = RPCError("Failed", method_id="abc123")
        assert e.method_id == "abc123"

    def test_rpc_error_backward_compat_rpc_id(self):
        """RPCError supports permanent backward-compatible rpc_id alias without warning."""
        import warnings

        e = RPCError("Failed", method_id="abc123")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert e.rpc_id == "abc123"  # Alias
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert deprecation_warnings == []

    def test_rpc_error_stores_rpc_code(self):
        """RPCError stores rpc_code attribute."""
        e = RPCError("Failed", rpc_code=404)
        assert e.rpc_code == 404

    def test_rpc_error_backward_compat_code(self):
        """RPCError supports permanent backward-compatible code alias without warning."""
        import warnings

        e = RPCError("Failed", rpc_code="NOT_FOUND")
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            assert e.code == "NOT_FOUND"  # Alias
        deprecation_warnings = [x for x in w if issubclass(x.category, DeprecationWarning)]
        assert deprecation_warnings == []

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

    def test_source_not_found_accepts_rpc_metadata(self):
        """SourceNotFoundError can carry ``method_id`` / ``raw_response`` for
        callsites that wrap a degenerate RPC payload (v0.6.0)."""
        e = SourceNotFoundError("src_xyz", method_id="getSourceXYZ", raw_response="[]")
        assert e.source_id == "src_xyz"
        assert e.method_id == "getSourceXYZ"
        assert e.raw_response == "[]"
        # Default empty list from RPCError parent.
        assert e.found_ids == []

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
        """ArtifactNotFoundError stores artifact_id and artifact_type, and the
        message is well-formatted (no leading space; ``artifact_type``
        capitalized; ``artifact_id`` appears in the string).
        """
        e = ArtifactNotFoundError("art_123", artifact_type="audio")
        assert e.artifact_id == "art_123"
        assert e.artifact_type == "audio"
        # Format pin (regression-guard for the pre-existing leading-space /
        # capitalize-on-leading-space bug fixed in PR #1037):
        assert str(e) == "Audio artifact not found: art_123"
        # And specifically: ID must be in the string (RPCError has no
        # __str__ override, so the message text is the entire string repr).
        assert "art_123" in str(e)
        assert not str(e).startswith(" "), "no leading space in message"

    def test_artifact_not_found_without_type_has_clean_message(self):
        """When ``artifact_type`` is omitted, the message starts with
        ``Artifact`` (no leading space) and still includes the ID. Regression
        guard for the pre-existing message-formatting bug fixed in PR #1037.
        """
        e = ArtifactNotFoundError("art_xyz")
        assert e.artifact_id == "art_xyz"
        assert e.artifact_type is None
        assert str(e) == "Artifact not found: art_xyz"
        assert not str(e).startswith(" ")

    def test_artifact_not_found_accepts_rpc_metadata(self):
        """ArtifactNotFoundError can carry ``method_id`` / ``raw_response`` for
        callsites that wrap a degenerate RPC payload (v0.6.0)."""
        e = ArtifactNotFoundError(
            "art_xyz",
            artifact_type="video",
            method_id="listArtifacts",
            raw_response="[[]]",
        )
        assert e.artifact_id == "art_xyz"
        assert e.artifact_type == "video"
        assert e.method_id == "listArtifacts"
        assert e.raw_response == "[[]]"
        assert e.found_ids == []

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

    def test_artifact_feature_unavailable_error_has_rpc_metadata(self):
        """ArtifactFeatureUnavailableError stores artifact type and RPC metadata."""
        e = ArtifactFeatureUnavailableError("infographic", method_id="R7cb6c")
        assert e.artifact_type == "infographic"
        assert e.method_id == "R7cb6c"
        assert isinstance(e, ArtifactError)
        assert isinstance(e, RPCError)
        assert str(e) == "Infographic generation is unavailable"


class TestAuthExtractionErrorScrubbing:
    """AuthExtractionError must redact credential-shaped substrings in its preview."""

    def test_auth_extraction_error_scrubs_payload(self):
        """payload_preview must not leak ``f.sid=`` values from raw HTML.

        Drift previews can capture multi-KB HTML snippets that contain live
        session-id query params; ``scrub_secrets`` is applied during the
        slice + whitespace-collapse pipeline so the redaction cannot be
        defeated by a value that straddles the 5x preview boundary.
        """
        # Token value lives in the prefix that will survive truncation.
        payload = "<html><body>boot script f.sid=ABC123XYZ remaining markup</body></html>"
        exc = AuthExtractionError("SNlM0e", payload)

        assert "ABC123XYZ" not in exc.payload_preview
        assert "ABC123XYZ" not in str(exc)
        # Sanity: the redaction marker should be present so operators can see
        # WHY the value is missing.
        assert "f.sid=***" in exc.payload_preview

    def test_auth_extraction_error_scrubs_secret_near_5x_boundary(self):
        """Secret straddling the 5x boundary is still scrubbed via the 10x slice.

        The implementation pre-slices to 10x PREVIEW_LENGTH (2000 chars) before
        scrubbing — large enough that a secret near the 5x cutoff (~1000 chars)
        is fully contained in the pre-slice and gets redacted.
        """
        prefix = "A" * (AuthExtractionError.PREVIEW_LENGTH * 5 - 10)
        # Secret begins inside the 5x cut and continues past it — without the
        # 10x pre-slice we'd see the unredacted "f.sid=SECRET" prefix.
        payload = prefix + "f.sid=SECRET_NEAR_BOUNDARY_VALUE"
        exc = AuthExtractionError("SNlM0e", payload)

        assert "SECRET_NEAR_BOUNDARY" not in exc.payload_preview
        assert "SECRET_NEAR_BOUNDARY" not in str(exc)


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
