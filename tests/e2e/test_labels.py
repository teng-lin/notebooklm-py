"""E2E tests for LabelsAPI (``client.labels``).

These exercise the real source-label lifecycle against a live notebook. Like
the other e2e modules they are collected only under ``-m e2e`` with valid auth
(``requires_auth`` skips otherwise) and use the ``temp_notebook`` fixture so the
whole notebook — and every label created on it — is torn down after the test.

Cleanup discipline: each test that creates a label deletes it in a ``finally``
block (``labels.delete`` is idempotent, so a double-delete from the
notebook-level teardown is harmless), mirroring the create/delete symmetry the
sharing/source e2e tests follow.
"""

import pytest

from notebooklm import Label, LabelNotFoundError, Source

from .conftest import requires_auth


@requires_auth
class TestLabelLifecycle:
    """Create / read / mutate / delete a single label end-to-end."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_create_list_get(self, client, temp_notebook):
        """``create`` returns a Label; ``list``/``get`` find it; misses behave."""
        label = await client.labels.create(temp_notebook.id, "Papers", "\U0001f4c4")
        try:
            assert isinstance(label, Label)
            assert label.id
            assert label.name == "Papers"
            assert label.emoji == "\U0001f4c4"

            # list() includes the new label.
            labels = await client.labels.list(temp_notebook.id)
            assert isinstance(labels, list)
            assert all(isinstance(item, Label) for item in labels)
            assert label.id in {item.id for item in labels}

            # get() returns it.
            fetched = await client.labels.get(temp_notebook.id, label.id)
            assert isinstance(fetched, Label)
            assert fetched.id == label.id
            assert fetched.name == "Papers"

            # get_or_none() of a missing id is None; get() raises.
            assert await client.labels.get_or_none(temp_notebook.id, "missing") is None
            with pytest.raises(LabelNotFoundError):
                await client.labels.get(temp_notebook.id, "missing")
        finally:
            await client.labels.delete(temp_notebook.id, label.id)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_rename_preserves_emoji(self, client, temp_notebook):
        """``rename`` changes the name but keeps the existing emoji."""
        label = await client.labels.create(temp_notebook.id, "Drafts", "\U0001f4dd")
        try:
            renamed = await client.labels.rename(temp_notebook.id, label.id, "Final")
            assert isinstance(renamed, Label)
            assert renamed.name == "Final"
            # rename must not clobber the emoji (carried over from the preflight).
            assert renamed.emoji == "\U0001f4dd"
        finally:
            await client.labels.delete(temp_notebook.id, label.id)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_set_emoji(self, client, temp_notebook):
        """``set_emoji`` updates the emoji and preserves the name."""
        label = await client.labels.create(temp_notebook.id, "Ideas", "\U0001f4a1")
        try:
            updated = await client.labels.set_emoji(temp_notebook.id, label.id, "\U0001f680")
            assert isinstance(updated, Label)
            assert updated.emoji == "\U0001f680"
            assert updated.name == "Ideas"
        finally:
            await client.labels.delete(temp_notebook.id, label.id)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_update_name_and_emoji(self, client, temp_notebook):
        """``update`` sets both name and emoji in one call."""
        label = await client.labels.create(temp_notebook.id, "Old", "\U0001f4c1")
        try:
            updated = await client.labels.update(
                temp_notebook.id, label.id, name="New", emoji="\U0001f4c2"
            )
            assert isinstance(updated, Label)
            assert updated.name == "New"
            assert updated.emoji == "\U0001f4c2"
        finally:
            await client.labels.delete(temp_notebook.id, label.id)


@requires_auth
class TestLabelMembership:
    """Add sources to a label and expand the label back to Source objects."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_add_sources_and_expand(self, client, temp_notebook):
        """``add_sources`` then ``sources`` returns the added Source."""
        # temp_notebook fixture seeds one text source; grab it.
        sources = await client.sources.list(temp_notebook.id)
        if not sources:
            pytest.skip("temp_notebook has no sources to label")
        source = sources[0]
        assert isinstance(source, Source)

        label = await client.labels.create(temp_notebook.id, "Tagged", "\U0001f3f7")
        try:
            updated = await client.labels.add_sources(temp_notebook.id, label.id, [source.id])
            assert isinstance(updated, Label)
            assert source.id in updated.source_ids

            # sources() expands membership back to Source objects.
            members = await client.labels.sources(temp_notebook.id, label.id)
            assert isinstance(members, list)
            assert all(isinstance(item, Source) for item in members)
            assert source.id in {item.id for item in members}
        finally:
            await client.labels.delete(temp_notebook.id, label.id)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_add_sources_adds_all_not_just_first(self, client, temp_notebook):
        """Regression (wire truncation): ``add_sources([a, b])`` must add BOTH.

        The server keeps only the first id per ``le8sX`` group, so the API loops
        one call per id. Before the fix only the first source was assigned.
        """
        sources = await client.sources.list(temp_notebook.id)
        if len(sources) < 2:
            await client.sources.add_text(
                temp_notebook.id,
                title="Second Source",
                content="A second test source so multi-source labeling can be exercised.",
            )
            sources = await client.sources.list(temp_notebook.id)
        if len(sources) < 2:
            pytest.skip("could not obtain two sources for the multi-add regression")
        first, second = sources[0].id, sources[1].id

        label = await client.labels.create(temp_notebook.id, "Multi", "")
        try:
            await client.labels.add_sources(temp_notebook.id, label.id, [first, second])
            members = {item.id for item in await client.labels.sources(temp_notebook.id, label.id)}
            assert {first, second} <= members, (
                f"expected both {first} and {second} in {members} (truncation regression)"
            )
        finally:
            await client.labels.delete(temp_notebook.id, label.id)

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_remove_sources_unassigns_without_deleting(self, client, temp_notebook):
        """``remove_sources`` un-assigns a source from a label but leaves the
        source in the notebook."""
        sources = await client.sources.list(temp_notebook.id)
        if not sources:
            pytest.skip("temp_notebook has no sources to label")
        source = sources[0]

        label = await client.labels.create(temp_notebook.id, "Removable", "")
        try:
            await client.labels.add_sources(temp_notebook.id, label.id, [source.id])
            assert source.id in (await client.labels.get(temp_notebook.id, label.id)).source_ids

            await client.labels.remove_sources(temp_notebook.id, label.id, [source.id])
            assert source.id not in (await client.labels.get(temp_notebook.id, label.id)).source_ids
            # Un-assign, not delete: the source still exists in the notebook.
            assert source.id in {s.id for s in await client.sources.list(temp_notebook.id)}
        finally:
            await client.labels.delete(temp_notebook.id, label.id)


@requires_auth
class TestLabelDelete:
    """Delete semantics: removal from the list + idempotent re-delete."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_delete_removes_label(self, client, temp_notebook):
        """``delete`` removes the label from ``list``."""
        label = await client.labels.create(temp_notebook.id, "Temporary", "\U0001f5d1")

        result = await client.labels.delete(temp_notebook.id, label.id)
        assert result is None

        labels = await client.labels.list(temp_notebook.id)
        assert label.id not in {item.id for item in labels}

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_delete_absent_is_noop(self, client, temp_notebook):
        """Deleting an already-absent label id is a no-op (no raise)."""
        # Idempotent: deleting a never-existing id returns None without raising.
        result = await client.labels.delete(temp_notebook.id, "nonexistent_label_id")
        assert result is None


@requires_auth
class TestLabelGenerate:
    """AI auto-labeling (``generate``), guarded to the safe ``unlabeled`` scope."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_generate_unlabeled(self, client, temp_notebook):
        """``generate(scope='unlabeled')`` returns a list[Label].

        Guarded: only runs when the notebook actually has sources to group, and
        deliberately uses ``scope='unlabeled'`` (the safe, non-destructive
        default) — ``scope='all'`` WIPES every existing label and is never
        exercised here.
        """
        sources = await client.sources.list(temp_notebook.id)
        if not sources:
            pytest.skip("temp_notebook has no sources to auto-label")

        labels = await client.labels.generate(temp_notebook.id, scope="unlabeled")
        assert isinstance(labels, list)
        assert all(isinstance(item, Label) for item in labels)

        # Best-effort cleanup of any labels the AI created (idempotent delete).
        if labels:
            await client.labels.delete(temp_notebook.id, [item.id for item in labels])


@requires_auth
class TestLabelsAPIAttributes:
    """Tests for LabelsAPI availability on the client."""

    @pytest.mark.asyncio
    @pytest.mark.e2e
    async def test_client_has_labels_api(self, client):
        """The client exposes ``labels`` with the full method surface."""
        assert hasattr(client, "labels")
        for method in (
            "list",
            "get",
            "get_or_none",
            "sources",
            "generate",
            "create",
            "update",
            "rename",
            "set_emoji",
            "add_sources",
            "remove_sources",
            "delete",
        ):
            assert hasattr(client.labels, method), f"labels.{method} missing"
