"""Source upload request payload builders."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ResumableUploadStartRequest:
    """HTTP request fields for starting a Scotty resumable upload."""

    url: str
    headers: dict[str, str]
    body: str


def build_retrieve_relevant_chunks_params(
    notebook_id: str,
    query: str,
    source_ids: list[str] | tuple[str, ...] | None,
) -> list[Any]:
    """Build ``RetrieveRelevantChunks`` params from its live proto layout.

    Fields 1/2 are notebook id and query, field 3 is unused, and field 4 is
    the retrieval-options message with mode ``1``. Field 5 is omitted when no
    source filter is requested; when present it wraps the repeated ``SourceId``
    messages in the filter message.
    """
    params: list[Any] = [notebook_id, query, None, [1]]
    if source_ids:
        params.append([[[source_id] for source_id in source_ids]])
    return params


def build_template_block() -> list[Any]:
    """Return the nested request-options wrapper ``[2, None, None, [1, ..., [1]]]``.

    Shared by ``CREATE_NOTEBOOK`` and every ``ADD_SOURCE`` / ``ADD_SOURCE_FILE``
    variant. This is the same wrapper the label RPCs already send
    (``_web.params.labels._opts``; its inner ``[1, ..., [1]]`` context block also
    appears in ``_web.settings``). Google's Gemini-3.5 rollout made create/source
    require the full wrapper too — they previously sent a degenerate
    ``[2], [1]`` (create) / ``[2], None, None`` (source) tail, which migrated
    backends now reject (``status=3``/``5``/``9``). Verified live against an
    un-migrated account. Returns a fresh list each call so callers never share a
    mutable nested structure. See https://github.com/teng-lin/notebooklm-py/issues/1546.
    """
    return [2, None, None, [1, None, None, None, None, None, None, None, None, None, [1]]]


def build_register_file_source_params(filename: str, notebook_id: str) -> list[Any]:
    """Build ``ADD_SOURCE_FILE`` params for file source registration.

    Uses the shared nested template block (#1546); the old flat
    ``[2], [1,...,[1]]`` tail no longer validates on migrated cohorts.
    """
    return [
        [[filename]],
        notebook_id,
        build_template_block(),
    ]


def build_rename_source_params(source_id: str, new_title: str) -> list[Any]:
    """Build ``UPDATE_SOURCE`` params for source title updates."""
    return [None, [source_id], [[[new_title]]]]


def build_resumable_upload_start_request(
    *,
    notebook_id: str,
    filename: str,
    file_size: int,
    source_id: str,
    content_type: str,
    upload_url: str,
    authuser_query: str,
    authuser_header: str,
) -> ResumableUploadStartRequest:
    """Build the HTTP request that starts a resumable upload session.

    ``Origin`` / ``Referer`` are derived from ``upload_url`` — the endpoint this
    request is actually POSTed to — rather than from a separately supplied base
    URL, so the two can never name different hosts (see
    :func:`notebooklm._web.sources._upload_decode._upload_url_origin`).
    """
    # Local import prevents the params module from eagerly initializing the
    # concrete ``_web.sources`` facade while its payload builders are imported.
    from ..sources._upload_decode import _upload_url_origin

    origin = _upload_url_origin(upload_url)
    return ResumableUploadStartRequest(
        url=f"{upload_url}?{authuser_query}",
        headers={
            "Accept": "*/*",
            "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8",
            "Origin": origin,
            "Referer": f"{origin}/",
            "x-goog-authuser": authuser_header,
            "x-goog-upload-command": "start",
            "x-goog-upload-header-content-length": str(file_size),
            "x-goog-upload-header-content-type": content_type,
            "x-goog-upload-protocol": "resumable",
        },
        body=json.dumps(
            {
                "PROJECT_ID": notebook_id,
                "SOURCE_NAME": filename,
                "SOURCE_ID": source_id,
            }
        ),
    )


def build_append_source_params(source_id: str, *, header: str, body: str) -> list[Any]:
    """Build ``APPEND_SOURCE`` (``QsNTEd`` / ``AppendSource``) params.

    Mobile proto (live-pinned, #2283): ``AppendSourceRequest { SourceId
    source_id = 2; SourceContent content = 4 }`` with ``SourceContent {
    PlainTextSourceContent plain_text = 2 }`` and ``PlainTextSourceContent {
    string header = 1; string body = 2 }``. Positionally that is a leading
    ``None`` (unused field 1), the ``[source_id]`` wrapper, another ``None``
    (field 3) and the doubly nested content block. The ``body`` lands in the
    source fulltext; the ``header`` does not appear in it.
    """
    return [None, [source_id], None, [None, [header, body]]]


def build_copy_sources_params(source_ids: list[str], target_notebook_id: str) -> list[Any]:
    """Build ``COPY_SOURCES`` (``R27wvc`` / ``CopySourcesAsync``) params.

    Mobile proto (live-pinned, #2283): ``{ repeated SourceId source_ids = 3;
    string target_project_id = 4 }`` — fields 1 and 2 are unused, so the
    positional request leads with two ``None`` slots.
    """
    return [None, None, [[source_id] for source_id in source_ids], target_notebook_id]


def build_add_sources_async_params(url_specs: list[list[Any]], notebook_id: str) -> list[Any]:
    """Build ``ADD_SOURCES_ASYNC`` (``X1snv`` / ``AddSourcesAsync``) params.

    Identical to the batch ``ADD_SOURCE`` request: the repeated ``UserContent``
    specs, the notebook id, then the request context (#2283).
    """
    return [url_specs, notebook_id, build_template_block()]


def build_list_play_books_params() -> list[Any]:
    """Build ``LIST_EXPERT_INTELLIGENCE_CONTENT`` (``mVtEUb``) params.

    ``[RequestContext, ContentProvider]``. ``1`` is ``GOOGLE_PLAY_BOOKS`` — the
    only provider the app sends and the only one the backend serves; the
    request context is unused for the list, so a bare ``None`` is sent for it
    (both ``[None, 1]`` and a populated context return the same rows, verified
    live 2026-09-01, #2292).
    """
    return [None, 1]


#: The ``UserContent`` (web spec ``vv``) index that holds the
#: ``ExpertIntelligenceContent`` oneof (proto tag 16). The server parses the
#: payload only at index 15 — 14 and 16 both decode as ``status 3`` (#2292).
_EXPERT_INTELLIGENCE_SPEC_INDEX = 15
#: The ``vv.f11 = 1`` marker every source spec carries, at index 10.
_SPEC_F11_INDEX = 10
#: ``ContentProvider.GOOGLE_PLAY_BOOKS``.
_GOOGLE_PLAY_BOOKS_PROVIDER = 1


def build_play_book_source_spec(
    content_id: str,
    title: str | None,
    description_html: str | None,
    cover_url: str | None,
    field_type: float | None,
    authors: list[str],
) -> list[Any]:
    """Build one ``UserContent`` spec adding a Play Book (#2292).

    Lays the ``ExpertIntelligenceContent`` message —
    ``[provider, content_id, title, description, cover_url, field_type,
    [authors]]`` — at index 15, with the shared ``f11 = 1`` marker at index 10
    and ``None`` everywhere else. Wrapped as ``[[spec], notebook_id, ctx]`` by
    :func:`build_add_sources_async_params` and dispatched through
    ``ADD_SOURCES_ASYNC``; the created source ingests to ``EXPERT_INTELLIGENCE``
    (verified live end to end on the web tier).
    """
    ei_content = [
        _GOOGLE_PLAY_BOOKS_PROVIDER,
        content_id,
        title,
        description_html,
        cover_url,
        field_type,
        list(authors),
    ]
    spec: list[Any] = [None] * 16
    spec[_SPEC_F11_INDEX] = 1
    spec[_EXPERT_INTELLIGENCE_SPEC_INDEX] = ei_content
    return spec
