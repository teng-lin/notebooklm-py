"""E2E UI-based test cases for NotebookLM Enterprise using Playwright.

This suite exercises the high-level browser-driven user journeys in Google Cloud
NotebookLM Enterprise (notebooklm.cloud.google.com). It validates login routing,
notebook workspace creation, source link insertion, and the generation of Audio
Overviews, Video Overviews, and Presenter Slides.

Run with:
    pytest tests/e2e/test_enterprise_ui.py -m e2e --headed
or headlessly:
    pytest tests/e2e/test_enterprise_ui.py -m e2e
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from notebooklm import NotebookLMClient
from notebooklm.auth import AuthTokens
from notebooklm.paths import get_storage_path


def get_enterprise_url() -> str:
    """Construct the fully qualified NotebookLM Enterprise URL.

    Uses environment variables for project-bound and regional enterprise URL routing:
    - NOTEBOOKLM_BASE_URL (defaults to https://notebooklm.cloud.google.com)
    - NOTEBOOKLM_REGION (required)
    - NOTEBOOKLM_PROJECT (required)
    """
    base_url = os.environ.get("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")
    region = os.environ.get("NOTEBOOKLM_REGION")
    project = os.environ.get("NOTEBOOKLM_PROJECT")

    if not region or not project:
        pytest.skip(
            "Skipping Enterprise UI tests because NOTEBOOKLM_REGION and/or "
            "NOTEBOOKLM_PROJECT environment variables are missing."
        )

    url = f"{base_url.rstrip('/')}/{region}/"
    if project:
        url += f"?project={project}"
    return url


def capture_screenshot(page: Page, step_name: str) -> Path:
    """Capture a full page screenshot and save it to the workspace scratch or /tmp directory.

    Args:
        page: The Playwright page instance.
        step_name: Short identifier for the step (e.g. '01_login').

    Returns:
        The Path to the saved screenshot.
    """
    workspace_root = Path(__file__).resolve().parent.parent.parent
    scratch_dir = workspace_root / "scratch"

    artifact_dir = (
        scratch_dir
        if scratch_dir.exists()
        else Path("/tmp") / "notebooklm_scratch"
    )

    artifact_dir.mkdir(parents=True, exist_ok=True)

    timestamp = int(time.time())
    path = artifact_dir / f"ui_test_enterprise_{step_name}_{timestamp}.png"
    try:
        page.screenshot(path=str(path), full_page=True)
        print(f"📸 Screenshot saved successfully: {path}")
    except Exception as exc:
        print(f"⚠️ Warning: Could not capture screenshot: {exc}")
    return path


def list_available_buttons(page: Page) -> list[str]:
    """Helper to audit and log all visible button texts on the page on selector failure."""
    visible_texts = []
    try:
        buttons = page.locator("button").all()
        for btn in buttons:
            if btn.is_visible():
                txt = btn.inner_text().strip()
                if txt:
                    visible_texts.append(txt)
    except Exception:
        pass
    return visible_texts


@pytest.mark.e2e
@pytest.mark.requires_playwright
class TestNotebookLMEnterpriseUI:
    """E2E UI Test Suite for NotebookLM Enterprise."""

    def test_enterprise_login_routing(self) -> None:
        """Verify regional and project-bound routing navigation and authentication check.

        This test checks that navigating to the enterprise landing page either resolves
        to the authenticated home view (showing notebooks) or, if run headfully, allows
        re-authentication before validating landing states.
        """
        headless = os.environ.get("NOTEBOOKLM_HEADLESS", "1") == "1"
        storage_path = get_storage_path()

        with sync_playwright() as p:
            print("[Login Test] Launching browser...")
            browser = p.chromium.launch(headless=headless)

            context_kwargs: dict[str, Any] = {
                "viewport": {"width": 1280, "height": 800},
            }
            if storage_path.exists() and storage_path.stat().st_size > 0:
                context_kwargs["storage_state"] = str(storage_path)
                print(f"[Login Test] Loaded storage state from {storage_path}")
            else:
                print(
                    "[Login Test] ⚠️ No storage state found. "
                    "Interactive fallback is required."
                )
                if headless:
                    pytest.skip("Skipping headless run; no valid storage_state.json is available.")

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            try:
                target_url = get_enterprise_url()
                print(f"[Login Test] Navigating to: {target_url}")
                page.goto(target_url, wait_until="commit")

                # Wait for either Google login landing or the notebook workspace dashboard
                try:
                    page.wait_for_function(
                        "() => window.location.href.includes('accounts.google.com') || "
                        "window.location.href.includes('notebooklm')",
                        timeout=15000,
                    )
                except Exception:
                    pass

                capture_screenshot(page, "01_login_navigation")

                # Handle Google SSO accounts page redirection if headful
                if "accounts.google.com" in page.url:
                    if not headless:
                        print(
                            "👉 Redirection detected. "
                            "Please authenticate manually in the open window..."
                        )
                        page.wait_for_url(re.compile(r"^https://notebooklm\.cloud\.google\.com"), timeout=180000)
                        print("👉 Successfully logged in. Persisting authenticated state.")
                        context.storage_state(path=str(storage_path))
                    else:
                        raise AssertionError(
                            "Authentication failed: redirected to Google Login in "
                            f"headless mode (URL: {page.url})"
                        )

                try:
                    page.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass

                # Validate landing. We expect to see 'NotebookLM' in the title
                # or workspace elements.
                print(f"[Login Test] Landed Page URL: {page.url}")
                print(f"[Login Test] Landed Page Title: {page.title()}")

                assert "notebooklm" in page.url or "NotebookLM" in page.title(), (
                    f"Unexpected landing page URL/title. URL: {page.url}, Title: {page.title()}"
                )
                print(
                    "[Login Test] ✅ Enterprise Login & Routing Navigation Verified successfully."
                )

            finally:
                context.close()
                browser.close()

    def test_full_notebook_studio_features(self) -> None:
        """Cohesive end-to-end user journey across all key NotebookLM Enterprise features:

        1. Create Notebook
        2. Add website source link
        3. Generate Audio Overview (Deep Dive)
        4. Generate Presenter Slides
        5. Generate Video Overview
        6. Generate Report (Briefing Doc)
        """
        headless = os.environ.get("NOTEBOOKLM_HEADLESS", "1") == "1"
        storage_path = get_storage_path()

        if not storage_path.exists() or storage_path.stat().st_size == 0:
            pytest.skip(
                "Skipping E2E Studio feature tests: "
                "no storage_state.json session profile available."
            )

        created_notebook_id = None

        with sync_playwright() as p:
            print("[Studio Test] Launching browser...")
            browser = p.chromium.launch(headless=headless)

            context = browser.new_context(
                storage_state=str(storage_path),
                viewport={"width": 1280, "height": 800},
            )
            page = context.new_page()

            try:
                # --- Step 1: Login & Navigation ---
                target_url = get_enterprise_url()
                print(f"[Studio Test] Loading landing page: {target_url}")
                page.goto(target_url, wait_until="commit")
                try:
                    page.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass
                time.sleep(3)

                # --- Step 2: Create Notebook ---
                print("\n--- [Step 2] Creating Notebook ---")
                create_btn = None
                selectors = [
                    page.get_by_role("button", name="New notebook", exact=False),
                    page.get_by_role("button", name="Create notebook", exact=False),
                    page.get_by_role("button", name="New", exact=False),
                    page.get_by_text("New notebook", exact=False),
                    page.locator("button:has-text('New')"),
                    page.locator("button:has-text('新建')"),
                    page.locator("button:has-text('创建')"),
                ]

                for sel in selectors:
                    try:
                        if sel.is_visible():
                            create_btn = sel
                            break
                    except Exception:
                        pass

                if not create_btn:
                    # Fallback text audit on visible buttons
                    buttons = page.locator("button").all()
                    for btn in buttons:
                        try:
                            if btn.is_visible():
                                txt = btn.inner_text().strip()
                                if any(k in txt for k in ["New", "Create", "新建", "创建"]):
                                    create_btn = btn
                                    print(f"Found matches via inner text search: '{txt}'")
                                    break
                        except Exception:
                            pass

                if not create_btn:
                    avail = list_available_buttons(page)
                    capture_screenshot(page, "error_create_notebook_button")
                    raise AssertionError(
                        f"Could not find 'New notebook' button. Available buttons: {avail}"
                    )

                # Try to dismiss any generic popups (like "Unable to access NotebookLM")
                try:
                    page.keyboard.press("Escape")
                    time.sleep(1)
                except Exception:
                    pass

                print("Clicking 'New notebook' button...")
                create_btn.click(force=True)

                # Wait for the transition to notebook page
                print("Waiting for workspace initialization redirection...")
                page.wait_for_url("**/notebook/*", timeout=30000)
                match = re.search(r"/notebook/([^/?#]+)", page.url)
                created_notebook_id = match.group(1) if match else None
                print(f"Successfully entered notebook workspace: {page.url}")
                print(f"Parsed created notebook ID: {created_notebook_id}")
                time.sleep(3)
                capture_screenshot(page, "02_notebook_created")

                # --- Step 3: Add Website Link Source ---
                print("\n--- [Step 3] Adding website link source ---")
                website_btn = None

                # Specific target selectors matching the "Website" span/option inside the modal
                target_selectors = [
                    page.locator(".cdk-overlay-container").get_by_text("Website", exact=True),
                    page.get_by_text("Website", exact=True),
                    page.locator(".cdk-overlay-container").get_by_text("网页", exact=True),
                    page.locator(".cdk-overlay-container").get_by_text("链接", exact=True),
                ]

                # Wait up to 3 seconds for the Website option to be visible automatically
                for sel in target_selectors:
                    try:
                        sel.first.wait_for(state="visible", timeout=3000)
                        website_btn = sel.first
                        print("Found 'Website' source selector immediately!")
                        break
                    except Exception:
                        pass

                if not website_btn:
                    # If Website option is not visible, maybe the modal is not open yet.
                    # Let's locate the 'Upload a source' or 'Add' button to trigger it.
                    print("Website selector not immediately visible. Finding modal trigger...")
                    add_source_btn = None
                    add_selectors = [
                        page.get_by_role("button", name="Upload a source", exact=False),
                        page.get_by_role("button", name="upload", exact=False),
                        page.get_by_role("button", name="Add source", exact=False),
                        page.get_by_role("button", name="Add", exact=False),
                        page.get_by_text("Upload a source", exact=False),
                        page.get_by_text("Add source", exact=False),
                        page.locator("button:has-text('Upload a source')"),
                        page.locator("button:has-text('Add')"),
                        page.locator("button:has-text('添加')"),
                    ]
                    for sel in add_selectors:
                        try:
                            if sel.first.is_visible():
                                add_source_btn = sel.first
                                break
                        except Exception:
                            pass

                    if add_source_btn:
                        print(f"Clicking Add Source button: {add_source_btn}")
                        add_source_btn.click()
                        time.sleep(2)

                        # Check again for 'Website' span with a wait
                        for sel in target_selectors:
                            try:
                                sel.first.wait_for(state="visible", timeout=5000)
                                website_btn = sel.first
                                break
                            except Exception:
                                pass

                if not website_btn:
                    avail = list_available_buttons(page)
                    capture_screenshot(page, "error_website_source_button")
                    raise AssertionError(
                        f"Could not locate Website or Link source selector. Buttons found: {avail}"
                    )

                print("Clicking 'Website' option...")
                website_btn.click()
                time.sleep(3)

                # Locate input field for URL (textarea with formcontrolname='newUrl')
                url_input = None
                input_selectors = [
                    page.locator("textarea[formcontrolname='newUrl']"),
                    page.locator(".cdk-overlay-container textarea"),
                    page.get_by_label("Paste URLs", exact=False),
                    page.locator("textarea"),
                ]
                for sel in input_selectors:
                    try:
                        # Wait up to 5 seconds for it to be visible
                        sel.first.wait_for(state="visible", timeout=5000)
                        url_input = sel.first
                        break
                    except Exception:
                        pass

                assert url_input, "Could not find URL input field (textarea) in the sources dialog."

                test_url = "https://en.wikipedia.org/wiki/Artificial_intelligence"
                print(f"Entering source URL: {test_url}")
                url_input.fill(test_url)

                # Find submission button
                insert_btn = None
                insert_selectors = [
                    page.locator(".cdk-overlay-container").get_by_role(
                        "button", name="Insert", exact=False
                    ),
                    page.get_by_role("button", name="Insert", exact=False),
                    page.get_by_role("button", name="Add", exact=False),
                    page.get_by_text("Insert", exact=False),
                    page.get_by_text("Add", exact=False),
                    page.locator("button:has-text('Insert')"),
                    page.locator("button:has-text('Add')"),
                    page.locator("button:has-text('插入')"),
                ]
                for sel in insert_selectors:
                    try:
                        # Wait up to 3 seconds for it to be visible
                        sel.first.wait_for(state="visible", timeout=3000)
                        insert_btn = sel.first
                        break
                    except Exception:
                        pass

                assert insert_btn, "Could not locate 'Insert' / 'Add' submission button."
                print("Clicking 'Insert' button...")
                insert_btn.click()

                print("Waiting for source processing to finalize...")
                ai_text = page.get_by_text("Artificial intelligence", exact=False).first
                ai_text.wait_for(state="visible", timeout=30000)
                capture_screenshot(page, "03_source_processing_complete")

                # --- Step 4: Create Audio Overview (Deep Dive) ---
                print("\n--- [Step 4] Generating Audio Overview ---")
                audio_tile = page.locator(
                    ".create-artifact-button-container:has-text('Audio Overview') .create-label-container"
                ).first
                audio_tile.wait_for(state="visible", timeout=10000)
                print("Clicking 'Audio Overview' tile in the Studio panel...")
                audio_tile.click(force=True)

                # Wait dynamically for generation to start
                audio_loading = page.locator(
                    "//*[contains(text(), 'Generating Audio Overview')]"
                ).first
                audio_loading.wait_for(state="visible", timeout=30000)
                print("✅ Audio Overview generation is initiated and running.")

                # --- Step 5: Create Slides (Enterprise specific) ---
                print("\n--- [Step 5] Generating Presenter Slides ---")
                slides_tile = page.locator(
                    ".create-artifact-button-container:has-text('Slide Deck') .create-label-container"
                ).first
                slides_tile.wait_for(state="visible", timeout=10000)
                print("Clicking 'Slide Deck' tile in the Studio panel...")
                slides_tile.click(force=True)

                # Wait dynamically for generation to start
                slides_loading = page.locator(
                    "//*[contains(text(), 'Generating Slide Deck')]"
                ).first
                slides_loading.wait_for(state="visible", timeout=30000)
                print("✅ Slide Deck generation is initiated and running.")

                # --- Step 6: Create Video Overview (Enterprise specific) ---
                print("\n--- [Step 6] Generating Video Overview ---")
                video_tile = page.locator(
                    ".create-artifact-button-container:has-text('Video Overview') .create-label-container"
                ).first
                video_tile.wait_for(state="visible", timeout=10000)
                print("Clicking 'Video Overview' tile in the Studio panel...")
                video_tile.click(force=True)

                # Wait dynamically for generation to start
                video_loading = page.locator(
                    "//*[contains(text(), 'Generating Video Overview')]"
                ).first
                video_loading.wait_for(state="visible", timeout=30000)
                print("✅ Video Overview generation is initiated and running.")

                # --- Step 7: Create Report (Enterprise specific) ---
                print("\n--- [Step 7] Generating Briefing Doc Report ---")
                reports_tile = page.locator(
                    "hover-create-artifact-button .create-artifact-button-container .create-label-container"
                ).first
                reports_tile.wait_for(state="visible", timeout=10000)
                print("Clicking 'Reports' tile in the Studio panel...")
                reports_tile.click(force=True)

                # Wait for Material Menu Panel to be visible
                menu_panel = page.locator(".mat-mdc-menu-panel").first
                menu_panel.wait_for(state="visible", timeout=10000)
                print("Clicking 'Briefing Doc' option inside the menu...")

                briefing_doc_item = page.locator(".mat-mdc-menu-item:has-text('Briefing Doc')").first
                briefing_doc_item.wait_for(state="visible", timeout=10000)
                briefing_doc_item.click()

                # Wait dynamically for generation to start
                report_loading = page.locator(
                    "//*[contains(text(), 'Generating Briefing Doc') or contains(text(), 'Generating Note')]"
                ).first
                report_loading.wait_for(state="visible", timeout=30000)
                print("✅ Report generation is initiated and running.")

                # --- Step 8: Verify Generation Progress ---
                print("\n--- [Step 8] Verifying Generation States ---")
                capture_screenshot(page, "04_generation_initiated")

                print(
                    "\n🎉 Full NotebookLM Enterprise Studio E2E UI flow "
                    "executed & verified successfully."
                )
                capture_screenshot(page, "05_final_studio_state")

            except PlaywrightError as err:
                print(f"❌ Playwright Error encountered: {err}")
                capture_screenshot(page, "error_playwright_exception")
                raise

            finally:
                if created_notebook_id:
                    print(
                        f"[Studio Test] Cleaning up created notebook: "
                        f"{created_notebook_id}"
                    )
                    try:
                        import threading
                        def run_in_thread():
                            async def do_cleanup():
                                tokens = await AuthTokens.from_storage()
                                async with NotebookLMClient(tokens) as client:
                                    await client.notebooks.delete(created_notebook_id)
                            asyncio.run(do_cleanup())
                        thread = threading.Thread(target=run_in_thread)
                        thread.start()
                        thread.join()
                        print("[Studio Test] Notebook cleanup successful.")
                    except Exception as exc:
                        print(
                            f"[Studio Test] ⚠️ Warning: Could not clean up "
                            f"notebook {created_notebook_id}: {exc}"
                        )

                context.close()
                browser.close()
