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
    blocks: list[list[str] | int],
    build_rpc_response,
) -> tuple[bytes, str, tuple[int, int]]:
    """A GET_SOURCE response whose ``result[3][0]`` is a real document ``Body``.

    The bare-strings builder above decodes to *no* blocks — so every test using
    it exercises only the search fallback. This one emits the shape the wire
    actually sends (``StructuralElement`` rows, each holding its text runs with
    their offsets), which is what makes offset resolution reachable at all.

    Each inner list is one block's **runs**: the sub-paragraph fragments the
    backend splits a paragraph into, and the reason ``content`` renders a
    paragraph as several lines. An ``int`` in place of a list is a block that
    occupies that many positions and decodes no text — an image or a rule.

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
        block_start = cursor
        if isinstance(runs, int):
            # A block that occupies positions and decodes no text — what an
            # image or a horizontal rule looks like once parsed.
            cursor += runs
            elements.append([block_start, cursor, [[]]])
            continue
        spans = []
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
        # ``cited_text`` omits the positions that decode no text, exactly as
        # ``_web.rows.chat_stream.extract_text_passages`` does.
        "".join(run for runs in blocks if not isinstance(runs, int) for run in runs),
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
        """The knob keeps working on the path that no longer searches.

        Every block here is split into two runs, which is what makes the
        assertions witness the offset path rather than agree with the search:
        the flat ``content`` the fallback windows puts each *run* on its own
        line, so a passage it returned would carry a newline mid-sentence.
        """
        blocks = [["Before ", "block."], ["The cited ", "block."], ["After ", "block."]]
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
            fulltext = await client.sources.get_fulltext("nb_ctx", "src_ctx")

        assert narrow == "The cited block."
        assert wide == "Before block.\nThe cited block.\nAfter block."
        # What the search fallback would have had to work with, for contrast.
        assert fulltext.content == "Before \nblock.\nThe cited \nblock.\nAfter \nblock."

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

        The source quotes itself, so the assertions witness the *offset* path:
        a search for the cited text lands on the first occurrence, whose
        neighbourhood is the heading, while the range names the second.
        """
        cited = "The assay ran for six hours."
        blocks = [
            ["\U0001f52c Lab notes"],
            [cited],
            ["Interlude."],
            [cited],
            ["Final remarks."],
        ]
        response, _all_text, _range = _build_block_document_response(
            source_id="src_emoji",
            title="Lab notes",
            blocks=blocks,
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response, is_reusable=True)

        assert utf16_len("\U0001f52c Lab notes") == 12  # 2 units for the emoji, 10 for the rest
        assert len("\U0001f52c Lab notes") == 11  # ...and 11 Python characters
        second_start = sum(utf16_len(runs[0]) for runs in blocks[:3])
        assert second_start == 50

        reference = ChatReference(
            source_id="src_emoji",
            cited_text=cited,
            start_char=second_start,
            end_char=second_start + utf16_len(cited),
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_emoji", reference=reference, context_chars=0
            )
            windowed = await resolve_chat_reference_passage(
                client, notebook_id="nb_emoji", reference=reference, context_chars=14
            )
            fulltext = await client.sources.get_fulltext("nb_emoji", "src_emoji")

        assert passage == cited
        # The cited occurrence, not the first one a search would return.
        assert "Final remarks." in windowed
        assert "Lab notes" not in windowed
        # The same range read as Python code points is off by the emoji's
        # extra unit, and returns text that looks almost right.
        assert fulltext.document.text[second_start : second_start + utf16_len(cited)] == (
            "he assay ran for six hours.F"
        )

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

    @pytest.mark.asyncio
    async def test_a_stale_range_that_still_fits_is_caught_by_the_cited_text(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """The failure ``extent`` cannot see: a source that was re-indexed *longer*.

        Checking the range against the document's extent only notices a source
        that shrank. Grow one — a preface added ahead of the cited paragraph —
        and the stale range still fits, still resolves, and resolves to the
        wrong text. The search this path replaced detected that for free by
        failing to match, so the offset path cross-checks ``cited_text`` before
        trusting the range, and stands down when the two disagree.
        """
        cited_text = "The cited sentence sits here."
        response, _all_text, _range = _build_block_document_response(
            source_id="src_grown",
            title="Grown since the answer",
            blocks=[["A preface added since the answer."], [cited_text], ["Tail matter."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        # The offsets the citation was emitted with: block 0 of the *old*
        # document, which is now the preface.
        stale = ChatReference(
            source_id="src_grown",
            cited_text=cited_text,
            start_char=0,
            end_char=utf16_len(cited_text),
        )
        assert stale.end_char < utf16_len("A preface added since the answer.") + utf16_len(
            cited_text
        )  # the stale range fits the grown document, so extent cannot reject it

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_grown", reference=stale, context_chars=0
            )

        # The search found the citation where it actually is; the stale range
        # would have returned the preface.
        assert cited_text in passage
        assert "A preface added" not in passage

    @pytest.mark.asyncio
    async def test_a_range_covering_only_an_image_does_not_borrow_its_neighbours_text(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """A citation with no text to show must not be handed the next paragraph.

        The range fits the document, so ``extent`` passes it; what stands it
        down is that the citation's *own* range renders nothing. Testing the
        widened window instead would let 200 units of neighbouring prose stand
        in for a passage the citation does not have — and the caller would have
        no way to tell.
        """
        response, _all_text, _range = _build_block_document_response(
            source_id="src_image",
            title="With an image",
            blocks=[["First paragraph."], 5, ["Third paragraph."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        image_start = utf16_len("First paragraph.")
        reference = ChatReference(
            source_id="src_image",
            cited_text=None,
            start_char=image_start,
            end_char=image_start + 5,
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ChatResponseParseError, match="did not resolve to text"):
                await resolve_chat_reference_passage(
                    client, notebook_id="nb_image", reference=reference, context_chars=200
                )

    @pytest.mark.asyncio
    async def test_the_error_names_only_what_was_actually_attempted(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """A message that blames a search that never ran sends the reader wrong.

        A reference with ``cited_text`` and no range, and one with a range and
        no ``cited_text``, fail for different reasons and must say so — the
        remedies differ (re-ask vs re-fetch the source).
        """
        response, _all_text, _range = _build_block_document_response(
            source_id="src_msg",
            title="Message",
            blocks=[["Some quite unrelated prose."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response, is_reusable=True)

        rangeless = ChatReference(source_id="src_msg", cited_text="nowhere in this source")
        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ChatResponseParseError) as rangeless_error:
                await resolve_chat_reference_passage(client, "nb_msg", rangeless)

            textless = ChatReference(
                source_id="src_msg", cited_text=None, start_char=900, end_char=1000
            )
            with pytest.raises(ChatResponseParseError) as textless_error:
                await resolve_chat_reference_passage(client, "nb_msg", textless)

        assert "carries no character range" in str(rangeless_error.value)
        assert "cited_text was not found" in str(rangeless_error.value)
        # ...and the converse: no search ran, so none is reported as failed.
        assert "cited_text was not found" not in str(textless_error.value)
        assert "(900, 1000)" in str(textless_error.value)

    @pytest.mark.asyncio
    async def test_a_negative_context_never_narrows_the_citations_own_range(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """``context_chars`` adds context or none; it does not subtract text.

        Left unclamped a negative value inverts the widening, shrinking the
        window until the citation resolves to nothing and silently drops to the
        search — a knob that quietly changes *which* path runs.
        """
        response, _all_text, _range = _build_block_document_response(
            source_id="src_neg",
            title="Negative context",
            blocks=[["Opening."], ["The cited block."], ["Closing."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        start = utf16_len("Opening.")
        reference = ChatReference(
            source_id="src_neg",
            cited_text="The cited block.",
            start_char=start,
            end_char=start + utf16_len("The cited block."),
        )

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_neg", reference=reference, context_chars=-6
            )

        assert passage == "The cited block."

    @pytest.mark.asyncio
    async def test_a_range_overrunning_the_document_is_refused_not_clamped(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """The extent check on its own terms — no ``cited_text`` to cross-check.

        A range that starts inside the document and ends past it renders
        *something* when clamped: the tail, which is not the citation. With no
        ``cited_text`` the range is the only evidence there is, so it has to be
        judged on fit alone — and only a few units of overrun are needed to
        prove the source is not the one the citation was emitted against.
        """
        blocks = [["Alpha block."], ["Beta block."]]
        response, _all_text, _range = _build_block_document_response(
            source_id="src_overrun",
            title="Overrun",
            blocks=blocks,
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        extent = sum(utf16_len(runs[0]) for runs in blocks)
        reference = ChatReference(
            source_id="src_overrun",
            cited_text=None,
            start_char=utf16_len("Alpha block."),
            end_char=extent + 5,
        )

        async with NotebookLMClient(auth_tokens) as client:
            with pytest.raises(ChatResponseParseError, match="did not resolve to text"):
                await resolve_chat_reference_passage(
                    client, notebook_id="nb_overrun", reference=reference, context_chars=0
                )

    @pytest.mark.asyncio
    async def test_the_cross_check_is_anchored_at_the_start_of_the_range(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """Agreeing *somewhere* in the range is not agreeing.

        A re-index that inserts text at the head of a still-fitting range
        leaves the old quote sitting further inside it. A containment test
        accepts that — and returns a passage opening with the inserted text and
        stopping mid-quote. Both readings start at ``start_char``, so agreement
        has to be checked there.
        """
        cited_text = "The cited sentence sits here."
        inserted = "Inserted lead-in text."
        response, _all_text, _range = _build_block_document_response(
            source_id="src_inserted",
            title="Inserted ahead of the citation",
            blocks=[[inserted], [cited_text], ["Tail matter."]],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        # The stale range still spans the same *width*, and now covers the
        # insertion plus all but the tail of the quote — so the quote is inside
        # it, just not at its start.
        stale = ChatReference(
            source_id="src_inserted",
            cited_text=cited_text,
            start_char=0,
            end_char=utf16_len(inserted) + utf16_len(cited_text),
        )
        assert stale.end_char <= utf16_len(inserted + cited_text + "Tail matter.")

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_inserted", reference=stale, context_chars=0
            )

        # Stood down to the search, which lands on the quote itself.
        assert passage == cited_text
        assert inserted not in passage

    @pytest.mark.asyncio
    async def test_a_range_made_negative_after_construction_is_not_clamped_into_a_passage(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
    ) -> None:
        """``ChatReference`` is mutable, and its validation does not re-run.

        A negative ``start_char`` assigned after construction reaches the
        resolver, where the renderer would clamp it to 0 and hand back the head
        of the source as though it were the citation. The range check rejects it
        instead, so an offsets-only reference stands down rather than resolving
        to the wrong text.
        """
        reference = ChatReference(source_id="src_mutated", cited_text=None)
        # Post-construction assignment: __post_init__ validation does not re-run.
        reference.start_char = -5
        reference.end_char = 5

        async with NotebookLMClient(auth_tokens) as client:
            # The message names the range as well as the text, so this asserts
            # the *range* was judged unusable rather than merely the text: the
            # short-circuit fires only when both are.
            with pytest.raises(
                ChatResponseParseError, match="no cited_text and no character range"
            ):
                await resolve_chat_reference_passage(
                    client, notebook_id="nb_mutated", reference=reference
                )

        # ...and no request was issued, which is the second half of the same
        # statement: without the negative-range check the reference reads as
        # resolvable and the resolver fetches.
        assert not httpx_mock.get_requests()

    @pytest.mark.asyncio
    async def test_a_negative_context_is_clamped_on_the_search_path_too(
        self,
        auth_tokens,
        httpx_mock: HTTPXMock,
        build_rpc_response,
    ) -> None:
        """The clamp belongs to the resolver, not to one of its two paths.

        ``find_citation_context`` subtracts the context from the match start
        and adds it to the end, so a negative value inverts its slice and
        returns ``""`` for a citation it actually found — the helper would then
        raise "could not locate" about text it had in hand.
        """
        cited_text = "a needle worth finding in this haystack"
        response = _build_fulltext_response(
            source_id="src_negsearch",
            title="Search path",
            content_chunks=["Some preamble. " + cited_text + " Some tail."],
            build_rpc_response=build_rpc_response,
        )
        httpx_mock.add_response(content=response)

        # No range at all, so the search path is the only one available.
        reference = ChatReference(source_id="src_negsearch", cited_text=cited_text)

        async with NotebookLMClient(auth_tokens) as client:
            passage = await resolve_chat_reference_passage(
                client, notebook_id="nb_negsearch", reference=reference, context_chars=-50
            )

        assert cited_text[:40] in passage
