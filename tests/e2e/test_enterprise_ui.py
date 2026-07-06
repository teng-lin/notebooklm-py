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

import os
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from notebooklm.paths import get_storage_path


def get_enterprise_url() -> str:
    """Construct the fully qualified NotebookLM Enterprise URL.

    Uses environment variables for project-bound and regional enterprise URL routing:
    - NOTEBOOKLM_BASE_URL (defaults to https://notebooklm.cloud.google.com)
    - NOTEBOOKLM_REGION (defaults to global)
    - NOTEBOOKLM_PROJECT (defaults to 766918001064)
    """
    base_url = os.environ.get("NOTEBOOKLM_BASE_URL", "https://notebooklm.cloud.google.com")
    region = os.environ.get("NOTEBOOKLM_REGION", "global")
    project = os.environ.get("NOTEBOOKLM_PROJECT", "766918001064")

    url = f"{base_url.rstrip('/')}/{region}/"
    if project:
        url += f"?project={project}"
    return url


def capture_screenshot(page: Page, step_name: str) -> Path:
    """Capture a full page screenshot and save it to the session artifacts directory.

    Args:
        page: The Playwright page instance.
        step_name: Short identifier for the step (e.g. '01_login').

    Returns:
        The Path to the saved screenshot.
    """
    artifact_dir = Path("/Users/jush/.gemini/antigravity-cli/brain/dd9cb0d8-5a1a-40f6-874a-d03d75977356")
    if not artifact_dir.exists():
        # Fallback to local workspace scratch dir
        artifact_dir = Path("./scratch")
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
                print("[Login Test] ⚠️ No storage state found. Interactive fallback is required.")
                if headless:
                    pytest.skip("Skipping headless run; no valid storage_state.json is available.")

            context = browser.new_context(**context_kwargs)
            page = context.new_page()

            try:
                target_url = get_enterprise_url()
                print(f"[Login Test] Navigating to: {target_url}")
                page.goto(target_url, wait_until="commit")

                # Give dynamic elements a few seconds to settle
                time.sleep(5)
                capture_screenshot(page, "01_login_navigation")

                # Handle Google SSO accounts page redirection if headful
                if "accounts.google.com" in page.url:
                    if not headless:
                        print("👉 Redirection detected. Please authenticate manually in the open window...")
                        page.wait_for_url("**/notebooklm.cloud.google.com/**", timeout=60000)
                        print("👉 Successfully logged in. Persisting authenticated state.")
                        context.storage_state(path=str(storage_path))
                    else:
                        raise AssertionError(
                            f"Authentication failed: redirected to Google Login in headless mode (URL: {page.url})"
                        )

                try:
                    page.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass

                # Validate landing. We expect to see 'NotebookLM' in the title or workspace elements.
                print(f"[Login Test] Landed Page URL: {page.url}")
                print(f"[Login Test] Landed Page Title: {page.title()}")

                assert "notebooklm" in page.url or "NotebookLM" in page.title(), (
                    f"Unexpected landing page URL/title. URL: {page.url}, Title: {page.title()}"
                )
                print("[Login Test] ✅ Enterprise Login & Routing Navigation Verified successfully.")

            finally:
                context.close()
                browser.close()

    def test_full_notebook_studio_features(self) -> None:
        """Cohesive end-to-end user journey across all key NotebookLM Enterprise features:

        1. Create Notebook
        2. Add website source link
        3. Generate Audio Overview (Deep Dive)
        4. Generate Video Overview (Enterprise specific)
        5. Generate Slides presentation (Enterprise specific)
        """
        headless = os.environ.get("NOTEBOOKLM_HEADLESS", "1") == "1"
        storage_path = get_storage_path()

        if not storage_path.exists() or storage_path.stat().st_size == 0:
            pytest.skip("Skipping E2E Studio feature tests: no storage_state.json session profile available.")

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

                print("Clicking 'New notebook' button...")
                create_btn.click()

                # Wait for the transition to notebook page
                print("Waiting for workspace initialization redirection...")
                page.wait_for_url("**/notebook/*", timeout=30000)
                print(f"Successfully entered notebook workspace: {page.url}")
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
                    page.locator(".cdk-overlay-container").get_by_role("button", name="Insert", exact=False),
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
                time.sleep(15)
                capture_screenshot(page, "03_source_processing_complete")

                # --- Step 4: Create Audio Overview (Deep Dive) ---
                print("\n--- [Step 4] Generating Audio Overview ---")
                audio_tile = page.locator(".create-artifact-button-container:has-text('Audio Overview')").first
                audio_tile.wait_for(state="visible", timeout=10000)
                print("Clicking 'Audio Overview' tile in the Studio panel...")
                audio_tile.click()
                time.sleep(2)

                # --- Step 5: Create Slides (Enterprise specific) ---
                print("\n--- [Step 5] Generating Presenter Slides ---")
                slides_tile = page.locator(".create-artifact-button-container:has-text('Slide Deck')").first
                slides_tile.wait_for(state="visible", timeout=10000)
                print("Clicking 'Slide Deck' tile in the Studio panel...")
                slides_tile.click()
                time.sleep(2)

                # --- Step 6: Create Video Overview (Enterprise specific) ---
                print("\n--- [Step 6] Generating Video Overview ---")
                video_tile = page.locator(".create-artifact-button-container:has-text('Video Overview')").first
                video_tile.wait_for(state="visible", timeout=10000)
                print("Clicking 'Video Overview' tile in the Studio panel...")
                video_tile.click()
                time.sleep(5)

                # --- Step 7: Verify Generation Progress ---
                print("\n--- [Step 7] Verifying Generation States ---")
                capture_screenshot(page, "04_generation_initiated")

                # Verify each generation task successfully displays its loading progress element
                audio_loading = page.locator("//*[contains(text(), 'Generating Audio Overview')]").first
                audio_loading.wait_for(state="visible", timeout=10000)
                print("✅ Successfully verified: Audio Overview generation is initiated and running.")

                slides_loading = page.locator("//*[contains(text(), 'Generating Slide Deck')]").first
                slides_loading.wait_for(state="visible", timeout=10000)
                print("✅ Successfully verified: Slide Deck generation is initiated and running.")

                video_loading = page.locator("//*[contains(text(), 'Generating Video Overview')]").first
                video_loading.wait_for(state="visible", timeout=10000)
                print("✅ Successfully verified: Video Overview generation is initiated and running.")

                print("\n🎉 Full NotebookLM Enterprise Studio E2E UI flow executed & verified successfully.")
                time.sleep(5)
                capture_screenshot(page, "05_final_studio_state")

            except PlaywrightError as err:
                print(f"❌ Playwright Error encountered: {err}")
                capture_screenshot(page, "error_playwright_exception")
                raise

            finally:
                context.close()
                browser.close()
