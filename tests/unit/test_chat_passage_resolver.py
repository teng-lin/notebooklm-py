"""Tests for ``resolve_chat_reference_passage``.

End-to-end exercise of the top-level helper that resolves a
:class:`ChatReference` to its surrounding source-text passage. The helper
composes ``client.sources.get_fulltext`` with the citation's own character
range — reading the passage straight out of the source document's coordinate
space — and falls back to the value-based
:meth:`SourceFulltext.find_citation_context` search only when that range is
unusable (#2211). Both paths are exercised here, along with the reasons the
offset path stands aside.

The GET_SOURCE response is deterministic and served by ``pytest_httpx``;
no live API or cassette is needed for this coverage.
"""

from __future__ import annotations

import pytest
from pytest_httpx import HTTPXMock

from notebooklm import NotebookLMClient, resolve_chat_reference_passage
from notebooklm.exceptions import ChatResponseParseError
from notebooklm.rpc import RPCMethod
from notebooklm.types import ChatReference, utf16_len


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


def _build_block_document_response(
    *,
    source_id: str,
    title: str,
    blocks: list[list[str]],
    build_rpc_response,
) -> tuple[bytes, str, tuple[int, int]]:
    """A GET_SOURCE response whose ``result[3][0]`` is a real document ``Body``.

    The bare-strings builder above decodes to *no* blocks — so every test using
    it exercises only the search fallback. This one emits the shape the wire
    actually sends (``StructuralElement`` rows, each holding its text runs with
    their offsets), which is what makes offset resolution reachable at all.

    Each inner list is one block's **runs**: the sub-paragraph fragments the
    backend splits a paragraph into, and the reason ``content`` renders a
    paragraph as several lines.

    Offsets are laid out with :func:`utf16_len`, not ``len`` — the wire counts
    UTF-16 code units, so a block containing an emoji occupies one more
    position than it has Python characters, and a builder using ``len`` here
    would quietly encode this client's old misreading instead of the wire's
    convention.

    Returns the response bytes, the ``cited_text`` a fragment spanning every
    block would now carry, and that fragment's ``(start_char, end_char)``.
    """
    elements: list[object] = []
    cursor = 0
    for runs in blocks:
        spans = []
        block_start = cursor
        for run in runs:
            end = cursor + utf16_len(run)
            spans.append([cursor, end, [run]])
            cursor = end
        elements.append([block_start, cursor, [spans]])
    data = [
        [source_id, title, [None, None, None, None, 5]],
        None,
        None,
        [[elements]],  # result[3][0] == Body == [content, ...]
    ]
    return (
        build_rpc_response(RPCMethod.GET_SOURCE, data).encode(),
        "".join(run for runs in blocks for run in runs),
        (0, cursor),
    )


def _build_document_fulltext_response(
    *,
    source_id: str,
    title: str,
    block_texts: list[str],
    build_rpc_response,
) -> tuple[bytes, str, tuple[int, int]]:
    """:func:`_build_block_document_response` with one run per block."""
    return _build_block_document_response(
        source_id=source_id,
        title=title,
        blocks=[[text] for text in block_texts],
        build_rpc_response=build_rpc_response,
    )


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
    async def test_resolves_a_fragment_whose_first_block_is_shorter_than_the_search_key(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """Regression: multi-block citations are unsearchable in ``content``.

        ``find_citation_context`` searches ``cited_text[:40]`` inside
        ``SourceFulltext.content``, which joins the document's text runs with
        ``"\n"``. Since #2120 made ``cited_text`` span the whole fragment with
        **no** separator, any fragment whose first block is shorter than 40
        characters produces a search key that crosses a block boundary — and so
        cannot occur in ``content`` at all. Before offset resolution this raised
        ``ChatResponseParseError`` for a perfectly valid citation, and
        note-saving broke on it.

        The heading here is 14 characters, well under the 40-character key, so
        the key straddles the boundary exactly as a real source's title block
        does.
        """
        block_texts = [
            "Photosynthesis",
            "Photosynthesis converts light into chemical energy.",
            "It runs in two stages.",
        ]
        response, cited_text, (start_char, end_char) = _build_document_fulltext_response(
            source_id="src_photo",
            title="Photosynthesis",
            block_texts=block_texts,
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        # The preconditions that make this a regression rather than a quirk:
        # a short first block, and an unbounded key that provably cannot occur
        # in the newline-joined rendering the search runs against.
        assert len(block_texts[0]) < 40
        assert cited_text[:40] not in "\n".join(block_texts)

        reference = ChatReference(
            source_id="src_photo",
            cited_text=cited_text,
            start_char=start_char,
            end_char=end_char,
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client,
                notebook_id="nb_photo",
                reference=reference,
                context_chars=20,
            )

        # Resolves at all — before the fix this raised ChatResponseParseError,
        # because the 40-character key straddled the first block boundary and so
        # could not occur in the newline-joined ``content`` — and lands on the
        # cited span rather than somewhere arbitrary.
        assert passage
        assert block_texts[0] in passage

    @pytest.mark.asyncio
    async def test_falls_back_to_search_when_the_document_did_not_decode(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """Offsets are preferred, not required — the search path still works.

        A source whose document does not decode (or a reference predating the
        offsets) must still resolve, or this would trade one regression for
        another.
        """
        cited_text = "Quantum entanglement permits non-classical correlations between"
        content = "Preamble text. " + cited_text + " And a trailing clause."
        response = _build_fulltext_response(
            source_id="src_fallback",
            title="Fallback",
            content_chunks=[content],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        # Offsets present but the document is empty, so they cannot be used.
        reference = ChatReference(
            source_id="src_fallback", cited_text=cited_text, start_char=0, end_char=63
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_fallback", reference=reference, context_chars=10
            )

        assert cited_text[:40] in passage

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


class TestOffsetResolution:
    """The range is read, not searched for (#2211).

    A citation carries ``start_char`` / ``end_char`` into the source
    document's own coordinate space. Reading them is exact; searching for a
    prefix of ``cited_text`` inside the flat ``content`` is a value comparison
    between two strings that render the same source differently, and #2210
    could only bound its failure mode rather than remove it. These tests pin
    which path runs, and what makes the resolver stand the offsets down.
    """

    @pytest.mark.asyncio
    async def test_offsets_pick_the_cited_occurrence_and_not_the_first_match(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """The distinguishing test: a source that says the same thing twice.

        A search can only return the *first* place the text occurs, so a
        citation into the second one resolves to a passage from the wrong
        chapter — plausible, adjacent, and wrong. The range says which
        occurrence, and reading it is the only way to honour that.
        """
        blocks = [
            ["Chapter one."],
            ["The signal is weak."],
            ["Chapter two."],
            ["The signal is weak."],
            ["Chapter three."],
        ]
        response, _all_text, _range = _build_block_document_response(
            source_id="src_repeat",
            title="Repeats",
            blocks=blocks,
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        # The second occurrence's range: everything before it, then its width.
        second_start = sum(utf16_len(runs[0]) for runs in blocks[:3])
        second_end = second_start + utf16_len("The signal is weak.")

        reference = ChatReference(
            source_id="src_repeat",
            cited_text="The signal is weak.",
            start_char=second_start,
            end_char=second_end,
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_repeat", reference=reference, context_chars=14
            )

        assert "The signal is weak." in passage
        assert "Chapter three." in passage  # the neighbour of the cited occurrence
        assert "Chapter one." not in passage  # the neighbour of the first match

    @pytest.mark.asyncio
    async def test_the_passage_is_the_readable_rendering_not_the_flat_one(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """A paragraph comes back whole, the way #2211's third surface renders it.

        ``content`` joins text *runs* with ``"\\n"``, so the very paragraph
        used here — the shape of the one in this repo's captured fixture —
        arrives as three lines. The offset path renders from the document
        instead: runs joined within the block, blocks separated.
        """
        paragraph_runs = [
            "Photosynthesis is the process by which green plants convert light energy into",
            " ",
            "chemical energy.",
        ]
        response, _all_text, _range = _build_block_document_response(
            source_id="src_runs",
            title="Photosynthesis",
            blocks=[["Photosynthesis"], paragraph_runs],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        start = utf16_len("Photosynthesis")
        end = start + sum(utf16_len(run) for run in paragraph_runs)
        reference = ChatReference(
            source_id="src_runs",
            cited_text="".join(paragraph_runs),
            start_char=start,
            end_char=end,
        )

        async with NotebookLMClient(auth_tokens) as client:
            fulltext = await client.sources.get_fulltext("nb_runs", "src_runs")
        httpx_mock.add_response(content=response)
        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_runs", reference=reference, context_chars=0
            )

        assert passage == "".join(paragraph_runs)
        # ...which is precisely what the flat rendering cannot give back.
        assert "light energy into\n \nchemical energy." in fulltext.content
        assert "light energy into chemical energy." in fulltext.rendered_content

    @pytest.mark.asyncio
    async def test_a_reference_with_a_range_and_no_cited_text_still_resolves(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """Offsets alone are enough — the search needed a string, this does not."""
        response, _all_text, _range = _build_block_document_response(
            source_id="src_norange",
            title="No cited text",
            blocks=[["Opening line."], ["The interesting middle."], ["Closing line."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        start = utf16_len("Opening line.")
        reference = ChatReference(
            source_id="src_norange",
            cited_text=None,
            start_char=start,
            end_char=start + utf16_len("The interesting middle."),
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_norange", reference=reference, context_chars=0
            )

        assert passage == "The interesting middle."

    @pytest.mark.asyncio
    async def test_a_range_past_the_documents_extent_falls_back_to_the_search(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """An out-of-range citation must not resolve to whatever happens to be there.

        This is the shape a re-indexed source produces: the offsets still look
        like offsets, but they describe a document this one no longer is.
        Clamping them silently returns the document's tail — text that is not
        the citation — so the range is checked against
        ``StructuredDocument.extent`` and, failing that, the value search runs.
        """
        cited_text = "Alpha beta gamma delta epsilon zeta eta theta iota."
        response, _all_text, _range = _build_block_document_response(
            source_id="src_stale",
            title="Re-indexed",
            blocks=[[cited_text], ["A tail block that is not the citation."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        extent = utf16_len(cited_text) + utf16_len("A tail block that is not the citation.")
        reference = ChatReference(
            source_id="src_stale",
            cited_text=cited_text,
            start_char=extent - 10,
            end_char=extent + 200,  # past the end of the document as it stands now
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_stale", reference=reference, context_chars=5
            )

        # Resolved by search, at the citation — not by clamping the range onto
        # the tail block it would otherwise have landed in.
        assert cited_text[:40] in passage
        assert "A tail block" not in passage

    @pytest.mark.asyncio
    async def test_no_request_is_made_for_a_reference_with_neither_text_nor_a_range(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ) -> None:
        """The short-circuit survives the rewrite, for every unusable range.

        Fetching before checking would turn a structural anchor — which has
        nothing to resolve either way — into an RPC the helper used to avoid.
        A zero-width range is as unresolvable as a missing one (it selects no
        text however it is read), so both must stop here rather than at the
        document. The other unusable shapes — half-populated, negative,
        inverted — cannot reach this function: ``ChatReference`` rejects them
        at construction, which the assertions below pin.
        """
        unusable = [
            ChatReference(source_id="src_anchor", cited_text=None),
            ChatReference(source_id="src_anchor", cited_text=None, start_char=5, end_char=5),
        ]
        for start, end in ((9, 4), (-3, 10), (5, None)):
            with pytest.raises(ValueError):
                ChatReference(
                    source_id="src_anchor", cited_text=None, start_char=start, end_char=end
                )

        async with NotebookLMClient(auth_tokens) as client:
            for reference in unusable:
                with pytest.raises(ChatResponseParseError, match="no cited_text"):
                    await resolve_chat_reference_passage(
                        client, notebook_id="nb_anchor", reference=reference
                    )

        assert not httpx_mock.get_requests()

    @pytest.mark.asyncio
    async def test_context_chars_widens_the_offset_window(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """The knob keeps working on the path that no longer searches."""
        blocks = [["Before block."], ["The cited block."], ["After block."]]
        response, _all_text, _range = _build_block_document_response(
            source_id="src_ctx",
            title="Context",
            blocks=blocks,
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response, is_reusable=True)

        start = utf16_len("Before block.")
        reference = ChatReference(
            source_id="src_ctx",
            cited_text="The cited block.",
            start_char=start,
            end_char=start + utf16_len("The cited block."),
        )

        async with NotebookLMClient(auth_tokens) as client:
            narrow = await resolve_chat_reference_passage(
                client, "nb_ctx", reference, context_chars=0
            )
            wide = await resolve_chat_reference_passage(
                client, "nb_ctx", reference, context_chars=40
            )

        assert narrow == "The cited block."
        assert wide == "Before block.\nThe cited block.\nAfter block."

    @pytest.mark.asyncio
    async def test_a_source_containing_an_emoji_resolves_to_the_right_characters(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """Composing the two APIs across an astral character (#2211).

        The offsets are UTF-16 code units and Python indexes code points, so a
        single emoji ahead of the cited span shifts every later position by
        one. Nothing raises when that goes wrong — the passage simply starts a
        character early — which is why this composition needs a test of its own
        rather than a code review.
        """
        blocks = [["\U0001f52c Lab notes"], ["The assay ran for six hours."], ["Inconclusive."]]
        response, _all_text, _range = _build_block_document_response(
            source_id="src_emoji",
            title="Lab notes",
            blocks=blocks,
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response, is_reusable=True)

        start = utf16_len("\U0001f52c Lab notes")
        assert start == 12  # 2 units for the emoji, 10 for " Lab notes"
        assert len("\U0001f52c Lab notes") == 11  # ...and 11 Python characters

        reference = ChatReference(
            source_id="src_emoji",
            cited_text="The assay ran for six hours.",
            start_char=start,
            end_char=start + utf16_len("The assay ran for six hours."),
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_emoji", reference=reference, context_chars=0
            )
            fulltext = await client.sources.get_fulltext("nb_emoji", "src_emoji")

        assert passage == "The assay ran for six hours."
        # The same range read as Python code points is off by the emoji's
        # extra unit, and returns text that looks almost right.
        assert fulltext.document.text[start : start + 28] == "he assay ran for six hours.I"

    @pytest.mark.asyncio
    async def test_an_unusable_range_with_no_cited_text_raises_with_both_readings(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """Nothing left to try: the error says which range and which document.

        The fetch is unavoidable here — a range's fit can only be judged
        against the document it claims to index — so the short-circuit does not
        apply, and the message carries the two numbers a caller needs to tell
        "re-indexed source" from "citation this client mis-decoded".
        """
        response, _all_text, _range = _build_block_document_response(
            source_id="src_nofit",
            title="Re-indexed",
            blocks=[["A short document."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        reference = ChatReference(
            source_id="src_nofit", cited_text=None, start_char=900, end_char=1000
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(
                ChatResponseParseError, match=r"\(900, 1000\).*extent 17"
            ) as excinfo:
                await resolve_chat_reference_passage(
                    client, notebook_id="nb_nofit", reference=reference
                )

        assert "Could not locate" in str(excinfo.value)
