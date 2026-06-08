"""Unit tests for the transport-neutral ``notebooklm._app.source_add`` core.

These pin the relocated ``source add`` business logic at the ``_app`` boundary
(independent of the Click adapter):

* :func:`validate_url` — the SSRF / local-file-read guard (scheme allowlist,
  no-host rejection, private/loopback/link-local/unspecified IP rejection,
  ``localhost`` spelling rejection, the ``allow_internal`` bypass).
* :func:`looks_like_path` — slash / known-extension path heuristic.
* :func:`validate_upload_path` — symlink refusal + regular-file check.
* :func:`build_source_add_plan` — input-mode detection (url / youtube / file /
  text), warning collection, and the gate that explicit ``--type`` still honours.
* :func:`add_source` / :func:`execute_source_add` — the add-workflow dispatch
  to ``add_url`` / ``add_text`` / ``add_file``.

Pure-service tests (no Click / CliRunner): the command-layer wiring is exercised
in ``tests/unit/cli/test_source.py``.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from notebooklm._app.source_add import (
    SourceAddExecutionPlan,
    SourceAddPlan,
    SourceAddResult,
    SourceAddValidationError,
    add_source,
    build_source_add_plan,
    execute_source_add,
    looks_like_path,
    validate_upload_path,
    validate_url,
)
from notebooklm.exceptions import ValidationError
from notebooklm.types import Source

# ===========================================================================
# validate_url — SSRF / local-file-read guard
# ===========================================================================


class TestValidateUrl:
    @pytest.mark.parametrize(
        "url",
        [
            "http://example.com/a",
            "https://example.com/a",
            "HTTPS://Example.com/a",  # scheme case-insensitive
            "https://8.8.8.8/path",  # public IP literal
        ],
    )
    def test_accepts_public_http_urls(self, url: str) -> None:
        # Should not raise.
        validate_url(url, allow_internal=False)

    @pytest.mark.parametrize(
        "scheme",
        ["file", "ftp", "gopher", "data", "javascript"],
    )
    def test_rejects_disallowed_schemes(self, scheme: str) -> None:
        with pytest.raises(SourceAddValidationError) as exc:
            validate_url(f"{scheme}://etc/passwd", allow_internal=False)
        assert "scheme" in str(exc.value).lower()

    def test_rejects_disallowed_scheme_even_with_allow_internal(self) -> None:
        # The scheme allowlist still applies under allow_internal.
        with pytest.raises(SourceAddValidationError):
            validate_url("file:///etc/passwd", allow_internal=True)

    def test_rejects_missing_host(self) -> None:
        with pytest.raises(SourceAddValidationError) as exc:
            validate_url("http:///path", allow_internal=False)
        assert "no host" in str(exc.value).lower()

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080/x",  # loopback
            "http://10.0.0.1/x",  # private (RFC1918)
            "http://192.168.1.1/x",  # private
            "http://169.254.169.254/latest",  # link-local (cloud metadata)
            "http://0.0.0.0/x",  # unspecified
            "http://127.1/x",  # legacy numeric IPv4 spelling
            "http://localhost/x",  # localhost literal
            "http://foo.localhost/x",  # localhost suffix
        ],
    )
    def test_rejects_internal_hosts(self, url: str) -> None:
        with pytest.raises(SourceAddValidationError):
            validate_url(url, allow_internal=False)

    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1:8080/x",
            "http://10.0.0.1/x",
            "http://localhost/x",
        ],
    )
    def test_allow_internal_bypasses_host_check(self, url: str) -> None:
        # Should not raise once the host check is opted out.
        validate_url(url, allow_internal=True)

    def test_dns_name_is_accepted_without_resolution(self) -> None:
        # Non-localhost DNS names pass at this layer (no DNS resolution here).
        validate_url("http://internal-but-named.example/x", allow_internal=False)

    def test_validation_error_is_public_validation_error_subclass(self) -> None:
        # _app.errors.classify covers it uniformly via the public base.
        assert issubclass(SourceAddValidationError, ValidationError)


# ===========================================================================
# looks_like_path — path heuristic
# ===========================================================================


class TestLooksLikePath:
    @pytest.mark.parametrize(
        "content",
        ["docs/file.pdf", "C:\\foo\\bar", "report.pdf", "notes.md", "data.csv"],
    )
    def test_path_shaped_content(self, content: str) -> None:
        assert looks_like_path(content) is True

    @pytest.mark.parametrize(
        "content",
        ["just some text", "hello world", "a sentence."],
    )
    def test_non_path_content(self, content: str) -> None:
        assert looks_like_path(content) is False


# ===========================================================================
# validate_upload_path — symlink + regular-file checks
# ===========================================================================


class TestValidateUploadPath:
    def test_accepts_regular_file(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_text("x")
        resolved = validate_upload_path(str(f), follow_symlinks=False)
        assert resolved == f.resolve()

    def test_rejects_non_regular_file(self, tmp_path: Path) -> None:
        with pytest.raises(SourceAddValidationError) as exc:
            validate_upload_path(str(tmp_path / "missing.pdf"), follow_symlinks=False)
        assert "regular file" in str(exc.value).lower()

    def test_rejects_symlink_without_follow(self, tmp_path: Path) -> None:
        target = tmp_path / "real.pdf"
        target.write_text("x")
        link = tmp_path / "link.pdf"
        link.symlink_to(target)
        with pytest.raises(SourceAddValidationError) as exc:
            validate_upload_path(str(link), follow_symlinks=False)
        assert "symlink" in str(exc.value).lower()

    def test_follows_symlink_when_opted_in(self, tmp_path: Path) -> None:
        target = tmp_path / "real.pdf"
        target.write_text("x")
        link = tmp_path / "link.pdf"
        link.symlink_to(target)
        resolved = validate_upload_path(str(link), follow_symlinks=True)
        assert resolved == target.resolve()


# ===========================================================================
# build_source_add_plan — input detection + warning collection
# ===========================================================================


def _validate_path_stub(content: str, follow_symlinks: bool) -> Path:
    return Path(content)


class TestBuildSourceAddPlan:
    def test_autodetect_url(self) -> None:
        plan = build_source_add_plan(
            content="https://example.com/a",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=_validate_path_stub,
            looks_path_shaped=looks_like_path,
        )
        assert plan.detected_type == "url"
        assert plan.upload_path is None

    def test_autodetect_youtube(self) -> None:
        plan = build_source_add_plan(
            content="https://www.youtube.com/watch?v=abc123",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=_validate_path_stub,
            looks_path_shaped=looks_like_path,
        )
        assert plan.detected_type == "youtube"

    def test_autodetect_plain_text_default_title(self) -> None:
        plan = build_source_add_plan(
            content="just a note",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=_validate_path_stub,
            looks_path_shaped=looks_like_path,
        )
        assert plan.detected_type == "text"
        assert plan.title == "Pasted Text"
        assert plan.warnings == ()

    def test_autodetect_path_shaped_missing_warns_and_falls_back_to_text(self) -> None:
        plan = build_source_add_plan(
            content="docs/missing.pdf",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=_validate_path_stub,
            looks_path_shaped=looks_like_path,
        )
        assert plan.detected_type == "text"
        assert len(plan.warnings) == 1
        assert "looks like a path" in plan.warnings[0]

    def test_autodetect_existing_file(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_text("x")
        plan = build_source_add_plan(
            content=str(f),
            source_type=None,
            title=None,
            mime_type="application/pdf",
            follow_symlinks=False,
            validate_path=validate_upload_path,
            looks_path_shaped=looks_like_path,
        )
        assert plan.detected_type == "file"
        assert plan.upload_path == f.resolve()
        assert plan.mime_type == "application/pdf"

    def test_internal_url_rejected_before_type_binding(self) -> None:
        # A bad host must raise before url-vs-youtube is even decided.
        with pytest.raises(SourceAddValidationError):
            build_source_add_plan(
                content="http://127.0.0.1:8080/x",
                source_type=None,
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=_validate_path_stub,
                looks_path_shaped=looks_like_path,
            )

    def test_explicit_type_url_still_validates(self) -> None:
        # ``--type url file:///etc/passwd`` must not skip the gate.
        with pytest.raises(SourceAddValidationError):
            build_source_add_plan(
                content="file:///etc/passwd",
                source_type="url",
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=_validate_path_stub,
                looks_path_shaped=looks_like_path,
            )

    def test_explicit_type_youtube_validates_internal_host(self) -> None:
        # ``--type youtube`` honours the same internal-host gate.
        with pytest.raises(SourceAddValidationError):
            build_source_add_plan(
                content="http://127.0.0.1/x",
                source_type="youtube",
                title=None,
                mime_type=None,
                follow_symlinks=False,
                validate_path=_validate_path_stub,
                looks_path_shaped=looks_like_path,
            )

    def test_allow_internal_threaded_through(self) -> None:
        plan = build_source_add_plan(
            content="http://127.0.0.1:8080/x",
            source_type=None,
            title=None,
            mime_type=None,
            follow_symlinks=False,
            validate_path=_validate_path_stub,
            looks_path_shaped=looks_like_path,
            allow_internal=True,
        )
        assert plan.detected_type == "url"

    def test_explicit_type_file_validates_path(self, tmp_path: Path) -> None:
        f = tmp_path / "doc.pdf"
        f.write_text("x")
        plan = build_source_add_plan(
            content=str(f),
            source_type="file",
            title="My Doc",
            mime_type=None,
            follow_symlinks=False,
            validate_path=validate_upload_path,
            looks_path_shaped=looks_like_path,
        )
        assert plan.detected_type == "file"
        assert plan.upload_path == f.resolve()
        assert plan.title == "My Doc"

    def test_mime_type_only_kept_for_file(self) -> None:
        # A non-file detection drops the mime_type.
        plan = build_source_add_plan(
            content="just text",
            source_type="text",
            title=None,
            mime_type="application/pdf",
            follow_symlinks=False,
            validate_path=_validate_path_stub,
            looks_path_shaped=looks_like_path,
        )
        assert plan.detected_type == "text"
        assert plan.mime_type is None


# ===========================================================================
# add_source / execute_source_add — dispatch
# ===========================================================================


def _make_sources_facade() -> MagicMock:
    facade = MagicMock()
    facade.add_url = AsyncMock(return_value=Source(id="s_url", title="URL"))
    facade.add_text = AsyncMock(return_value=Source(id="s_txt", title="Text"))
    facade.add_file = AsyncMock(return_value=Source(id="s_file", title="File"))
    return facade


@pytest.mark.asyncio
async def test_add_source_url_dispatch() -> None:
    facade = _make_sources_facade()
    plan = SourceAddPlan(
        content="https://ex.com/a", detected_type="url", title=None, upload_path=None
    )
    src = await add_source(facade, notebook_id="nb_1", plan=plan)
    assert src.id == "s_url"
    facade.add_url.assert_awaited_once_with("nb_1", "https://ex.com/a")


@pytest.mark.asyncio
async def test_add_source_youtube_uses_add_url() -> None:
    facade = _make_sources_facade()
    plan = SourceAddPlan(
        content="https://youtu.be/abc", detected_type="youtube", title=None, upload_path=None
    )
    await add_source(facade, notebook_id="nb_1", plan=plan)
    facade.add_url.assert_awaited_once_with("nb_1", "https://youtu.be/abc")


@pytest.mark.asyncio
async def test_add_source_text_dispatch_default_title() -> None:
    facade = _make_sources_facade()
    plan = SourceAddPlan(content="some text", detected_type="text", title=None, upload_path=None)
    await add_source(facade, notebook_id="nb_1", plan=plan)
    facade.add_text.assert_awaited_once_with("nb_1", "Untitled", "some text")


@pytest.mark.asyncio
async def test_add_source_file_dispatch() -> None:
    facade = _make_sources_facade()
    plan = SourceAddPlan(
        content="ignored",
        detected_type="file",
        title="My File",
        upload_path=Path("/tmp/doc.pdf"),
        mime_type="application/pdf",
    )
    await add_source(facade, notebook_id="nb_1", plan=plan)
    # ``add_source`` forwards ``str(plan.upload_path)``; compare against the same
    # ``str(Path(...))`` so the assertion holds on Windows (``\tmp\doc.pdf``) too.
    facade.add_file.assert_awaited_once_with(
        "nb_1", str(Path("/tmp/doc.pdf")), "application/pdf", title="My File"
    )


@pytest.mark.asyncio
async def test_add_source_file_without_path_raises() -> None:
    facade = _make_sources_facade()
    plan = SourceAddPlan(content="x", detected_type="file", title=None, upload_path=None)
    with pytest.raises(SourceAddValidationError):
        await add_source(facade, notebook_id="nb_1", plan=plan)


@pytest.mark.asyncio
async def test_execute_source_add_returns_typed_result() -> None:
    client = MagicMock()
    client.sources = _make_sources_facade()
    plan = SourceAddPlan(
        content="https://ex.com/a", detected_type="url", title=None, upload_path=None
    )
    result = await execute_source_add(client, SourceAddExecutionPlan(notebook_id="nb_1", plan=plan))
    assert isinstance(result, SourceAddResult)
    assert result.source.id == "s_url"
