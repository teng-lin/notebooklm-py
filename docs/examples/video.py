"""Example: Generate a Video Overview from notebook sources.

This example demonstrates:
1. Setting up a notebook with sources
2. Generating a video overview with style options
3. Checking artifact status
4. Downloading the completed video

Video Overviews are animated explainer videos that summarize your
notebook content with AI-generated narration and visuals.

Prerequisites:
    - Authentication configured via `notebooklm auth` CLI command
    - Valid Google account with NotebookLM access
"""

import asyncio

from notebooklm import NotebookLMClient, VideoFormat, VideoStyle


async def main():
    """Generate a video overview from notebook sources."""

    async with await NotebookLMClient.from_storage() as client:
        # Step 1: Create a notebook with content
        print("Creating notebook...")
        notebook = await client.notebooks.create("Video Demo Notebook")
        print(f"Created notebook: {notebook.id}")

        # Add sources for video content
        print("\nAdding sources...")
        urls = [
            "https://en.wikipedia.org/wiki/Quantum_computing",
        ]

        for url in urls:
            source = await client.sources.add_url(notebook.id, url)
            print(f"  Added: {source.title or url}")

        # Wait for source processing
        print("\nWaiting for source processing...")
        await asyncio.sleep(5)

        # Step 2: Generate the video overview
        # Video generation options:
        #
        # video_format:
        #   - EXPLAINER: Full explanatory video (default)
        #   - BRIEF: Shorter summary video
        #
        # video_style:
        #   - AUTO_SELECT: Let AI choose the best style (default)
        #   - CLASSIC: Traditional presentation style
        #   - WHITEBOARD: Whiteboard animation style
        #   - ABSTRACT: Abstract visual style
        #   - CORPORATE: Professional corporate style
        #   - DYNAMIC: Dynamic, energetic style

        print("\nStarting video generation...")
        print("Video generation typically takes 3-8 minutes")

        generation = await client.artifacts.generate_video(
            notebook.id,
            video_format=VideoFormat.EXPLAINER,
            video_style=VideoStyle.AUTO_SELECT,
            language="en",
            instructions="Create an engaging overview suitable for general audiences",
        )

        print(f"Generation started: {generation.task_id}")
        print(f"Initial status: {generation.status}")

        # Step 3: Wait for completion with status updates
        print("\nWaiting for video generation...")

        try:
            final_status = await client.artifacts.wait_for_completion(
                notebook.id,
                generation.task_id,
                initial_interval=10.0,  # Check every 10 seconds initially
                max_interval=30.0,  # Max 30 seconds between checks
                timeout=900.0,  # 15 minute timeout for videos
            )

            if final_status.is_complete:
                print("\nVideo generation complete!")

                # Step 4: Download the video
                output_path = "quantum_video.mp4"
                print(f"Downloading video to {output_path}...")

                await client.artifacts.download_video(
                    notebook.id,
                    output_path,
                    artifact_id=generation.task_id,
                )
                print(f"Video downloaded: {output_path}")

            elif final_status.is_failed:
                print(f"\nGeneration failed: {final_status.error}")

        except TimeoutError:
            print("\nVideo generation timed out")
            print("Check NotebookLM web UI for completion")

        # =====================================================================
        # Alternative: Check existing artifacts
        # =====================================================================

        print("\n--- Listing All Video Artifacts ---")

        # List all video artifacts in the notebook
        videos = await client.artifacts.list_video(notebook.id)

        for video in videos:
            status = "Ready" if video.is_completed else "Processing"
            print(f"\n  Title: {video.title}")
            print(f"  ID: {video.id}")
            print(f"  Status: {status}")
            if video.created_at:
                print(f"  Created: {video.created_at}")

        # =====================================================================
        # Manual status polling example
        # =====================================================================

        print("\n--- Manual Status Polling ---")

        if generation.task_id:
            # You can manually poll status without wait_for_completion
            status = await client.artifacts.poll_status(
                notebook.id,
                generation.task_id,
            )
            print(f"Current status: {status.status}")
            print(f"Is complete: {status.is_complete}")
            print(f"Is in progress: {status.is_in_progress}")

        # =====================================================================
        # Download existing video
        # =====================================================================

        # If you have an existing completed video, download it directly
        if videos and videos[0].is_completed:
            print("\n--- Downloading Existing Video ---")
            existing_path = "existing_video.mp4"
            await client.artifacts.download_video(
                notebook.id,
                existing_path,
                artifact_id=videos[0].id,
            )
            print(f"Downloaded: {existing_path}")


if __name__ == "__main__":
    asyncio.run(main())
