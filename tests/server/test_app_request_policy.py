"""Request-admission helpers in the REST server app.

These decide how much body the server will read and how a bootstrap failure is
projected to the client. They are pure functions in front of the middleware, so
the rejections are cheap to pin and expensive to get wrong: a mis-parsed
``Content-Length`` is an unbounded read, and a mis-projected startup error hides
"your auth is stale" behind a generic 500.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi", reason="requires the optional [server] extra")

from notebooklm.exceptions import AuthError, NotebookLMError, ValidationError  # noqa: E402
from notebooklm.server.app import (  # noqa: E402
    DEFAULT_JSON_BODY_BYTES,
    _is_json_content_type,
    _json_body_limit,
    _media_type,
    _normalize_client_startup_error,
    _parse_content_length,
)

# ---------------------------------------------------------------------------
# Content type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("application/json", "application/json", id="plain"),
        pytest.param("application/json; charset=utf-8", "application/json", id="with-parameters"),
        pytest.param("  APPLICATION/JSON  ", "application/json", id="normalised"),
        pytest.param("", "", id="empty"),
    ],
)
def test_the_media_type_drops_parameters_and_case(raw: str, expected: str) -> None:
    assert _media_type(raw) == expected


@pytest.mark.parametrize(
    ("raw", "is_json"),
    [
        pytest.param("application/json", True, id="canonical"),
        pytest.param("application/json; charset=utf-8", True, id="with-charset"),
        pytest.param("APPLICATION/JSON", True, id="uppercase"),
        pytest.param("application/merge-patch+json", True, id="structured-suffix"),
        pytest.param("application/vnd.api+json", True, id="vendor-suffix"),
        pytest.param("text/json", False, id="wrong-tree"),
        pytest.param("application/jsonish", False, id="prefix-only"),
        pytest.param("multipart/form-data", False, id="upload"),
        pytest.param("", False, id="absent"),
    ],
)
def test_json_content_types_are_recognised_by_suffix_not_substring(raw: str, is_json: bool) -> None:
    assert _is_json_content_type(raw) is is_json


# ---------------------------------------------------------------------------
# Content-Length
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param("0", 0, id="zero"),
        pytest.param("1024", 1024, id="positive"),
        pytest.param(" 1024 ", 1024, id="surrounding-whitespace"),
        pytest.param("-1", None, id="negative"),
        pytest.param("", None, id="empty"),
        pytest.param("abc", None, id="non-numeric"),
        pytest.param("1.5", None, id="fractional"),
        pytest.param("0x10", None, id="hex"),
    ],
)
def test_only_a_non_negative_integer_length_is_accepted(raw: str, expected: int | None) -> None:
    """Anything else means the server cannot bound the read and must refuse."""
    assert _parse_content_length(raw) == expected


# ---------------------------------------------------------------------------
# Body limits
# ---------------------------------------------------------------------------


def test_a_mutating_json_route_falls_back_to_the_default_limit() -> None:
    limit = _json_body_limit("POST", "/v1/notebooks/abc/unlisted", "application/json")

    assert limit is not None
    assert limit.max_bytes == DEFAULT_JSON_BODY_BYTES
    assert limit.name == "JSON"


@pytest.mark.parametrize(
    ("method", "path", "content_type"),
    [
        pytest.param("GET", "/v1/notebooks", "application/json", id="read-method"),
        pytest.param("POST", "/healthz", "application/json", id="outside-v1"),
        pytest.param("POST", "/v1/notebooks/abc/unlisted", "multipart/form-data", id="not-json"),
        pytest.param(
            "POST",
            "/v1/notebooks/abc/artifacts/def/retry",
            "application/json",
            id="declared-no-body-route",
        ),
    ],
)
def test_routes_outside_the_json_mutation_shape_carry_no_limit(
    method: str, path: str, content_type: str
) -> None:
    assert _json_body_limit(method, path, content_type) is None


def test_the_method_is_matched_case_insensitively() -> None:
    assert _json_body_limit("post", "/v1/notebooks/abc/unlisted", "application/json") is not None


def test_an_explicit_route_limit_wins_over_the_content_type_gate() -> None:
    """The declared table is consulted first, so its limit applies regardless."""
    limit = _json_body_limit("POST", "/v1/notebooks", "multipart/form-data")

    assert limit is not None
    assert limit.name == "notebook create"
    assert limit.max_bytes < DEFAULT_JSON_BODY_BYTES


# ---------------------------------------------------------------------------
# Startup-error projection
# ---------------------------------------------------------------------------


def test_an_auth_error_is_reprojected_without_its_original_context() -> None:
    normalized = _normalize_client_startup_error(AuthError("token expired"))

    assert isinstance(normalized, AuthError)
    assert str(normalized) == "token expired"


@pytest.mark.parametrize(
    "message",
    [
        pytest.param("Authentication expired", id="canonical-marker"),
        pytest.param("AUTHENTICATION EXPIRED OR INVALID", id="uppercase"),
        pytest.param("Please run 'notebooklm login' to continue", id="login-hint"),
        pytest.param("authentication\n  expired", id="collapsed-whitespace"),
    ],
)
def test_a_stale_auth_value_error_is_projected_onto_the_auth_category(message: str) -> None:
    """The bootstrap path raises plain ``ValueError`` for a stale local profile."""
    normalized = _normalize_client_startup_error(ValueError(message))

    assert isinstance(normalized, AuthError)
    assert str(normalized) == message


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            ValueError("some unrelated configuration problem"), id="unrelated-value-error"
        ),
        pytest.param(ValidationError("bad input"), id="already-a-library-error"),
        pytest.param(NotebookLMError("generic"), id="library-base-error"),
        pytest.param(RuntimeError("authentication expired"), id="wrong-type-despite-marker"),
        pytest.param(OSError("disk gone"), id="os-error"),
    ],
)
def test_everything_else_is_left_for_the_generic_projector(exc: Exception) -> None:
    assert _normalize_client_startup_error(exc) is None
