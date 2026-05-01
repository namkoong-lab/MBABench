"""
ChatGPT Web Agent - Automate interactions with chatgpt.com web interface.

Uses Playwright to:
1. Navigate to a ChatGPT project
2. Enable agent mode + extended thinking
3. Upload files and submit prompts
4. Wait for response completion
5. Download Excel artifacts
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from claude_web_agent.web_agent import WebAgent, WebAgentState, ConversationMessage

logger = logging.getLogger(__name__)


class ChatGPTWebAgent(WebAgent):
    """Agent for automating ChatGPT web interface."""

    CHATGPT_BASE_URL = "https://chatgpt.com"

    SELECTORS = {
        # Chat input — visible ProseMirror contenteditable div
        "chat_input": 'div.ProseMirror[contenteditable="true"]',
        # Hidden textarea (for page-type detection; display:none so not usable for visibility checks)
        "textarea_project": 'textarea[placeholder*="New chat in"]',
        "textarea_conversation": 'textarea[placeholder="Ask anything"]',
        # Buttons
        "send_button": 'button:has-text("Send prompt"), button[aria-label="Send prompt"], [data-testid="send-button"]',
        "plus_menu_button": '[data-testid="composer-plus-btn"]',
        "stop_button": 'button:has-text("Stop")',
        "answer_now_button": 'button:has-text("Answer now")',
        # File upload
        "add_files_menuitem": '[role="menuitem"]:has-text("Add photos & files")',
        # Feature toggles
        # Agent mode: + menu → hover "More" → click "Agent mode" (menuitemradio)
        # State detection
        "login_button": 'button:has-text("Log in")',
        "thinking_active": 'button:has-text("Pro thinking")',
        "thinking_complete": 'button:text-matches("Thought for \\d")',
        # Response
        "chatgpt_said": 'heading:has-text("ChatGPT said:")',
        "user_said": 'heading:has-text("You said:")',
        # Model — pill in the composer toolbar that opens the dropdown
        "model_selector": 'button.__composer-pill[aria-haspopup="menu"]',
    }

    def __init__(self, page, config: dict, shutdown_event=None, completion_logger=None):
        super().__init__(page, config, shutdown_event, completion_logger)
        self.agent_config = config.get("chatgpt_web", {})
        self.project_id = self.agent_config.get("project_id", "")
        self.project_slug = self.agent_config.get("project_slug", "")
        self.max_wait_per_prompt = self.agent_config.get(
            "max_wait_per_prompt_seconds", 1800
        )
        self.check_interval = self.agent_config.get("check_interval_seconds", 3)
        self.agent_mode = self.agent_config.get("agent_mode", True)
        # Tracks how many response articles existed BEFORE the first prompt,
        # so download_all_artifacts searches all articles from the conversation.
        # Set once before the first prompt; not overwritten by subsequent prompts.
        self._baseline_article_count = 0
        self._baseline_set = False

    @property
    def project_url(self) -> str:
        slug_part = f"-{self.project_slug}" if self.project_slug else ""
        return f"{self.CHATGPT_BASE_URL}/g/g-p-{self.project_id}{slug_part}/project"

    async def navigate_to_new_chat(self) -> bool:
        """Navigate to the ChatGPT project page (which is a new chat).

        Returns True if the page loaded (even if login is required).
        The engine's auth-wait loop handles the login case.
        """
        try:
            # Reset baseline for the new conversation so download_all_artifacts
            # will search all articles from this chat (not carry over from prior task).
            self._baseline_article_count = 0
            self._baseline_set = False

            logger.info(f"Navigating to ChatGPT project: {self.project_url}")

            # NOTE: Do NOT call set_viewport_size on CDP pages — it uses
            # Emulation.setDeviceMetricsOverride which crashes ChatGPT tabs.
            await self.page.goto(
                self.project_url, wait_until="domcontentloaded", timeout=60000
            )

            # Debug: log where we ended up
            current_url = self.page.url
            page_title = await self.page.title()
            logger.info(f"Page loaded — URL: {current_url}")
            logger.info(f"Page loaded — Title: {page_title}")

            # Check if we were redirected to an auth page (not on chatgpt.com)
            if "chatgpt.com" not in current_url:
                logger.warning(f"Redirected to auth page: {current_url}")
                return True  # Let the engine's auth-wait loop handle it

            # Check if we need to authenticate (login button on chatgpt.com)
            state = await self.get_state()
            if state == WebAgentState.AUTH_REQUIRED:
                logger.warning("Authentication required - please log in manually")
                return True  # Let the engine's auth-wait loop handle it

            logger.info(f"Page state: {state.value}")

            # Wait for chat input — the React SPA may need time to hydrate.
            # Try ProseMirror editor first, then fall back to paragraph placeholder.
            # Use wait_for(state="attached") since is_visible() is unreliable on CDP.
            chat_input = self.page.locator(
                'div.ProseMirror[contenteditable="true"], '
                'p[data-placeholder*="New chat"]'
            )

            for attempt_label in ("initial load", "after reload"):
                try:
                    await chat_input.first.wait_for(state="attached", timeout=15000)
                    logger.info(f"ChatGPT chat input visible ({attempt_label})")
                    return True
                except Exception:
                    if attempt_label == "initial load":
                        logger.info("Chat input not visible yet — reloading page")
                        await self.page.reload(
                            wait_until="domcontentloaded", timeout=30000
                        )
                        await self.page.wait_for_timeout(3000)

            # Last resort: dump page content for debugging
            try:
                body_text = await self.page.locator("body").inner_text()
                logger.error(
                    f"Could not find chat input. Page text (first 500 chars): {body_text[:500]}"
                )
            except Exception:
                logger.error("Could not find chat input on ChatGPT page")
            return False
        except Exception as e:
            logger.error(f"Navigation failed: {e}")
            return False

    async def get_state(self) -> WebAgentState:
        """Detect ChatGPT page state.

        Uses JS evaluate instead of Playwright ``is_visible()`` because the
        latter is unreliable on Chrome CDP connections.
        """
        try:
            current_url = self.page.url
            if "chatgpt.com" not in current_url:
                return WebAgentState.AUTH_REQUIRED

            state_info = await self.page.evaluate(
                """() => {
                const btns = Array.from(document.querySelectorAll('button'));
                const hasLogin = btns.some(b => b.textContent.trim() === 'Log in');
                const hasStop = btns.some(b => b.textContent.trim() === 'Stop');
                const hasThinking = btns.some(b => b.textContent.includes('Pro thinking'));
                const hasInput = !!document.querySelector(
                    'div.ProseMirror[contenteditable="true"], p[data-placeholder]'
                );
                return { hasLogin, hasStop, hasThinking, hasInput };
            }"""
            )

            if state_info["hasLogin"]:
                return WebAgentState.AUTH_REQUIRED
            if state_info["hasStop"] or state_info["hasThinking"]:
                return WebAgentState.RUNNING
            if state_info["hasInput"]:
                return WebAgentState.READY

            return WebAgentState.UNKNOWN
        except Exception as e:
            logger.error(f"State detection error: {e}")
            return WebAgentState.ERROR

    async def _check_button_text(self, text: str) -> bool:
        """CDP-safe check: does any button on the page contain *text*?

        Checks both ``textContent`` and ``aria-label`` because ChatGPT uses
        aria-labels like "Agent, click to remove" while the visible text is
        just "Agent".  Playwright's ``is_visible()`` is unreliable on CDP
        connections, so we query the DOM directly via JavaScript.
        """
        try:
            return await self.page.evaluate(
                "(t) => Array.from(document.querySelectorAll('button'))"
                ".some(b => b.textContent.includes(t)"
                " || (b.getAttribute('aria-label') || '').includes(t))",
                text,
            )
        except Exception:
            return False

    MODEL_TESTIDS = {
        "instant": "model-switcher-gpt-5-3",
        "thinking": "model-switcher-gpt-5-5-thinking",
        "pro": "model-switcher-gpt-5-5-pro",
    }

    async def ensure_model_selected(self) -> bool:
        """
        Select a ChatGPT model via the composer-pill model switcher.

        Reads ``chatgpt_web.model`` from config. If not set, skips selection
        and uses the current default. Lookup is case-insensitive.

        Supported values: ``instant``, ``thinking``, ``pro``. ``pro`` selects
        Pro with its current effort setting (default Extended) — no separate
        "Extended Pro" toggle exists in the new UI.

        Selection flow:
          1. Click the composer pill (``button.__composer-pill[aria-haspopup="menu"]``)
             whose visible text is the current model name.
          2. Click the row matching the configured model's ``data-testid``
             (e.g. ``model-switcher-gpt-5-5-pro``).
        """
        target_model = self.agent_config.get("model")
        if not target_model:
            logger.info("No ChatGPT model specified in config — using current default")
            return True

        testid = self.MODEL_TESTIDS.get(target_model.lower())
        if not testid:
            logger.warning(
                "Unknown ChatGPT model '%s'. Valid options: %s. Using current default.",
                target_model,
                ", ".join(self.MODEL_TESTIDS.keys()),
            )
            return True

        try:
            logger.info("Selecting ChatGPT model: %s (%s)", target_model, testid)

            pill_handle = await self.page.evaluate_handle(
                """() => {
                    const pills = Array.from(document.querySelectorAll(
                        'button.__composer-pill[aria-haspopup="menu"]'
                    ));
                    const re = /^(instant|thinking|pro|gpt|chatgpt|auto)\\b/i;
                    return pills.find(b => re.test((b.textContent || '').trim()))
                        || pills[0]
                        || null;
                }"""
            )
            pill = pill_handle.as_element() if pill_handle else None
            if pill is None:
                logger.warning(
                    "ChatGPT model-switcher pill not found — skipping model selection"
                )
                return True

            await pill.click()
            await asyncio.sleep(1)

            target = await self.page.query_selector(f'[data-testid="{testid}"]')
            if target is None:
                logger.warning(
                    "Model row '%s' not found in dropdown — using current default",
                    testid,
                )
                await self.page.keyboard.press("Escape")
                return True

            await target.click()
            await asyncio.sleep(0.5)
            logger.info("ChatGPT model '%s' selected successfully", target_model)
            return True

        except Exception as e:
            logger.error("Error selecting ChatGPT model: %s", e)
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return True

    async def _enable_agent_mode(self) -> bool:
        """Enable Agent mode via + menu > hover More > click Agent mode."""
        try:
            # Open + menu
            plus_btn = self.page.locator('[data-testid="composer-plus-btn"]')
            await plus_btn.click(timeout=5000)
            await asyncio.sleep(1)

            # Hover over "More" to reveal submenu
            more = self.page.get_by_role("menuitem", name="More")
            await more.hover()
            await asyncio.sleep(3)

            # Click "Agent mode"
            agent = self.page.get_by_role("menuitemradio", name="Agent mode")
            checked = await agent.get_attribute("aria-checked")
            if checked == "true":
                logger.info("Agent mode already enabled")
                await self.page.keyboard.press("Escape")
                return True

            await agent.click(timeout=5000)
            await asyncio.sleep(1)
            logger.info("Agent mode enabled")
            return True
        except Exception as e:
            logger.warning(f"Failed to enable agent mode: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def ensure_features_enabled(self) -> bool:
        """Enable Agent mode or Pro model as configured, and select model.

        When ``agent_mode`` is True: + menu > More > Agent mode toggle.
        When ``agent_mode`` is False: rely on ``chatgpt_web.model`` (set
        ``pro`` for Extended Pro) — no separate toggle exists.

        If ``model`` is set in config, ``ensure_model_selected`` picks it
        via the composer model pill before agent mode is enabled.
        """
        await asyncio.sleep(2)

        await self.ensure_model_selected()

        if self.agent_mode:
            return await self._enable_agent_mode()
        else:
            logger.info("Non-agent mode — model selection done via ensure_model_selected")
            return True

    async def upload_files(self, file_paths: list[str]) -> bool:
        """Upload files via the + menu > Add photos & files flow.

        Falls back to the "Add files and more" button (bottom-left of composer)
        if the + menu approach fails — the button text changes depending on
        whether agent mode is active.
        """
        try:
            for file_path in file_paths:
                logger.info(f"Uploading file: {file_path}")

                uploaded = False

                # Approach 1: + menu > "Add photos & files"
                try:
                    # Dismiss any stale popup first
                    await self.page.keyboard.press("Escape")
                    await self.page.wait_for_timeout(300)

                    plus_btn = self.page.locator(self.SELECTORS["plus_menu_button"])
                    if await plus_btn.count() > 0:
                        await plus_btn.click(timeout=5000)

                        # Wait for the Radix menu to actually open instead of
                        # a blind 1s timeout. data-state="open" on the role=menu
                        # root is ChatGPT's open signal; catches silent click
                        # failures.
                        try:
                            await self.page.wait_for_selector(
                                '[role="menu"][data-state="open"]', timeout=3000
                            )
                        except Exception:
                            logger.debug("+ menu did not show [data-state=open]")

                        # Anchor on role+name (ARIA accessible name), with
                        # historical labels as fallbacks. Current ChatGPT UI
                        # ships "Add photos & files"; older builds shipped
                        # "Add files and more" / "Add files".
                        add_files = None
                        for name in (
                            "Add photos & files",
                            "Add files and more",
                            "Add files",
                        ):
                            cand = self.page.get_by_role("menuitem", name=name)
                            if await cand.count() > 0:
                                add_files = cand.first
                                break

                        try:
                            if add_files is None:
                                raise RuntimeError("add_files menuitem not found")
                            async with self.page.expect_file_chooser(
                                timeout=10000
                            ) as fc_info:
                                await add_files.click(timeout=5000)

                            file_chooser = await fc_info.value
                            await file_chooser.set_files(file_path)
                            uploaded = True
                        except Exception:
                            logger.info(
                                "+ menu 'Add photos & files' not found, trying fallback"
                            )
                            await self.page.keyboard.press("Escape")
                            await self.page.wait_for_timeout(500)
                except Exception:
                    logger.info("+ menu approach failed, trying fallback")

                # Approach 2: composer-level "Add files" button (legacy path
                # visible in some agent-mode states). Anchor via aria-label
                # where possible; text fallbacks are last-resort.
                if not uploaded:
                    try:
                        add_files_btn = self.page.locator(
                            'button[aria-label="Add files and more"], '
                            'button[aria-label="Add photos & files"], '
                            'button[aria-label*="file" i], '
                            'button:has-text("Add files and more"), '
                            'button:has-text("Add files")'
                        )
                        if await add_files_btn.count() > 0:
                            async with self.page.expect_file_chooser(
                                timeout=10000
                            ) as fc_info:
                                await add_files_btn.first.click()

                            file_chooser = await fc_info.value
                            await file_chooser.set_files(file_path)
                            uploaded = True
                            logger.info("Used 'Add files' button fallback")
                    except Exception as e:
                        logger.warning(f"Fallback file upload also failed: {e}")

                if not uploaded:
                    raise RuntimeError(f"Could not upload file: {file_path}")

                await self.page.wait_for_timeout(2000)

                # Verify file appears as attachment
                filename = Path(file_path).name
                attachment = self.page.locator(f'[role="group"]:has-text("{filename}")')
                try:
                    await attachment.wait_for(state="attached", timeout=5000)
                    logger.info(f"File attached: {filename}")
                except Exception:
                    logger.warning(f"File attachment not confirmed for: {filename}")

            return True
        except Exception as e:
            logger.error(f"File upload failed: {e}")
            return False

    async def submit_prompt(self, prompt: str, prompt_number: int = 1) -> bool:
        """Type prompt text and click send."""
        try:
            logger.info(f"Submitting prompt {prompt_number} ({len(prompt)} chars)")

            # Focus the chat input (ProseMirror div OR paragraph placeholder)
            editor = self.page.locator(
                'div.ProseMirror[contenteditable="true"], '
                'p[data-placeholder*="New chat"]'
            )
            try:
                await editor.first.wait_for(state="attached", timeout=10000)
            except Exception:
                logger.error("Chat input not found")
                return False
            await editor.first.click()
            await self.page.wait_for_timeout(300)

            # Clear any leftover text
            await self.page.keyboard.press("Meta+a")
            await self.page.keyboard.press("Backspace")
            await self.page.wait_for_timeout(200)

            # Fill prompt — try Playwright fill() first, fall back to clipboard paste
            try:
                await editor.first.fill(prompt)
            except Exception:
                logger.info("fill() failed, falling back to clipboard paste")
                await self.page.evaluate(
                    "(text) => navigator.clipboard.writeText(text)", prompt
                )
                await self.page.keyboard.press("Meta+v")
                await self.page.wait_for_timeout(500)
            await self.page.wait_for_timeout(1000)

            # Enable agent mode or extended thinking after files are attached
            # and prompt is typed, but before sending (only on first prompt).
            if prompt_number == 1:
                features_ok = await self.ensure_features_enabled()
                if not features_ok:
                    logger.warning("Failed to enable features before sending")

            # Click send button
            url_before = self.page.url
            send_btn = self.page.locator(self.SELECTORS["send_button"])
            try:
                await send_btn.first.wait_for(state="visible", timeout=10000)
                await send_btn.first.click()
            except Exception:
                # Fallback: try pressing Enter to send
                logger.info("Send button not clickable, trying Enter key")
                await self.page.keyboard.press("Enter")

            # Wait for confirmation that the prompt was sent.
            # For prompt 1: URL changes from project page to /c/{id}
            # For prompts 2+: URL already has /c/ — check generation indicators
            already_in_conversation = "/c/" in url_before
            for _ in range(30):  # 30s max wait for send confirmation
                await self.page.wait_for_timeout(1000)
                current_url = self.page.url
                if current_url != url_before and "/c/" in current_url:
                    logger.info(f"Prompt sent — conversation: {current_url}")
                    return True
                # Check if generation started (Stop button visible) — CDP-safe
                if await self._check_button_text("Stop"):
                    logger.info("Prompt sent (Stop button appeared)")
                    return True
                # For follow-up prompts, also check if a new article appeared
                if already_in_conversation and await self._is_generating():
                    logger.info("Prompt sent (generation indicators detected)")
                    return True

            logger.error(
                f"Prompt may not have been sent (URL unchanged: {self.page.url})"
            )
            return False

        except Exception as e:
            logger.error(f"Submit prompt failed: {e}")
            return False

    async def _is_generating(self) -> bool:
        """Check if ChatGPT is still generating using JS DOM queries.

        CDP tabs may not render overlay elements as "visible" to Playwright,
        so we query the DOM directly via JavaScript. Checks for:
        - Stop button via data-testid (reliable during Code Interpreter execution)
        - Stop button via text (present during Pro thinking)
        - Answer now button (present during extended thinking)
        - Pro thinking indicator (extended thinking in progress)
        - result-streaming class (text streaming)
        - "Writing code" / "Analyzing" text (agent mode code execution)
        - "ChatGPT is generating" status text
        - Send button absence (no send button while generating)
        """
        try:
            return await self.page.evaluate(
                """() => {
                // Most reliable: data-testid="stop-button" (present during ALL generation phases)
                const hasStopBtn = !!document.querySelector('[data-testid="stop-button"]');
                // Only check buttons INSIDE the main chat area, not sidebar history items
                // (sidebar items like "Early Stopping in Experiments" have aria-labels containing "Stop")
                const mainArea = document.querySelector('main') || document.body;
                const btns = Array.from(mainArea.querySelectorAll('button'));
                const hasStop = btns.some(b => b.textContent.trim() === 'Stop');
                const hasAnswerNow = btns.some(b => b.textContent.trim() === 'Answer now');
                const hasThinking = btns.some(b => b.textContent.includes('Pro thinking'));
                const hasGenerating = !!document.querySelector('[class*="result-streaming"]');
                // Agent mode indicators: only check within the conversation area
                const mainText = mainArea.innerText || '';
                const hasWritingCode = mainText.includes('Writing code');
                const hasAnalyzing = /Analyz(ing|ed)/.test(mainText) && (hasStop || hasStopBtn);
                return hasStopBtn || hasStop || hasAnswerNow || hasThinking || hasGenerating || hasWritingCode || hasAnalyzing;
            }"""
            )
        except Exception:
            return False

    async def _dump_phase1_timeout_state(self, prompt_number: int) -> None:
        """Save HTML + screenshot when Phase-1 start-detection times out.

        Lets us reconstruct what the page actually looked like during a
        'Response generation did not start within 120s' failure.
        """
        try:
            from datetime import datetime
            from pathlib import Path

            if self.completion_logger is not None:
                base_dir = Path(self.completion_logger.log_dir).parent
                task_id = self.completion_logger.task_identifier or "unknown"
            else:
                base_dir = Path.cwd()
                task_id = "unknown"
            dump_dir = base_dir / "phase1_timeouts"
            dump_dir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            stem = f"{ts}_{task_id}_p{prompt_number}"
            html_path = dump_dir / f"{stem}.html"
            png_path = dump_dir / f"{stem}.png"
            try:
                html = await self.page.content()
                html_path.write_text(html, encoding="utf-8")
            except Exception as e:
                logger.warning(f"Phase-1 timeout HTML dump failed: {e}")
            try:
                await self.page.screenshot(path=str(png_path), full_page=True)
            except Exception as e:
                logger.warning(f"Phase-1 timeout screenshot failed: {e}")
            logger.info(f"Phase-1 timeout state saved to {dump_dir}/{stem}.{{html,png}}")
        except Exception as e:
            logger.warning(f"Phase-1 timeout dump failed: {e}")

    async def _count_conversation_turns(self) -> int:
        """Count [data-testid^="conversation-turn-"] elements.

        Used as an early Phase-1 signal: a new turn DOM is mounted as soon as
        the user submits, before the lazily-set data-message-author-role
        attribute appears and before the stop-button mounts in some flows.
        """
        try:
            return await self.page.evaluate(
                "() => document.querySelectorAll('[data-testid^=\"conversation-turn-\"]').length"
            )
        except Exception:
            return 0

    async def _count_response_articles(self) -> int:
        """Count ChatGPT response articles currently on the page.

        Uses JS evaluate for CDP reliability. Tries current DOM structure
        first (data-message-author-role), falls back to legacy <article>.
        """
        try:
            return await self.page.evaluate(
                """() => {
                // Current DOM: data-message-author-role='assistant'
                const assistants = document.querySelectorAll("[data-message-author-role='assistant']");
                if (assistants.length > 0) return assistants.length;
                // Legacy: <article> with <h6> ChatGPT said
                return Array.from(document.querySelectorAll('article'))
                    .filter(a => {
                        const h6 = a.querySelector('h6');
                        return h6 && h6.textContent.includes('ChatGPT said:');
                    }).length;
            }"""
            )
        except Exception:
            return 0

    async def wait_for_response(self, prompt_number: int = 1) -> Optional[str]:
        """Wait for ChatGPT to finish responding.

        Uses a three-phase approach:
        1. Snapshot existing articles so we only track NEW responses.
        2. Wait for generation to actually start (new article OR generation
           indicators like Stop/Pro thinking).
        3. Wait for completion using content stabilization — the response text
           must stop changing for a sustained period AND generation indicators
           must be gone.

        This prevents premature completion when:
        - Stale articles from prior conversations are on the page
        - There's a gap between extended thinking and agent code execution
        - Agent mode pauses between code execution steps
        """
        logger.info(f"Waiting for response to prompt {prompt_number}...")
        start_time = asyncio.get_event_loop().time()

        # Phase 0: Snapshot how many response articles exist BEFORE this response
        baseline_article_count = await self._count_response_articles()
        if not self._baseline_set:
            # Only store baseline before the FIRST prompt so that
            # download_all_artifacts searches ALL conversation articles,
            # not just those from the last prompt.
            self._baseline_article_count = baseline_article_count
            self._baseline_set = True
            logger.info(
                f"Baseline response articles on page (first prompt): {baseline_article_count}"
            )
        else:
            logger.info(
                f"Current response articles on page: {baseline_article_count} (baseline kept at {self._baseline_article_count})"
            )

        # Minimum time before accepting completion (agent mode tasks take 15-30 min,
        # Pro/Thinking mode tasks also need substantial time for Excel building).
        # However, if a file card is detected (Spreadsheet/.xlsx), we accept sooner
        # since the model may have finished quickly with just a file output.
        min_elapsed_sec = 300 if self.agent_mode else 120
        min_elapsed_sec_with_file = 30  # Accept quickly if file card present

        # Snapshot conversation-turn count too — turn DOM mounts immediately
        # on submit, so an increment is a strong start signal even when the
        # stop-button or data-message-author-role attribute hasn't appeared yet.
        baseline_turn_count = await self._count_conversation_turns()

        # Give ChatGPT time to start generating
        await self.page.wait_for_timeout(5000)

        # Phase 1: Wait for generation to start (up to 120s)
        # Require EITHER generation indicators OR a NEW article (beyond baseline)
        generation_started = False
        for _ in range(60):
            if self.shutdown_event and self.shutdown_event.is_set():
                return None

            if await self._is_generating():
                generation_started = True
                logger.info("Response generation started (indicators detected)")
                break

            # Check for a NEW ChatGPT article (beyond baseline count)
            current_count = await self._count_response_articles()
            if current_count > baseline_article_count:
                generation_started = True
                logger.info(
                    f"New ChatGPT response article appeared ({current_count} > {baseline_article_count})"
                )
                break

            current_turn_count = await self._count_conversation_turns()
            if current_turn_count > baseline_turn_count:
                generation_started = True
                logger.info(
                    f"New conversation turn appeared ({current_turn_count} > {baseline_turn_count})"
                )
                break

            await self.page.wait_for_timeout(2000)

        if not generation_started:
            logger.error("Response generation did not start within 120s")
            await self._dump_phase1_timeout_state(prompt_number)
            return None

        # Phase 2: Wait for completion using content stabilization
        # The response text must stop changing AND generation indicators must be
        # gone for required_stable consecutive checks.
        stable_count = 0
        required_stable = 5  # 5 consecutive checks × check_interval (15s at 3s)
        last_response_text = ""
        last_article_count = baseline_article_count

        while (asyncio.get_event_loop().time() - start_time) < self.max_wait_per_prompt:
            if self.shutdown_event and self.shutdown_event.is_set():
                return None

            generating = await self._is_generating()

            # Also track article count — agent mode creates multiple response articles
            current_article_count = await self._count_response_articles()
            articles_changed = current_article_count != last_article_count
            if articles_changed:
                logger.info(
                    f"Article count changed: {last_article_count} -> {current_article_count}"
                )
                last_article_count = current_article_count

            # Always sample response text (even during generation) for monitoring
            current_response = await self._extract_last_response() or ""

            if generating or articles_changed:
                stable_count = 0
                # Track text even during generation so we know progress
                last_response_text = current_response
            else:
                # Check if response text is still changing (content stabilization)
                text_changed = current_response != last_response_text
                last_response_text = current_response

                if text_changed:
                    stable_count = 0  # Content still growing
                else:
                    stable_count += 1

                elapsed = asyncio.get_event_loop().time() - start_time

                if stable_count >= required_stable:
                    # Content is stable and generation stopped.
                    # Accept if we have content and enough time elapsed.
                    # File card responses can be very short (e.g. "Here is your file:\nSpreadsheet" = ~37 chars)
                    has_file_indicator = any(
                        kw in current_response
                        for kw in [
                            "Spreadsheet",
                            ".xlsx",
                            ".xls",
                            "Excel file",
                            "Download",
                        ]
                    )
                    content_ok = len(current_response) > 50 or has_file_indicator
                    if content_ok:
                        effective_min = (
                            min_elapsed_sec_with_file
                            if has_file_indicator
                            else min_elapsed_sec
                        )
                        if elapsed >= effective_min:
                            logger.info(
                                f"Response complete ({int(elapsed)}s elapsed, "
                                f"{len(current_response)} chars, file_indicator={has_file_indicator})"
                            )
                            return current_response
                        else:
                            logger.info(
                                f"Content stable but too early ({int(elapsed)}s < {min_elapsed_sec}s min), "
                                f"continuing to wait..."
                            )
                            stable_count = 0
                    else:
                        # Content too short and no file indicators — keep waiting
                        stable_count = 0

            elapsed = int(asyncio.get_event_loop().time() - start_time)
            # Log every ~30s (use range check since loop interval may skip exact multiples)
            if elapsed > 0 and elapsed % 30 < (self.check_interval + 1):
                resp_len = len(last_response_text)
                logger.info(
                    f"Waiting... {elapsed}s elapsed, generating={generating}, "
                    f"response_len={resp_len}, stable={stable_count}/{required_stable}"
                )

            await self.page.wait_for_timeout(self.check_interval * 1000)

        # Timeout — return whatever we have if it looks reasonable
        logger.warning(f"Response timeout after {self.max_wait_per_prompt}s")
        final_response = await self._extract_last_response()
        if final_response and len(final_response) > 50:
            logger.info(
                f"Returning partial response ({len(final_response)} chars) after timeout"
            )
            return final_response
        return None

    async def _extract_last_response(self) -> Optional[str]:
        """Extract text from the last ChatGPT response.

        Uses JS evaluate instead of Playwright locators for CDP reliability.
        Tries multiple DOM strategies since ChatGPT's structure changes:
        1. data-message-author-role='assistant' (current, 2025+)
        2. <article> with <h6> 'ChatGPT said:' (legacy)
        """
        try:
            text = await self.page.evaluate(
                """() => {
                // Strategy 1: data-message-author-role (current DOM structure)
                const assistants = document.querySelectorAll("[data-message-author-role='assistant']");
                if (assistants.length > 0) {
                    return assistants[assistants.length - 1].innerText;
                }
                // Strategy 2: <article> with <h6> ChatGPT said (legacy)
                const articles = Array.from(document.querySelectorAll('article'));
                for (let i = articles.length - 1; i >= 0; i--) {
                    const h6 = articles[i].querySelector('h6');
                    if (h6 && h6.textContent.includes('ChatGPT said:')) {
                        return articles[i].innerText;
                    }
                }
                return null;
            }"""
            )
            if text:
                text = text.replace("ChatGPT said:\n", "").strip()
                return text if text else None
            return None
        except Exception as e:
            logger.error(f"Response extraction failed: {e}")
            return None

    async def download_all_artifacts(
        self, download_dir: Optional[str] = None, timeout: int = 30000
    ) -> list[str]:
        """Download all Excel artifacts from the ChatGPT conversation.

        ChatGPT agent mode produces file artifacts as inline preview cards
        embedded in the response article. The DOM structure is:

            paragraph
              └── generic (outer container)
                    └── generic (header row)
                          ├── generic (left: img icon + filename text)
                          └── generic (right: icon-only buttons)
                                ├── button (expand/preview)
                                └── button (download)   ← TARGET
                    └── generic (sheet tabs: "model", "answers", etc.)

        The download buttons have NO text — just an <img> icon. We identify
        artifact cards using JS to find the correct structure, then click the
        last button in the action row (which is the download button).

        Falls back to sandbox download links (/mnt/data/ paths) if no
        preview cards are found.
        """
        downloaded = []
        download_path = Path(download_dir) if download_dir else Path(".")
        download_path.mkdir(parents=True, exist_ok=True)

        try:
            # Scroll to the bottom of the conversation to ensure all file cards
            # are rendered in the DOM before searching.
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.wait_for_timeout(2000)

            # Strategy 1: Find artifact preview cards via JS DOM inspection.
            # Only search articles AFTER the baseline count (i.e., new articles
            # from the current response), to avoid picking up file cards from
            # previous conversations in the project.
            baseline = self._baseline_article_count
            logger.info(
                f"Searching for artifacts in articles after baseline={baseline}"
            )

            scan_result = await self.page.evaluate(
                """(baseline) => {
                // File card keywords: ChatGPT may show "Spreadsheet", the actual
                // filename with .xlsx extension, or other file type labels.
                const FILE_KEYWORDS = ['.xlsx', '.xls', 'Spreadsheet', 'Excel'];

                // Roots to skip in document-wide fallback (composer, nav, etc.)
                const SKIP_TAGS = new Set(['CODE', 'PRE', 'NAV', 'HEADER', 'FORM',
                                           'TEXTAREA', 'INPUT', 'SCRIPT', 'STYLE']);

                function ancestorChain(el) {
                    const parts = [];
                    let cur = el;
                    while (cur && cur !== document.body && parts.length < 8) {
                        let label = cur.tagName;
                        const role = cur.getAttribute && cur.getAttribute('data-message-author-role');
                        if (role) label += '[role=' + role + ']';
                        else if (cur.className && typeof cur.className === 'string') {
                            const firstCls = cur.className.trim().split(/\\s+/)[0];
                            if (firstCls) label += '.' + firstCls.substring(0, 24);
                        }
                        parts.push(label);
                        cur = cur.parentElement;
                    }
                    return parts.join(' < ');
                }

                function scanRoot(root, artifactsOut, seenCards) {
                    const walker = document.createTreeWalker(
                        root,
                        NodeFilter.SHOW_TEXT,
                        { acceptNode: (node) => {
                            const text = node.textContent.trim();
                            if (!text) return NodeFilter.FILTER_REJECT;
                            const isFile = FILE_KEYWORDS.some(kw => text.includes(kw));
                            if (!isFile) return NodeFilter.FILTER_REJECT;
                            // Reject if inside a skipped ancestor, OR inside a
                            // user message (user-uploaded file cards have the
                            // same shape as assistant output cards, so only the
                            // author-role ancestor distinguishes them).
                            let el = node.parentElement;
                            while (el && el !== root) {
                                if (SKIP_TAGS.has(el.tagName)) return NodeFilter.FILTER_REJECT;
                                const role = el.getAttribute &&
                                    el.getAttribute('data-message-author-role');
                                if (role === 'user') return NodeFilter.FILTER_REJECT;
                                // Belt-and-suspenders: user-message wrapper has
                                // items-end rtl:items-start on the outer flex div.
                                if (el.className && typeof el.className === 'string' &&
                                    el.className.includes('items-end') &&
                                    el.className.includes('rtl:items-start')) {
                                    return NodeFilter.FILTER_REJECT;
                                }
                                el = el.parentElement;
                            }
                            return NodeFilter.FILTER_ACCEPT;
                        }}
                    );

                    let node;
                    while (node = walker.nextNode()) {
                        const filename = node.textContent.trim();
                        let container = node.parentElement;
                        // Aria-labels of the per-message action toolbar that sits
                        // under every assistant reply. These look like icon-only
                        // buttons and otherwise satisfy the artifact-card heuristic,
                        // so we exclude them explicitly — clicking them switches
                        // model, opens Share, downvotes the response, etc.
                        const MSG_ACTION_ARIAS = new Set([
                            'Copy response', 'Copy', 'Good response', 'Bad response',
                            'Share', 'Switch model', 'More actions', 'Edit',
                            'Read aloud', 'Try again', 'Regenerate'
                        ]);
                        for (let depth = 0; depth < 8 && container; depth++) {
                            const buttons = container.querySelectorAll('button');
                            // Artifact cards have icon-only buttons (SVG icons).
                            const iconButtons = Array.from(buttons).filter(b => {
                                const hasIcon = b.querySelector('img') || b.querySelector('svg');
                                const isSmall = b.textContent.trim().length === 0 ||
                                                b.textContent.trim().length < 5;
                                const aria = (b.getAttribute('aria-label') || '').trim();
                                if (MSG_ACTION_ARIAS.has(aria)) return false;
                                return hasIcon && isSmall;
                            });
                            const isFileCard = container.className &&
                                container.className.includes('rounded-2xl') &&
                                container.className.includes('my-4');
                            if (iconButtons.length >= 1 || isFileCard) {
                                // Dedup by button-set: if any of these icon
                                // buttons was already tagged by a previous
                                // text-node match, skip — it's the same card
                                // reached via a different walker path.
                                if (iconButtons.some(b => b.hasAttribute('data-artifact-btn'))) {
                                    break;
                                }
                                const cardIdx = artifactsOut.length;
                                // Tag EVERY icon button — we don't know which
                                // one is download (icons are sprite-hashed and
                                // classes don't disambiguate in pro mode), so
                                // Python will try each in turn.
                                const buttonMetas = iconButtons.map((b, i) => {
                                    const id = 'art-' + cardIdx + '-btn-' + i;
                                    b.setAttribute('data-artifact-btn', id);
                                    const useEl = b.querySelector('use');
                                    const rect = b.getBoundingClientRect();
                                    return {
                                        id: id,
                                        ariaLabel: b.getAttribute('aria-label') || '',
                                        title: b.getAttribute('title') || '',
                                        spriteHref: useEl ? (useEl.getAttribute('href') ||
                                                             useEl.getAttribute('xlink:href') || '') : '',
                                        classes: b.className || '',
                                        rectX: Math.round(rect.x),
                                        rectY: Math.round(rect.y),
                                        rectW: Math.round(rect.width),
                                        rectH: Math.round(rect.height),
                                        isRoundedFull: b.className.includes('rounded-full'),
                                        innerHtml: b.innerHTML.substring(0, 300),
                                    };
                                });
                                const displayName = filename.includes('.xls')
                                    ? filename : 'ai_attempt.xlsx';
                                artifactsOut.push({
                                    filename: displayName,
                                    buttons: buttonMetas,
                                    containerHtml: container.outerHTML.substring(0, 1500),
                                    foundVia: root === document.body ? 'document' : 'article',
                                });
                                break;
                            }
                            container = container.parentElement;
                        }
                    }
                }

                // Find ChatGPT response containers (try current DOM, then legacy)
                let responseElements = Array.from(
                    document.querySelectorAll("[data-message-author-role='assistant']")
                );
                if (responseElements.length === 0) {
                    responseElements = Array.from(document.querySelectorAll('article'))
                        .filter(a => {
                            const h6 = a.querySelector('h6');
                            return h6 && h6.textContent.includes('ChatGPT said:');
                        });
                }
                const totalAssistant = responseElements.length;
                const newArticles = responseElements.slice(baseline);
                const artifacts = [];
                const seenCards = new Set();

                // Pass 1: scan assistant articles after the baseline.
                for (const article of newArticles) {
                    scanRoot(article, artifacts, seenCards);
                }

                // Pass 2 (fallback): if nothing found, scan entire document body.
                // Pro mode / canvas sometimes renders file cards outside the
                // assistant article (e.g. side panel, attachment tray).
                let fallbackUsed = false;
                if (artifacts.length === 0) {
                    fallbackUsed = true;
                    scanRoot(document.body, artifacts, seenCards);
                }

                // Diagnostics: where do file-keyword text nodes live on the page?
                const sightings = [];
                const diagWalker = document.createTreeWalker(
                    document.body,
                    NodeFilter.SHOW_TEXT,
                    { acceptNode: (node) => {
                        const text = node.textContent.trim();
                        if (!text) return NodeFilter.FILTER_REJECT;
                        return FILE_KEYWORDS.some(kw => text.includes(kw))
                            ? NodeFilter.FILTER_ACCEPT
                            : NodeFilter.FILTER_REJECT;
                    }}
                );
                let dn;
                while ((dn = diagWalker.nextNode()) && sightings.length < 10) {
                    sightings.push({
                        text: dn.textContent.trim().substring(0, 120),
                        ancestors: ancestorChain(dn.parentElement),
                    });
                }

                return {
                    artifacts,
                    diagnostics: {
                        totalAssistantArticles: totalAssistant,
                        baselineSkipped: baseline,
                        newArticlesScanned: newArticles.length,
                        fallbackUsed,
                        fileKeywordSightings: sightings,
                    },
                };
            }""",
                baseline,
            )

            artifact_info = scan_result["artifacts"]
            diag = scan_result["diagnostics"]
            logger.info(
                f"Found {len(artifact_info)} artifact preview card(s) "
                f"(fallback={diag['fallbackUsed']}, "
                f"assistant_articles={diag['totalAssistantArticles']}, "
                f"new_scanned={diag['newArticlesScanned']})"
            )
            if not artifact_info:
                sightings = diag["fileKeywordSightings"]
                if sightings:
                    logger.warning(
                        f"No cards matched, but {len(sightings)} file-keyword "
                        f"text node(s) exist on page. Ancestor chains:"
                    )
                    for s in sightings:
                        logger.warning(f"  '{s['text']}' in {s['ancestors']}")
                else:
                    logger.warning(
                        "No file-keyword text nodes found anywhere on page — "
                        "response may not have produced a file yet."
                    )

            # Set up CDP download behavior so Chrome saves files to our
            # directory.  page.expect_download() does NOT work on CDP
            # connections — Chrome handles downloads natively.
            download_path.mkdir(parents=True, exist_ok=True)
            use_cdp_download = False
            try:
                cdp = await self.page.context.new_cdp_session(self.page)
                await cdp.send(
                    "Browser.setDownloadBehavior",
                    {
                        "behavior": "allowAndName",
                        "downloadPath": str(download_path.resolve()),
                        "eventsEnabled": True,
                    },
                )
                use_cdp_download = True
                logger.info(f"CDP download path: {download_path.resolve()}")
            except Exception as e:
                logger.info(
                    f"CDP download setup failed ({e}), using Playwright fallback"
                )

            # Per-button click budget. Each card may have N icon buttons and
            # we try them in order; the REAL download button is the first one
            # that causes a new file to appear within this window.
            per_button_wait_sec = 6

            for info in artifact_info:
                filename = info["filename"]
                card_html = info.get("containerHtml", "")
                buttons = info.get("buttons", [])
                found_via = info.get("foundVia", "?")
                logger.info(
                    f"Artifact card for {filename} (via {found_via}) has "
                    f"{len(buttons)} icon button(s):"
                )
                for b in buttons:
                    logger.info(
                        f"  - {b['id']} rounded_full={b['isRoundedFull']} "
                        f"aria={b['ariaLabel']!r} title={b['title']!r} "
                        f"sprite={b['spriteHref']!r} "
                        f"rect=({b['rectX']},{b['rectY']},{b['rectW']}x{b['rectH']})"
                    )

                if not buttons:
                    logger.warning(
                        f"No icon buttons for {filename}. Card DOM: {card_html}"
                    )
                    continue

                # Ordering priority:
                #   0. Button whose SVG sprite href matches a known download
                #      icon fragment. Sprite IDs are stable *within* a ChatGPT
                #      build but rotate on redeploys, so we maintain a list of
                #      known hints. Update DOWNLOAD_SPRITE_HINTS when a new
                #      build rolls out and the log shows a different sprite.
                #   1. rounded-full icon buttons (broad fallback — download is
                #      almost always styled as a round icon button).
                #   2. anything else.
                DOWNLOAD_SPRITE_HINTS = ["#1a3695"]

                def button_rank(b):
                    sprite = b.get("spriteHref", "") or ""
                    if any(hint in sprite for hint in DOWNLOAD_SPRITE_HINTS):
                        return 0
                    if b.get("isRoundedFull"):
                        return 1
                    return 2

                ordered = sorted(buttons, key=button_rank)

                saved_path: Optional[Path] = None
                tried_button_ids = []
                for btn_meta in ordered:
                    btn_id = btn_meta["id"]
                    tried_button_ids.append(btn_id)
                    btn = self.page.locator(f'[data-artifact-btn="{btn_id}"]')
                    if await btn.count() == 0:
                        logger.info(f"  {btn_id}: locator missing, skipping")
                        continue

                    try:
                        await btn.scroll_into_view_if_needed(timeout=3000)
                    except Exception:
                        pass

                    if use_cdp_download:
                        files_before = set(download_path.iterdir())
                        try:
                            await btn.click(force=True, timeout=5000)
                        except Exception as click_err:
                            logger.info(
                                f"  {btn_id}: click(force=True) failed "
                                f"({click_err}); dispatching DOM click"
                            )
                            try:
                                await btn.dispatch_event("click")
                            except Exception as de:
                                logger.info(f"  {btn_id}: dispatch_event failed ({de})")
                                continue

                        # Wait per-button for a new completed file.
                        deadline = (
                            asyncio.get_event_loop().time() + per_button_wait_sec
                        )
                        new_file = None
                        while asyncio.get_event_loop().time() < deadline:
                            new_files = set(download_path.iterdir()) - files_before
                            complete = [
                                f for f in new_files
                                if not f.name.endswith(".crdownload")
                            ]
                            if complete:
                                new_file = complete[0]
                                break
                            await asyncio.sleep(0.3)

                        if new_file:
                            target = download_path / filename
                            if new_file.name != filename:
                                new_file.rename(target)
                                new_file = target
                            logger.info(
                                f"  {btn_id}: triggered download -> {new_file}"
                            )
                            saved_path = new_file
                            break
                        else:
                            logger.info(
                                f"  {btn_id}: no file within {per_button_wait_sec}s"
                            )
                            # Clicking a non-download button (preview, expand)
                            # may open a canvas/modal that occludes the
                            # remaining buttons. Dismiss before the next try.
                            try:
                                await self.page.keyboard.press("Escape")
                                await asyncio.sleep(0.3)
                            except Exception:
                                pass
                    else:
                        # Playwright download event path (non-CDP connections)
                        try:
                            async with self.page.expect_download(
                                timeout=per_button_wait_sec * 1000
                            ) as dl_info:
                                try:
                                    await btn.click(force=True, timeout=5000)
                                except Exception:
                                    await btn.dispatch_event("click")
                            download = await dl_info.value
                            target = download_path / filename
                            await download.save_as(str(target))
                            logger.info(
                                f"  {btn_id}: triggered download -> {target}"
                            )
                            saved_path = target
                            break
                        except Exception as e:
                            logger.info(f"  {btn_id}: no download event ({e})")
                            continue

                if saved_path:
                    downloaded.append(str(saved_path))
                else:
                    logger.warning(
                        f"None of {tried_button_ids} produced a download for "
                        f"{filename}. Full card DOM: {card_html}"
                    )

            if downloaded:
                return downloaded

            # Strategy 2: Fallback — look for sandbox download links in NEW articles only.
            # ChatGPT agent mode sometimes produces download links for files
            # saved to /mnt/data/ instead of inline preview cards.
            logger.info("No preview cards found, trying sandbox download links...")
            download_links = await self.page.evaluate(
                """(baseline) => {
                const allArticles = Array.from(document.querySelectorAll('article'));
                const chatgptArticles = allArticles.filter(a => {
                    const h6 = a.querySelector('h6');
                    return h6 && h6.textContent.includes('ChatGPT said:');
                });
                const newArticles = chatgptArticles.slice(baseline);
                const links = [];
                for (const article of newArticles) {
                    const articleLinks = Array.from(article.querySelectorAll('a[href*="sandbox"]'));
                    for (const a of articleLinks) {
                        if (a.href && (a.href.includes('.xlsx') || a.href.includes('.xls'))) {
                            links.push({ href: a.href, text: a.textContent.trim() });
                        }
                    }
                }
                return links;
            }""",
                baseline,
            )

            for link_info in download_links:
                try:
                    logger.info(f"Trying sandbox link: {link_info['text']}")
                    link = self.page.locator(
                        f'a[href*="sandbox"]:has-text("{link_info["text"]}")'
                    )

                    if use_cdp_download:
                        files_before = set(download_path.iterdir())
                        await link.first.click()

                        deadline = asyncio.get_event_loop().time() + timeout / 1000
                        save_path = None
                        while asyncio.get_event_loop().time() < deadline:
                            current_files = set(download_path.iterdir())
                            new_files = current_files - files_before
                            complete = [
                                f
                                for f in new_files
                                if not f.name.endswith(".crdownload")
                            ]
                            if complete:
                                save_path = complete[0]
                                break
                            await asyncio.sleep(0.5)

                        if save_path:
                            logger.info(f"Downloaded via sandbox link: {save_path}")
                            downloaded.append(str(save_path))
                        else:
                            logger.warning(
                                f"Sandbox download timeout for {link_info['text']}"
                            )
                    else:
                        async with self.page.expect_download(
                            timeout=timeout
                        ) as dl_info:
                            await link.first.click()
                        download = await dl_info.value
                        save_path = download_path / download.suggested_filename
                        await download.save_as(str(save_path))
                        logger.info(f"Downloaded via sandbox link: {save_path}")
                        downloaded.append(str(save_path))

                except Exception as e:
                    logger.warning(
                        f"Sandbox download failed for {link_info['text']}: {e}"
                    )
                    continue

            if not downloaded:
                logger.warning(
                    "No artifacts downloaded (no preview cards or sandbox links found)"
                )

        except Exception as e:
            logger.error(f"Artifact download failed: {e}")

        return downloaded

    async def get_conversation_history(self) -> list[dict]:
        """Extract conversation as list of message dicts."""
        messages = []
        try:
            articles = self.page.locator("article")
            count = await articles.count()

            for i in range(count):
                article = articles.nth(i)
                text = await article.inner_text()

                # Determine role from heading
                user_heading = article.locator('h5:has-text("You said:")')
                if await user_heading.count() > 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": text.replace("You said:\n", "").strip(),
                        }
                    )
                else:
                    assistant_heading = article.locator('h6:has-text("ChatGPT said:")')
                    if await assistant_heading.count() > 0:
                        messages.append(
                            {
                                "role": "assistant",
                                "content": text.replace("ChatGPT said:\n", "").strip(),
                            }
                        )
        except Exception as e:
            logger.error(f"Conversation history extraction failed: {e}")

        return messages

    async def process_all_prompts(self, files_to_upload: list = None) -> bool:
        """Process all prompts: upload files, enable features, send prompts, wait."""
        prompts = self.config.get("prompts", [])
        if not prompts:
            logger.error("No prompts configured")
            return False

        # Upload files if provided (engine may have already uploaded in Phase 1)
        if files_to_upload:
            if not await self.upload_files(files_to_upload):
                logger.error("File upload failed")
                return False

        # Process each prompt
        for i, prompt in enumerate(prompts, 1):
            logger.info(f"Processing prompt {i}/{len(prompts)}")

            if not await self.submit_prompt(prompt, i):
                logger.error(f"Failed to submit prompt {i}")
                return False

            response = await self.wait_for_response(i)
            if response is None:
                logger.error(f"No response for prompt {i}")
                return False

            self.messages.append(
                ConversationMessage(
                    role="user", content=prompt, timestamp=datetime.now()
                )
            )
            self.messages.append(
                ConversationMessage(
                    role="assistant", content=response, timestamp=datetime.now()
                )
            )
            self.current_response_count += 1

        logger.info(f"All {len(prompts)} prompt(s) processed successfully")
        return True
