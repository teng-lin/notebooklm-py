"""Integration test for ``resolve_chat_reference_passage``.

End-to-end exercise of the top-level helper that resolves a
:class:`ChatReference` to its surrounding source-text passage. The
helper composes ``client.sources.get_fulltext`` with
:meth:`SourceFulltext.find_citation_context`, and this test verifies
the full round-trip: a synthesized chat reference with a real cited
substring lands on a non-empty surrounding passage containing that
substring.

Marked ``allow_no_vcr`` because the test exercises only one RPC
(``GET_SOURCE``) and the value comes from asserting on the helper's
slice-and-search behaviour over a deterministic fulltext payload — a
real cassette of an unrelated cited-text/source pair would not make
the test more honest, and recording one would need live auth that
isn't available to the polish/CI environment.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient, resolve_chat_reference_passage
from notebooklm.exceptions import ChatResponseParseError
from notebooklm.rpc import RPCMethod
from notebooklm.types import ChatReference

pytestmark = [pytest.mark.allow_no_vcr]


def _build_fulltext_response(
    *,
    source_id: str,
    title: str,
    content_chunks: list[str],
    source_type: int = 5,
    build_rpc_response,
) -> bytes:
    """Construct a GET_SOURCE batchexecute response with the given chunks.

    Mirrors the response shape consumed by ``SourceContentRenderer.get_fulltext``
    in text mode: ``result[0]`` carries title + metadata, ``result[3][0]``
    carries the recursively-extracted content blocks.
    """
    data = [
        [
            source_id,
            title,
            [None, None, None, None, source_type],  # metadata; source_type at idx 4
        ],
        None,
        None,
        [content_chunks],  # result[3][0] is the list of strings
    ]
    return build_rpc_response(RPCMethod.GET_SOURCE, data).encode()


class TestResolveChatReferencePassage:
    """End-to-end checks for ``resolve_chat_reference_passage``."""

    @pytest.mark.asyncio
    async def test_returns_non_empty_surrounding_passage(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """The helper returns the surrounding context for a cited span.

        Acceptance: the returned passage must be non-empty and must
        contain the cited substring (since ``find_citation_context``
        slices around the match position).
        """
        # 60-char prefix → comfortably above the 40-char search-prefix cap
        # used inside ``SourceFulltext.find_citation_context`` so the
        # match is unambiguous.
        cited_text = "Quantum entanglement permits non-classical correlations between"
        prelude = (
            "In the early 20th century, physicists wrestled with a counter-intuitive "
            "feature of the new mechanics: "
        )
        epilogue = (
            " spatially separated systems, a phenomenon that Einstein once "
            "dismissed as 'spooky action at a distance'."
        )
        content = prelude + cited_text + epilogue

        response = _build_fulltext_response(
            source_id="src_quantum",
            title="Quantum Mechanics Primer",
            content_chunks=[content],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        reference = ChatReference(source_id="src_quantum", cited_text=cited_text)

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client,
                notebook_id="nb_quantum",
                reference=reference,
                context_chars=80,
            )

        assert passage, "Resolver must return a non-empty surrounding passage"
        # The cited prefix (first 40 chars per ``find_citation_context``)
        # must appear inside the surrounding window.
        assert cited_text[:40] in passage
        # And some surrounding context must come along for the ride —
        # otherwise the helper is just echoing the cited text.
        assert len(passage) > len(cited_text[:40])

    @pytest.mark.asyncio
    async def test_raises_when_reference_has_no_cited_text(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ) -> None:
        """A structural-anchor citation (no cited_text) raises cleanly.

        Per :attr:`ChatReference.cited_text` semantics, single-char anchor
        citations carry no plaintext to resolve — the helper must surface
        that with ``ChatResponseParseError`` rather than silently calling
        ``get_fulltext`` for nothing.
        """
        reference = ChatReference(source_id="src_anchor", cited_text=None)

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ChatResponseParseError, match="no cited_text"):
                await resolve_chat_reference_passage(
                    client,
                    notebook_id="nb_quantum",
                    reference=reference,
                )

        # No fulltext fetch should have been attempted.
        assert not httpx_mock.get_requests()

    @pytest.mark.asyncio
    async def test_raises_when_cited_text_not_found_in_source(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """A cited span that doesn't appear in the fulltext raises.

        Re-chunking by the server between the citation and a follow-up
        fulltext fetch can produce this mismatch. The helper surfaces it
        as ``ChatResponseParseError`` so callers can fall back to the
        ``cited_text`` they already had.
        """
        response = _build_fulltext_response(
            source_id="src_other",
            title="Unrelated Document",
            content_chunks=["This document is about cooking pasta."],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        reference = ChatReference(
            source_id="src_other",
            cited_text="quantum entanglement permits non-classical correlations",
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ChatResponseParseError, match="Could not locate"):
                await resolve_chat_reference_passage(
                    client,
                    notebook_id="nb_other",
                    reference=reference,
                )

    @pytest.mark.asyncio
    async def test_resolver_is_reexported_from_top_level(self) -> None:
        """``resolve_chat_reference_passage`` is importable from ``notebooklm``.

        Codifies the public-API contract: callers should not need to
        reach into ``notebooklm.utils`` to use the helper.
        """
        import notebooklm

        assert hasattr(notebooklm, "resolve_chat_reference_passage")
        assert "resolve_chat_reference_passage" in notebooklm.__all__

    @pytest.mark.asyncio
    async def test_resolver_passes_context_chars_to_find_citation_context(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """The ``context_chars`` knob is honored end-to-end.

        Asks for a wide window (300 chars) and a narrow one (20 chars)
        and verifies the wide window returns more characters around the
        cited span. This pins the parameter contract so future helper
        refactors can't quietly drop the knob.
        """
        cited_text = "the principle of least action"
        # Wrap the cited text in enough filler on both sides to make the
        # wide vs narrow context windows distinguishable.
        filler = "A" * 400
        content = filler + " " + cited_text + " " + filler

        # Two GET_SOURCE responses for two calls.
        response = _build_fulltext_response(
            source_id="src_action",
            title="Variational Principles",
            content_chunks=[content],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response, is_reusable=True)

        reference = ChatReference(source_id="src_action", cited_text=cited_text)

        async with NotebookLMClient(auth_tokens) as client:
            wide = await resolve_chat_reference_passage(
                client, "nb_action", reference, context_chars=300
            )
            narrow = await resolve_chat_reference_passage(
                client, "nb_action", reference, context_chars=20
            )

        assert len(wide) > len(narrow)
        assert cited_text[:40] in wide
        assert cited_text[:40] in narrow
