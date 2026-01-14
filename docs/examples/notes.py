"""Example: Create and manage notes and mind maps.

This example demonstrates:
1. Creating and updating notes
2. Listing and searching notes
3. Generating mind maps from sources
4. Working with study materials (reports, quizzes)

Notes are user-created content, distinct from AI-generated artifacts.
They persist in your notebook and can be exported.

Prerequisites:
    - Authentication configured via `notebooklm auth` CLI command
    - Valid Google account with NotebookLM access
"""

import asyncio
import json

from notebooklm import NotebookLMClient, ReportFormat


async def main():
    """Demonstrate notes and mind map functionality."""

    async with await NotebookLMClient.from_storage() as client:
        # Create a notebook for our examples
        print("Creating notebook...")
        notebook = await client.notebooks.create("Study Notes Demo")
        print(f"Created notebook: {notebook.id}")

        # Add a source for AI features to work with
        print("\nAdding source...")
        source = await client.sources.add_url(
            notebook.id,
            "https://en.wikipedia.org/wiki/Data_structure",
        )
        print(f"Added: {source.title}")

        # Wait for source processing
        await asyncio.sleep(3)

        # =====================================================================
        # Creating Notes
        # =====================================================================

        print("\n--- Creating Notes ---")

        # Create a new note with title and content
        note1 = await client.notes.create(
            notebook.id,
            title="Key Concepts",
            content="# Data Structures Overview\n\n"
            "- Arrays: Sequential memory storage\n"
            "- Linked Lists: Node-based storage\n"
            "- Trees: Hierarchical organization\n"
            "- Hash Tables: Key-value mapping",
        )
        print(f"Created note: {note1.title} (ID: {note1.id})")

        # Create another note
        note2 = await client.notes.create(
            notebook.id,
            title="Study Questions",
            content="1. What is time complexity?\n"
            "2. When to use arrays vs linked lists?\n"
            "3. How do hash collisions work?",
        )
        print(f"Created note: {note2.title} (ID: {note2.id})")

        # =====================================================================
        # Updating Notes
        # =====================================================================

        print("\n--- Updating Notes ---")

        # Update an existing note
        await client.notes.update(
            notebook.id,
            note1.id,
            content="# Data Structures Overview (Updated)\n\n"
            "## Linear Structures\n"
            "- Arrays: O(1) access, O(n) insertion\n"
            "- Linked Lists: O(n) access, O(1) insertion\n\n"
            "## Non-Linear Structures\n"
            "- Trees: Hierarchical, O(log n) search\n"
            "- Graphs: Network relationships",
            title="Key Concepts (Revised)",
        )
        print(f"Updated note: {note1.id}")

        # =====================================================================
        # Listing Notes
        # =====================================================================

        print("\n--- Listing Notes ---")

        notes = await client.notes.list(notebook.id)
        print(f"Found {len(notes)} notes:")
        for note in notes:
            preview = note.content[:50].replace("\n", " ") if note.content else ""
            print(f"  - {note.title}: {preview}...")

        # =====================================================================
        # Getting a Specific Note
        # =====================================================================

        print("\n--- Getting Specific Note ---")

        retrieved = await client.notes.get(notebook.id, note1.id)
        if retrieved:
            print(f"Title: {retrieved.title}")
            print(f"Content:\n{retrieved.content[:200]}...")

        # =====================================================================
        # Mind Maps
        # =====================================================================

        print("\n--- Generating Mind Map ---")

        # Generate an interactive mind map from sources
        # This creates a visual representation of concepts
        try:
            mind_map_result = await client.artifacts.generate_mind_map(notebook.id)

            if mind_map_result.get("mind_map"):
                print("Mind map generated successfully!")

                # The mind map is returned as JSON data
                mind_map_data = mind_map_result["mind_map"]

                # If it's a string, parse it
                if isinstance(mind_map_data, str):
                    try:
                        mind_map_data = json.loads(mind_map_data)
                    except json.JSONDecodeError:
                        pass

                # Display mind map structure
                if isinstance(mind_map_data, dict):
                    print(f"Root topic: {mind_map_data.get('label', 'N/A')}")
                    children = mind_map_data.get("children", [])
                    print(f"Main branches: {len(children)}")
                    for child in children[:5]:
                        if isinstance(child, dict):
                            print(f"  - {child.get('label', 'N/A')}")

                if mind_map_result.get("note_id"):
                    print(f"Mind map stored as note: {mind_map_result['note_id']}")
            else:
                print("Mind map generation returned empty result")
        except Exception as e:
            print(f"Mind map generation error: {e}")

        # =====================================================================
        # Listing Mind Maps
        # =====================================================================

        print("\n--- Listing Mind Maps ---")

        mind_maps = await client.notes.list_mind_maps(notebook.id)
        print(f"Found {len(mind_maps)} mind maps")
        for mm in mind_maps:
            mm_id = mm[0] if isinstance(mm, list) and mm else "Unknown"
            print(f"  - Mind map ID: {mm_id}")

        # =====================================================================
        # Study Materials (Reports)
        # =====================================================================

        print("\n--- Generating Study Materials ---")

        # Generate a study guide
        print("Generating study guide...")
        study_gen = await client.artifacts.generate_study_guide(notebook.id)
        print(f"Study guide generation started: {study_gen.task_id}")

        # Generate a briefing document
        print("Generating briefing doc...")
        briefing_gen = await client.artifacts.generate_report(
            notebook.id,
            report_format=ReportFormat.BRIEFING_DOC,
        )
        print(f"Briefing doc generation started: {briefing_gen.task_id}")

        # Wait briefly and check status
        await asyncio.sleep(5)

        # List all reports
        reports = await client.artifacts.list_reports(notebook.id)
        print(f"\nReports in notebook: {len(reports)}")
        for report in reports:
            status = "Ready" if report.is_completed else "Processing"
            print(f"  - {report.title} ({status})")

        # =====================================================================
        # Quizzes and Flashcards
        # =====================================================================

        print("\n--- Generating Quiz ---")

        from notebooklm import QuizDifficulty, QuizQuantity

        quiz_gen = await client.artifacts.generate_quiz(
            notebook.id,
            quantity=QuizQuantity.STANDARD,
            difficulty=QuizDifficulty.MEDIUM,
        )
        print(f"Quiz generation started: {quiz_gen.task_id}")

        # Generate flashcards
        print("Generating flashcards...")
        flashcard_gen = await client.artifacts.generate_flashcards(
            notebook.id,
            quantity=QuizQuantity.FEWER,
            difficulty=QuizDifficulty.EASY,
        )
        print(f"Flashcard generation started: {flashcard_gen.task_id}")

        # =====================================================================
        # Deleting Notes
        # =====================================================================

        print("\n--- Deleting Note ---")

        # Delete a note
        success = await client.notes.delete(notebook.id, note2.id)
        print(f"Deleted note {note2.id}: {success}")

        # Verify deletion
        remaining = await client.notes.list(notebook.id)
        print(f"Remaining notes: {len(remaining)}")

        # =====================================================================
        # Summary
        # =====================================================================

        print("\n--- Final Summary ---")

        # List all artifacts
        all_artifacts = await client.artifacts.list(notebook.id)
        print(f"Total artifacts: {len(all_artifacts)}")

        # Categorize by type
        from notebooklm.rpc import StudioContentType

        type_counts = {}
        for art in all_artifacts:
            type_name = StudioContentType(art.artifact_type).name
            type_counts[type_name] = type_counts.get(type_name, 0) + 1

        for type_name, count in type_counts.items():
            print(f"  {type_name}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
