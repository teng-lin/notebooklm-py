"""Record-or-replay harness for Android gRPC cassette tests.

Both modes drive the *public* ``NotebookLMClient(..., backend="android")`` so
a cassette test exercises the same assembly path users do. The only injection
is the ``grpc_loader`` seam of ``AndroidSession``:

* **Replay** (default; CI): ``ReplayGrpcModule`` + ``ReplayBearer``. No live
  channel, no OAuth mint, synthetic ``AuthTokens``. A cassette miss fails —
  there is no live fallback.
* **Record** (``NOTEBOOKLM_ANDROID_GRPC_RECORD=1``; local only): the real
  ``grpc`` module wrapped in ``RecordingGrpcModule``, the real bearer provider,
  and the developer's profile. A disposable scratch notebook is created and
  deleted by a *separate* plain client so its setup traffic is never recorded.

Placeholders for test inputs are reserved on the redactor before any traffic,
in the same order in both modes, so the replay side knows the exact
placeholder each input received (see ``ProtoRedactor.reserve``).
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from notebooklm import AuthTokens, NotebookLMClient
from notebooklm._android import auth as android_auth
from notebooklm._android import phenotype as android_phenotype
from notebooklm._android import session as android_session

from .android_grpc_cassette import (
    ProtoRedactor,
    RecordingGrpcModule,
    ReplayBearer,
    ReplayGrpcModule,
    compose_sanitizers,
)
from .android_grpc_normalizers import normalize_request

RECORD_ENV = "NOTEBOOKLM_ANDROID_GRPC_RECORD"
SCRATCH_TITLE = "notebooklm-py grpc cassette scratch"
SCRATCH_SOURCE_TITLE = "Cassette scratch source"
SCRATCH_SOURCE_TEXT = (
    "The notebooklm-py project records sanitized Android gRPC cassettes. "
    "This scratch source exists only so read RPCs return a populated project."
)
SCRATCH_NOTE_TITLE = "Cassette scratch note"
SCRATCH_NOTE_CONTENT = "A note recorded into a sanitized cassette."
QUESTION = "What does this notebook say about cassettes?"
# A real web query: fast research must find importable results while recording.
RESEARCH_QUERY = "gRPC protobuf wire format"
# ``sources.py`` mints one ``nblm-<hex>`` correlation name per registered source
# and matches the server's echo of it; a cassette family may register at most
# this many sources.
CORRELATION_BUDGET = 6
# Free-text inputs a family may pass to the public API (titles, names, queries,
# emoji). They are reserved so a value the client later reads back from a
# response compares equal on replay.
TEXT_BUDGET = 16
URL = "https://example.com/"


def is_record_mode() -> bool:
    return os.environ.get(RECORD_ENV, "").casefold() in ("1", "true", "yes")


@dataclass(frozen=True)
class ScratchNotebook:
    """The disposable notebook's real id (record mode only).

    Its source and note ids are deliberately not carried: tests discover them
    through the public API so the discovered placeholders match on replay.
    """

    notebook_id: str


@dataclass(frozen=True)
class CassetteValues:
    """What a cassette test passes to the public API in the current mode.

    Record mode carries the real scratch identifiers; replay mode carries the
    deterministic placeholders those identifiers were redacted to.
    """

    notebook_id: str
    question: str
    url: str = URL
    research_query: str = RESEARCH_QUERY
    correlations: tuple[str, ...] = ()
    texts: tuple[str, ...] = ()


def _fresh_correlations() -> tuple[str, ...]:
    from notebooklm._android.sources import _CORRELATION_PREFIX

    return tuple(f"{_CORRELATION_PREFIX}{uuid4().hex}" for _ in range(CORRELATION_BUDGET))


def _fresh_texts() -> tuple[str, ...]:
    return tuple(f"Cassette text {index:02d}" for index in range(1, TEXT_BUDGET + 1))


def bind_values(
    redactor: ProtoRedactor,
    *,
    notebook_id: str,
    question: str,
    record: bool,
    url: str = URL,
    correlations: tuple[str, ...] = (),
    texts: tuple[str, ...] = (),
) -> CassetteValues:
    """Reserve placeholders for the inputs and return the mode-appropriate values.

    Reservation order is the contract: notebook id, question, url, research
    query, each correlation name, then each free text. Both modes reserve the
    same kinds in the same order. Client constants inside requests need no
    reservation: request sanitization numbers them per request.
    """

    placeholders = CassetteValues(
        notebook_id=redactor.reserve(notebook_id),
        question=redactor.reserve(question),
        url=redactor.reserve(url),
        research_query=redactor.reserve(RESEARCH_QUERY),
        correlations=tuple(redactor.reserve(name) for name in correlations),
        texts=tuple(redactor.reserve(text) for text in texts),
    )
    if record:
        return CassetteValues(
            notebook_id=notebook_id,
            question=question,
            url=url,
            research_query=RESEARCH_QUERY,
            correlations=correlations,
            texts=texts,
        )
    return placeholders


def _inject_correlation_names(monkeypatch: pytest.MonkeyPatch, names: tuple[str, ...]) -> None:
    """Replace the random correlation minter with the reserved sequence."""

    from notebooklm._android import sources as android_sources

    remaining = list(names)

    def next_name() -> str:
        if not remaining:
            raise RuntimeError(f"Cassette family registered more than {CORRELATION_BUDGET} sources")
        return remaining.pop(0)

    monkeypatch.setattr(android_sources, "_correlation_name", next_name)


def _live_client() -> Any:
    """The canonical profile-backed client (honours ``NOTEBOOKLM_PROFILE``)."""

    return NotebookLMClient.from_storage(backend="android")


async def create_scratch_notebook() -> ScratchNotebook:
    """Create the disposable notebook through an unrecorded live client."""

    async with _live_client() as client:
        notebook = await client.notebooks.create(SCRATCH_TITLE)
        try:
            await client.sources.add_text(
                notebook.id, SCRATCH_SOURCE_TITLE, SCRATCH_SOURCE_TEXT, wait=True
            )
            await client.notes.create(
                notebook.id, title=SCRATCH_NOTE_TITLE, content=SCRATCH_NOTE_CONTENT
            )
        except BaseException:
            # Best-effort cleanup must not mask the original failure; a leaked
            # scratch notebook is reported by id so it can be deleted by hand.
            try:
                await client.notebooks.delete(notebook.id)
            except Exception as cleanup_error:  # noqa: BLE001 - reported, not hidden
                print(
                    f"WARNING: scratch notebook {notebook.id} was not deleted: "
                    f"{type(cleanup_error).__name__}"
                )
            raise
    return ScratchNotebook(notebook_id=notebook.id)


async def delete_scratch_notebook(scratch: ScratchNotebook) -> None:
    async with _live_client() as client:
        await client.notebooks.delete(scratch.notebook_id)


def _inject_grpc_loader(monkeypatch: pytest.MonkeyPatch, grpc_module: Any) -> None:
    production_session = android_session.AndroidSession

    def seamed_session(
        bearer_provider: Any,
        supervisor: Any,
        *,
        timeout: float | None,
        rate_limit_max_retries: int,
        server_error_max_retries: int,
        refresh_retry_delay: float,
        metrics: Any,
        sleep: Any,
    ) -> android_session.AndroidSession:
        return production_session(
            bearer_provider,
            supervisor,
            timeout=timeout,
            rate_limit_max_retries=rate_limit_max_retries,
            server_error_max_retries=server_error_max_retries,
            refresh_retry_delay=refresh_retry_delay,
            metrics=metrics,
            sleep=sleep,
            grpc_loader=lambda: grpc_module,
        )

    monkeypatch.setattr(android_session, "AndroidSession", seamed_session)


RecordedCallback = Callable[[Path, Path], None]


def promote_recording(staging_path: Path, cassette_path: Path) -> None:
    """Move a finished staging file over the committed cassette."""

    if not staging_path.exists():
        raise RuntimeError(f"Recording {cassette_path.name} captured no interactions")
    os.replace(staging_path, cassette_path)


def discard_recording(staging_path: Path) -> None:
    staging_path.unlink(missing_ok=True)


@asynccontextmanager
async def android_cassette_client(
    cassette_path: Path,
    *,
    monkeypatch: pytest.MonkeyPatch,
    scratch: ScratchNotebook | None,
    question: str = QUESTION,
    phenotype_cassette_path: Path | None = None,
    on_recorded: RecordedCallback | None = None,
) -> AsyncIterator[tuple[NotebookLMClient, CassetteValues]]:
    """Open the public Android client bound to ``cassette_path`` in the current mode.

    While recording, the cassette is written to a sibling staging file. When
    ``on_recorded`` is given it receives ``(staging_path, cassette_path)`` once
    the recorded traffic completed without error and the caller decides when to
    promote (the fixture promotes only if the whole test passed); otherwise the
    staging file is promoted immediately.
    """

    redactor = ProtoRedactor(trust_placeholders=True)
    record = is_record_mode()
    phenotype_staging_path: Path | None = None
    phenotype_http_post: Any | None = None
    if phenotype_cassette_path is not None:
        from .android_phenotype_http_cassette import build_phenotype_http_post

        provider_type = android_phenotype.PhenotypeTokenProvider
        phenotype_capture_path = phenotype_cassette_path
        if record:
            phenotype_staging_path = phenotype_cassette_path.with_name(
                phenotype_cassette_path.name + ".recording"
            )
            discard_recording(phenotype_staging_path)
            phenotype_capture_path = phenotype_staging_path
        phenotype_http_post = build_phenotype_http_post(phenotype_capture_path)

        def cassette_provider(*args: Any, **kwargs: Any) -> Any:
            kwargs["http_post"] = phenotype_http_post
            return provider_type(*args, **kwargs)

        monkeypatch.setattr(android_phenotype, "PhenotypeTokenProvider", cassette_provider)
    if record:
        if scratch is None:
            raise RuntimeError("Recording requires the android_record_scratch fixture")
        values = bind_values(
            redactor,
            notebook_id=scratch.notebook_id,
            question=question,
            record=True,
            correlations=_fresh_correlations(),
            texts=_fresh_texts(),
        )
        _inject_correlation_names(monkeypatch, values.correlations)
        import grpc

        # Record next to the target; promotion happens only after the recorded
        # traffic completed without error -- and, through ``on_recorded``, only
        # after the test's own assertions passed -- so neither an auth failure,
        # a mid-family RPC error, nor a failing assertion can replace a good
        # committed cassette.
        staging_path = cassette_path.with_name(cassette_path.name + ".recording")
        discard_recording(staging_path)
        recorder = RecordingGrpcModule(
            grpc,
            staging_path,
            sanitizer=normalize_request,
            redactor=redactor,
        )
        _inject_grpc_loader(monkeypatch, recorder)
        try:
            async with _live_client() as client:
                assert set(client.backends.values()) == {"android"}
                yield client, values
            if phenotype_http_post is not None:
                phenotype_http_post.assert_consumed()
        except BaseException:
            discard_recording(staging_path)
            if phenotype_staging_path is not None:
                discard_recording(phenotype_staging_path)
            raise
        recordings = [(staging_path, cassette_path)]
        if phenotype_staging_path is not None and phenotype_cassette_path is not None:
            recordings.append((phenotype_staging_path, phenotype_cassette_path))
        missing = [target.name for staging, target in recordings if not staging.exists()]
        if missing:
            for staging, _target in recordings:
                discard_recording(staging)
            raise RuntimeError(f"Recording captured no interactions for: {', '.join(missing)}")
        for finished_path, target_path in recordings:
            if on_recorded is None:
                promote_recording(finished_path, target_path)
            else:
                on_recorded(finished_path, target_path)
        return

    values = bind_values(
        redactor,
        notebook_id=str(uuid4()),
        question=question,
        record=False,
        correlations=_fresh_correlations(),
        texts=_fresh_texts(),
    )
    _inject_correlation_names(monkeypatch, values.correlations)
    replay = ReplayGrpcModule(
        cassette_path,
        sanitizer=compose_sanitizers(normalize_request, redactor),
    )
    bearer = ReplayBearer()
    monkeypatch.setattr(android_auth, "_make_bearer_provider", lambda _storage_path: bearer)
    _inject_grpc_loader(monkeypatch, replay)
    auth = AuthTokens(
        cookies={"SID": "synthetic-cookie"},
        csrf_token="synthetic-csrf",
        session_id="synthetic-session",
    )
    async with NotebookLMClient(auth, backend="android") as client:
        assert set(client.backends.values()) == {"android"}
        yield client, values
    if phenotype_http_post is not None:
        phenotype_http_post.assert_consumed()
    replay.assert_consumed()
    assert bearer.gets, "replay never consulted the non-secret bearer"
    assert replay.secure_channel_calls == 1


__all__ = [
    "CORRELATION_BUDGET",
    "QUESTION",
    "RESEARCH_QUERY",
    "TEXT_BUDGET",
    "URL",
    "RECORD_ENV",
    "CassetteValues",
    "ScratchNotebook",
    "android_cassette_client",
    "bind_values",
    "create_scratch_notebook",
    "delete_scratch_notebook",
    "discard_recording",
    "is_record_mode",
    "promote_recording",
]
