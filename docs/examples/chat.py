"""Example: Chat with a notebook and manage conversations.

This example demonstrates:
1. Asking questions about notebook content
2. Follow-up questions in a conversation
3. Retrieving conversation history
4. Configuring chat behavior (response length, custom personas)

Prerequisites:
    - Authentication configured via `notebooklm auth` CLI command
    - Valid Google account with NotebookLM access
"""

import asyncio

from notebooklm import ChatGoal, ChatMode, ChatResponseLength, NotebookLMClient


async def main():
    """Demonstrate chat and conversation features."""

    async with await NotebookLMClient.from_storage() as client:
        # Create a notebook with some content
        print("Setting up notebook with sources...")
        notebook = await client.notebooks.create("Python Learning")

        # Add a source for context
        source = await client.sources.add_url(
            notebook.id,
            "https://en.wikipedia.org/wiki/Python_(programming_language)",
        )
        print(f"Added source: {source.title}")

        # Give NotebookLM a moment to process the source
        print("Waiting for source processing...")
        await asyncio.sleep(3)

        # =====================================================================
        # Basic Question/Answer
        # =====================================================================

        print("\n--- Basic Q&A ---")

        # Ask a question about the notebook's content
        result = await client.chat.ask(
            notebook.id,
            "What are the main features of Python?",
        )

        print("Question: What are the main features of Python?")
        print(f"Answer: {result.answer[:500]}...")
        print(f"Conversation ID: {result.conversation_id}")
        print(f"Turn number: {result.turn_number}")

        # =====================================================================
        # Follow-up Questions (Conversation Threading)
        # =====================================================================

        print("\n--- Follow-up Questions ---")

        # Use the same conversation_id for follow-up questions
        # This maintains context from previous exchanges
        followup = await client.chat.ask(
            notebook.id,
            "How does it compare to other programming languages?",
            conversation_id=result.conversation_id,  # Continue the conversation
        )

        print("Follow-up: How does it compare to other programming languages?")
        print(f"Answer: {followup.answer[:500]}...")
        print(f"Is follow-up: {followup.is_follow_up}")
        print(f"Turn number: {followup.turn_number}")

        # Another follow-up
        followup2 = await client.chat.ask(
            notebook.id,
            "What about for data science specifically?",
            conversation_id=result.conversation_id,
        )

        print("\nFollow-up 2: What about for data science specifically?")
        print(f"Answer: {followup2.answer[:400]}...")

        # =====================================================================
        # Conversation History
        # =====================================================================

        print("\n--- Conversation History ---")

        # Get locally cached conversation turns
        turns = client.chat.get_cached_turns(result.conversation_id)
        print(f"Cached turns in this conversation: {len(turns)}")
        for turn in turns:
            print(f"  Turn {turn.turn_number}:")
            print(f"    Q: {turn.query[:50]}...")
            print(f"    A: {turn.answer[:50]}...")

        # Get conversation history from the API (all conversations)
        try:
            history = await client.chat.get_history(notebook.id, limit=10)
            print(f"\nAPI conversation history: {type(history)}")
        except Exception as e:
            print(f"Note: History retrieval returned: {e}")

        # =====================================================================
        # Configuring Chat Behavior
        # =====================================================================

        print("\n--- Chat Configuration ---")

        # Method 1: Use predefined chat modes
        # Available modes: DEFAULT, LEARNING_GUIDE, CONCISE, DETAILED
        print("Setting chat mode to LEARNING_GUIDE...")
        await client.chat.set_mode(notebook.id, ChatMode.LEARNING_GUIDE)

        # Ask a question with the new mode
        learning_result = await client.chat.ask(
            notebook.id,
            "Explain decorators in Python",
        )
        print(f"Learning mode answer: {learning_result.answer[:400]}...")

        # Method 2: Fine-grained configuration
        # ChatGoal: DEFAULT, CUSTOM, LEARNING_GUIDE
        # ChatResponseLength: SHORTER, DEFAULT, LONGER
        print("\nSetting custom chat configuration...")
        await client.chat.configure(
            notebook.id,
            goal=ChatGoal.DEFAULT,
            response_length=ChatResponseLength.SHORTER,
        )

        concise_result = await client.chat.ask(
            notebook.id,
            "What is Python used for?",
        )
        print(f"Concise answer: {concise_result.answer[:300]}...")

        # Method 3: Custom persona with specific instructions
        print("\nSetting custom persona...")
        await client.chat.configure(
            notebook.id,
            goal=ChatGoal.CUSTOM,
            response_length=ChatResponseLength.DEFAULT,
            custom_prompt="You are an experienced Python developer. "
            "Explain concepts with practical code examples. "
            "Focus on best practices and real-world usage.",
        )

        custom_result = await client.chat.ask(
            notebook.id,
            "How should I handle errors in Python?",
        )
        print(f"Custom persona answer: {custom_result.answer[:500]}...")

        # =====================================================================
        # Source-Specific Questions
        # =====================================================================

        print("\n--- Source-Specific Questions ---")

        # Get source IDs to target specific sources
        sources = await client.sources.list(notebook.id)
        if sources:
            source_ids = [sources[0].id]

            # Ask about specific sources only
            targeted_result = await client.chat.ask(
                notebook.id,
                "Summarize the key points from this source",
                source_ids=source_ids,  # Only use these sources for context
            )
            print(f"Targeted answer: {targeted_result.answer[:400]}...")

        # =====================================================================
        # Cleanup
        # =====================================================================

        # Clear conversation cache (optional)
        client.chat.clear_cache(result.conversation_id)
        print("\nConversation cache cleared")


if __name__ == "__main__":
    asyncio.run(main())
