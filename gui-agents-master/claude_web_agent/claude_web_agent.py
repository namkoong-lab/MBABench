"""
Claude Web Agent - Automate interactions with Claude.ai web interface.

This agent handles:
1. Navigating to claude.ai/new
2. Handling authentication (if needed)
3. Enabling Extended Thinking and Web Search (configurable)
4. Submitting prompts and capturing responses
5. File attachments
6. Response extraction for grading

Configuration options (in claude_web section):
    mode: "chat" | "cowork" (default: "chat") - The Chat/Cowork toggle on
        the home/project surface. Asserted every navigation in both
        directions because the selection persists across sessions.
    cowork_approval: "manual" | "auto" | "skip" (default: "auto") - Cowork's
        action-approval setting; "manual" (the UI default) pauses for every
        action and stalls unattended runs. Cowork mode only.
    model: str | null - Config value mapped to a dropdown label (see
        MODEL_LABELS, e.g. fable_5, opus_4_6). null keeps the session default.
    effort: str | null - Reasoning effort: low|medium|high|xhigh|max.
        null keeps the session default.
    enable_extended_thinking: bool (default: True) - Desired thinking-switch
        state. Applied only for models that expose the switch (Opus family);
        a successful no-op for models that don't (Fable 5).
    enable_web_search: bool (default: False) - Enable Web Search capability
"""

import asyncio
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

from claude_web_agent.web_agent import WebAgent, WebAgentState, ConversationMessage

logger = logging.getLogger(__name__)


def _sanitize_for_filename(name: str) -> str:
    """Card titles → safe filenames (spaces to underscores, strip the rest)."""
    import re as _re

    return _re.sub(r"[^a-zA-Z0-9._-]", "", name.replace(" ", "_"))


@dataclass
class TaskResult:
    """Result of running a task through Claude.ai."""

    task_name: str
    success: bool
    messages: list  # List of ConversationMessage
    start_time: datetime
    end_time: datetime
    error_msg: Optional[str] = None

    @property
    def duration_seconds(self) -> float:
        return (self.end_time - self.start_time).total_seconds()


class ClaudeWebAgent(WebAgent):
    """
    Agent for automating Claude.ai web interface.

    This agent uses Playwright to:
    1. Navigate to claude.ai/new
    2. Submit prompts (with optional file attachments)
    3. Wait for and capture Claude's responses
    4. Extract conversation history for grading
    """

    CLAUDE_BASE_URL = "https://claude.ai"
    CLAUDE_NEW_CHAT_URL = "https://claude.ai/new"

    # Selectors for Claude.ai web interface
    SELECTORS = {
        # Input field — the composer is a TipTap/ProseMirror contenteditable
        # (2026-07 UI; the old [data-placeholder] attribute is gone).
        "chat_input": 'div[contenteditable="true"].ProseMirror',
        "chat_input_alt": 'div[enterkeyhint="enter"]',
        "chat_textarea": 'fieldset div[contenteditable="true"]',
        # Send button
        "send_button": 'button[aria-label="Send message"]',
        "send_button_alt": 'button:has(svg[viewBox="0 0 32 32"])',
        # Stop button (visible when generating)
        "stop_button": 'button[aria-label="Stop response"]',
        "stop_button_alt": 'button:has-text("Stop")',
        # Response content (for fallback detection)
        "response_content": '[class*="prose"]',
        # File upload
        "attach_button": 'button[aria-label="Add files, connectors, and more"]',
        "file_input": 'input[type="file"]',
        # Response elements
        "assistant_message": "div[data-is-streaming]",
        "message_container": 'div[class*="message"]',
        # Auth elements
        "login_button": 'a:has-text("Log in")',
        "email_input": 'input[type="email"]',
        # Model selector
        "model_selector": 'button[data-testid="model-selector-dropdown"]',
        # Extended thinking button (clock icon)
        "extended_thinking_button": 'button[aria-label="Extended thinking"]',
        # Toggle menu button (+ button that opens dropdown)
        # Claude.ai renamed this from "Toggle menu" to "Add files, connectors, and more"
        "toggle_menu_button": 'button[aria-label="Add files, connectors, and more"]',
        # Web search checkbox in the dropdown menu
        "web_search_checkbox": 'div[role="menuitemcheckbox"]:has-text("Web search")',
        # Download button in artifact card. The aria-label now carries the
        # filename ("Download <name>"), so prefix-match; exact-match and
        # :text-is() selectors match ZERO elements on the current UI.
        "download_button": 'button[aria-label^="Download "]',
        "download_button_text": 'button:has-text("Download")',
        "download_button_link": 'a:has-text("Download")',
    }

    # Phrases that mean the session hit a usage/rate limit. Text detectors
    # are as fragile as CSS selectors — keep BOTH legacy and current
    # phrasings, and keep each phrase specific enough that a financial
    # model containing the word "limit" can't false-positive.
    USAGE_LIMIT_PHRASES = (
        "You’ve reached your limit",
        "You've reached your limit",
        "reached your usage limit",
        "out of usage credits",
        "plan usage resets",
        "usage will reset",
    )

    # Errors that appear INSIDE an otherwise-complete response. The turn
    # ends normally, so these are not failures to detect and retry — they
    # explain why an artifact is missing and a Continue is needed.
    RESPONSE_ERROR_PHRASES = (
        "API Error",
        "exceeded the 64000 output token maximum",
        "output token maximum",
    )

    def __init__(self, page, config: dict, shutdown_event=None, completion_logger=None):
        """
        Initialize Claude Web Agent.

        Args:
            page: Playwright page instance
            config: Configuration dictionary
            shutdown_event: Optional asyncio.Event for graceful shutdown
            completion_logger: Optional logger for timing/completion tracking
        """
        super().__init__(page, config, shutdown_event, completion_logger)

        # Get agent-specific config
        self.agent_config = config.get("claude_web", {})
        self.max_wait_per_prompt = self.agent_config.get(
            "max_wait_per_prompt_seconds", 1800
        )
        self.check_interval = self.agent_config.get("check_interval_seconds", 2)

        # True once we've observed that the current model has no thinking
        # switch (e.g. Fable 5) — stops per-prompt re-asserts and the
        # mid-generation watcher from reopening the dropdown pointlessly.
        self._thinking_switch_absent = False

    async def navigate_to_new_chat(self) -> bool:
        """
        Navigate to claude.ai/new or project chat to start a fresh conversation.

        If project_id or project_url is configured, opens a new chat within that project.

        Returns:
            True if navigation succeeded
        """
        try:
            # Check for project URL or ID
            project_url = self.agent_config.get("project_url")
            project_id = self.agent_config.get("project_id")

            if project_url:
                # Extract project ID from URL if full URL provided
                if "/project/" in project_url:
                    project_id = (
                        project_url.split("/project/")[1].split("/")[0].split("?")[0]
                    )

            if project_id:
                # Navigate to project's new chat URL
                nav_url = f"{self.CLAUDE_BASE_URL}/project/{project_id}"
                logger.info(f"Navigating to project: {nav_url}...")
            else:
                nav_url = self.CLAUDE_NEW_CHAT_URL
                logger.info(f"Navigating to {nav_url}...")

            await self.page.goto(nav_url, wait_until="domcontentloaded", timeout=60000)
            await asyncio.sleep(3)  # Allow JS to render the chat UI

            # Check if we need to authenticate
            state = await self.get_state()
            if state == WebAgentState.AUTH_REQUIRED:
                logger.warning("Authentication required - please log in manually")
                return False

            logger.info(f"Successfully navigated to Claude.ai (state: {state.value})")

            # Clear any leftover text or files in the chat input
            await self._clear_chat_input()

            # Assert Chat/Cowork mode BEFORE anything touches the composer —
            # toggling re-renders it, and the selection persists across
            # sessions so we can't trust the leftover state.
            if not await self.ensure_mode():
                logger.error("Failed to set Chat/Cowork mode - aborting")
                return False

            # Configure model + effort + thinking. model=None means "keep
            # the session default" (no crash — this used to .lower() None).
            target_model = self.agent_config.get("model")
            target_effort = self.agent_config.get("effort")
            enable_et = self.agent_config.get("enable_extended_thinking", True)
            if not await self.ensure_model_config(
                model=target_model,
                effort=target_effort,
                extended_thinking=enable_et,
            ):
                logger.error("Failed to configure model - aborting")
                return False

            # Set Web Search per config (default: disabled)
            enable_web_search = self.agent_config.get("enable_web_search", False)
            if not await self.ensure_web_search_set(enabled=enable_web_search):
                logger.error("Failed to configure Web Search - aborting")
                return False

            return True

        except Exception as e:
            logger.error(f"Failed to navigate to Claude.ai: {e}")
            return False

    # Model button: exact testid, with text-based fallbacks for the case
    # where Anthropic renames/drops the testid. :has-text is case-insensitive.
    MODEL_BUTTON_SELECTORS = (
        'button[data-testid="model-selector-dropdown"]',
        'button[aria-haspopup="menu"]:has-text("Fable")',
        'button[aria-haspopup="menu"]:has-text("Opus")',
        'button[aria-haspopup="menu"]:has-text("Sonnet")',
        'button[aria-haspopup="menu"]:has-text("Haiku")',
    )

    # claude_web.model config values → dropdown labels (verified live
    # 2026-07-21). The selected model is a top-level menuitemradio; all
    # others live under the "More models" submenu. Unknown config values
    # fall back to underscores→spaces so future models still have a chance.
    MODEL_LABELS = {
        "fable_5": "Fable 5",
        "sonnet_5": "Sonnet 5",
        "haiku_4_5": "Haiku 4.5",
        "opus_4_8": "Opus 4.8",
        "opus_4_7": "Opus 4.7",
        "opus_4_6": "Opus 4.6",
        "opus_3": "Opus 3",
        "sonnet_4_6": "Sonnet 4.6",
    }

    # claude_web.effort config values → data-testid suffixes of the options
    # inside the Effort submenu ([data-testid="effort-menu-trigger"]).
    # UI labels: Low / Medium / High (default) / Extra / Max.
    EFFORT_LEVELS = ("low", "medium", "high", "xhigh", "max")
    EFFORT_TRIGGER_SELECTOR = '[data-testid="effort-menu-trigger"]'

    # claude_web.mode — the Chat/Cowork toggle (verified live 2026-07-21):
    # a Base-UI div[role=radiogroup] of span[role=radio] whose text is an
    # icon glyph + label ("Chat" / "Cowork"), so match with
    # endsWith, never equality. Selection PERSISTS across sessions, so the
    # runner must assert it every task. The toggle exists on the home and
    # project surfaces (not on /new).
    MODE_VALUES = ("chat", "cowork")
    MODE_RADIO_LABELS = {"chat": "Chat", "cowork": "Cowork"}

    # claude_web.cowork_approval — cowork's action-approval dropdown. The
    # trigger button's aria-label is the CURRENT selection's full label
    # (visible text is just "Manual"/"Auto"/…).
    COWORK_APPROVAL_LABELS = {
        "manual": "Manually approve",
        "auto": "Automatically approve",
        "skip": "Skip all approvals",
    }

    # JS-dispatched hover/click sequences. These skip Playwright's
    # "receives pointer events" actionability check, which is essential
    # here: Base-UI submenu flyouts and the artifact preview panel overlay
    # other controls, and a real .hover()/.click() then blocks for the
    # full timeout (see UI_DRIFT_PLAYBOOK §2).
    _JS_HOVER = (
        "el => ['pointerover','pointerenter','mouseover','mouseenter','mousemove']"
        ".forEach(t => el.dispatchEvent("
        "new MouseEvent(t, {bubbles: true, cancelable: true, view: window})))"
    )
    _JS_CLICK = "el => el.click()"

    async def _find_mode_radio(self, label: str):
        """Find the Chat/Cowork radio whose (icon-prefixed) text ends with
        ``label``. Returns an ElementHandle or None."""
        handle = await self.page.evaluate_handle(
            """(label) => Array.from(document.querySelectorAll(
                'span[role="radio"], button[role="radio"]'
            )).filter(el => el.getClientRects().length > 0)
              .find(el => (el.textContent || '').trim().endsWith(label)) || null""",
            label,
        )
        return handle.as_element()

    async def ensure_mode(self) -> bool:
        """Assert the Chat/Cowork toggle matches ``claude_web.mode``.

        mode defaults to "chat". Because the toggle's selection persists
        across sessions, this is asserted on every navigation, in BOTH
        directions — a leftover Cowork selection would otherwise silently
        change what a chat-configured run benchmarks.

        Backward compatible: if the toggle isn't on the page (e.g. /new, or
        an account without the feature), chat mode passes (that's the only
        behavior such a surface has) and cowork mode fails loudly.
        """
        mode = (self.agent_config.get("mode") or "chat").lower()
        if mode not in self.MODE_VALUES:
            logger.error(
                f"Unknown claude_web.mode {mode!r}. Valid: "
                f"{', '.join(self.MODE_VALUES)}"
            )
            return False
        label = self.MODE_RADIO_LABELS[mode]

        radio = await self._find_mode_radio(label)
        if radio is None:
            if mode == "chat":
                logger.info("Chat/Cowork toggle not present — chat-only surface")
                return True
            logger.error(
                "claude_web.mode=cowork but the Chat/Cowork toggle was not "
                "found. It only exists on the home and project surfaces — "
                "set claude_web.project_id, or the UI has drifted."
            )
            return False

        if (await radio.get_attribute("aria-checked")) == "true":
            logger.info(f"Mode already '{mode}'")
        else:
            await radio.evaluate(self._JS_CLICK)
            await asyncio.sleep(1.5)
            # The composer re-renders on toggle — re-find before verifying.
            radio = await self._find_mode_radio(label)
            if radio is None or (await radio.get_attribute("aria-checked")) != "true":
                logger.error(f"Mode toggle to '{mode}' did not verify")
                return False
            logger.info(f"Mode set to '{mode}' (verified)")

        if mode == "cowork":
            return await self.ensure_cowork_approval()
        return True

    async def ensure_cowork_approval(self) -> bool:
        """Set cowork's action-approval mode per ``claude_web.cowork_approval``.

        Defaults to "auto" — the UI default ("manual") pauses for every
        action, which stalls unattended runs.
        """
        target = (self.agent_config.get("cowork_approval") or "auto").lower()
        target_label = self.COWORK_APPROVAL_LABELS.get(target)
        if not target_label:
            logger.error(
                f"Unknown claude_web.cowork_approval {target!r}. Valid: "
                f"{', '.join(self.COWORK_APPROVAL_LABELS)}"
            )
            return False

        # The trigger button's aria-label is the current selection.
        btn = None
        for lbl in self.COWORK_APPROVAL_LABELS.values():
            cand = await self.page.query_selector(f'button[aria-label="{lbl}"]')
            if cand and await cand.is_visible():
                btn = cand
                if lbl == target_label:
                    logger.info(f"Cowork approval already {target!r}")
                    return True
                break
        if btn is None:
            logger.error("Cowork approval dropdown not found — UI drift?")
            return False

        try:
            await btn.evaluate(self._JS_CLICK)
            await asyncio.sleep(1.2)
            item = await self.page.evaluate_handle(
                """(label) => Array.from(document.querySelectorAll(
                    '[role="menuitemradio"]'
                )).filter(el => el.getClientRects().length > 0)
                  .find(el => (el.textContent || '').includes(label)) || null""",
                target_label,
            )
            el = item.as_element()
            if el is None:
                logger.error(f"Approval option {target_label!r} not in menu")
                await self.page.keyboard.press("Escape")
                return False
            await el.evaluate(self._JS_CLICK)
            await asyncio.sleep(1.0)

            verify = await self.page.query_selector(
                f'button[aria-label="{target_label}"]'
            )
            if verify and await verify.is_visible():
                logger.info(f"Cowork approval set to {target!r} (verified)")
                return True
            logger.error(f"Cowork approval {target!r} did not verify")
            return False
        except Exception as e:
            logger.error(f"Error setting cowork approval: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def _get_model_button(self):
        for sel in self.MODEL_BUTTON_SELECTORS:
            try:
                btn = await self.page.query_selector(sel)
                if btn and await btn.is_visible():
                    return btn
            except Exception:
                continue
        return None

    async def _model_button_label(self) -> str:
        """Current model/effort state, e.g. "Model: Fable 5 Max".

        The button's aria-label carries both axes; fall back to visible text.
        """
        btn = await self._get_model_button()
        if not btn:
            return ""
        label = await btn.get_attribute("aria-label")
        if label:
            return label
        return (await btn.text_content()) or ""

    async def _open_model_dropdown(self) -> bool:
        """Open the model selector dropdown if not already open. Retries once."""
        btn = await self._get_model_button()
        if not btn:
            logger.warning("Model selector dropdown button not found")
            return False
        for attempt in (1, 2):
            if (await btn.get_attribute("aria-expanded")) == "true":
                return True
            try:
                # JS dispatch: a settings modal backdrop or preview panel can
                # intercept pointer events and stall a real click.
                await btn.evaluate(self._JS_CLICK)
            except Exception as e:
                logger.warning(f"Dropdown click attempt {attempt} failed: {e}")
                continue
            await asyncio.sleep(0.8)
            menu = await self.page.query_selector('[role="menu"]:visible')
            if menu or (await btn.get_attribute("aria-expanded")) == "true":
                return True
        return False

    async def _close_model_dropdown(self) -> None:
        """Close the dropdown (and any open submenu flyout) via Escape."""
        try:
            for _ in range(3):
                btn = await self._get_model_button()
                if not btn or (await btn.get_attribute("aria-expanded")) != "true":
                    return
                await self.page.keyboard.press("Escape")
                await asyncio.sleep(0.3)
        except Exception:
            pass

    async def _hover_effort_submenu(self) -> bool:
        """Open the Effort flyout inside the model dropdown (dropdown must be
        open). Uses JS-dispatched hover — the flyout itself intercepts real
        pointer events once open."""
        trigger = await self.page.query_selector(self.EFFORT_TRIGGER_SELECTOR)
        if not trigger or not await trigger.is_visible():
            return False
        await trigger.evaluate(self._JS_HOVER)
        await asyncio.sleep(1.0)
        return True

    async def _find_visible_switch(self):
        """Return the visible thinking switch inside the open dropdown, or None.

        Current UI (2026-07): the Thinking switch lives inside the Effort
        flyout and appears only for models that expose manual thinking
        control (e.g. Opus 4.8). Fable 5 has no switch. Older UIs had the
        switch at the dropdown's top level — checked first for compat.
        """
        for sel in ('input[role="switch"]', '[role="switch"]'):
            try:
                for el in await self.page.query_selector_all(sel):
                    if await el.is_visible():
                        return el
            except Exception:
                continue
        return None

    async def _read_switch_state(self, sw) -> Optional[bool]:
        try:
            return await sw.is_checked()
        except Exception:
            pass
        try:
            aria = await sw.get_attribute("aria-checked")
            if aria in ("true", "false"):
                return aria == "true"
        except Exception:
            pass
        return None

    async def _watch_extended_thinking(
        self, stop_event: asyncio.Event, interval: int = 20
    ) -> None:
        """Background watcher — re-enables the thinking switch if claude.ai
        flips it off mid-generation. Polls until ``stop_event`` fires.
        Stops polling permanently once the switch is known to be absent
        (models like Fable 5 manage thinking automatically)."""
        while not stop_event.is_set():
            if getattr(self, "_thinking_switch_absent", False):
                return
            try:
                await self.ensure_extended_thinking(enabled=True)
            except Exception as e:
                logger.debug(f"ET watcher iteration error: {e}")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass

    async def _log_dropdown_dom(self) -> None:
        """Dump a compact summary of dropdown contents for debugging selector drift."""
        try:
            items = await self.page.query_selector_all(
                '[role="menuitem"], [role="menuitemradio"]'
            )
            summaries = []
            for it in items[:12]:
                try:
                    if not await it.is_visible():
                        continue
                    text = ((await it.text_content()) or "").strip()[:60]
                    has_switch = bool(
                        await it.query_selector(
                            'input, [role="switch"], [aria-checked]'
                        )
                    )
                    summaries.append(f"'{text}'{'[switch]' if has_switch else ''}")
                except Exception:
                    continue
            logger.warning(f"Dropdown menuitems seen: {summaries}")
        except Exception:
            pass

    async def ensure_extended_thinking(self, enabled: bool = True) -> bool:
        """Ensure the Thinking switch is ``enabled`` — where the switch exists.

        The switch lives inside the Effort flyout (2026-07 UI) and only for
        models with manual thinking control (Opus family). For models
        without it (Fable 5) this is a successful no-op: thinking is
        governed by the effort level, so absence is not a failure. The
        absence is cached so per-prompt re-asserts and the mid-generation
        watcher stop reopening the dropdown for nothing.

        Returns True when the desired state is reached OR the control does
        not exist for the current model; False on real toggle failures.
        """
        if getattr(self, "_thinking_switch_absent", False):
            return True
        try:
            if not await self._open_model_dropdown():
                return False

            sw = await self._find_visible_switch()
            if not sw:
                # Not at top level — try inside the Effort flyout.
                if await self._hover_effort_submenu():
                    sw = await self._find_visible_switch()

            if not sw:
                self._thinking_switch_absent = True
                logger.info(
                    "No thinking switch for this model — thinking is "
                    "managed by the effort setting; skipping."
                )
                await self._close_model_dropdown()
                return True

            current = await self._read_switch_state(sw)
            if current == enabled:
                logger.info(f"Thinking switch already {'on' if enabled else 'off'}")
                await self._close_model_dropdown()
                return True

            try:
                await sw.evaluate(self._JS_CLICK)
            except Exception as e:
                logger.error(f"Failed to click thinking switch: {e}")
                await self._close_model_dropdown()
                return False
            await asyncio.sleep(0.5)

            final = await self._read_switch_state(sw)
            await self._close_model_dropdown()

            if final == enabled:
                logger.info(f"Thinking switch {'enabled' if enabled else 'disabled'}")
                return True
            logger.error(
                f"Thinking switch toggle failed: wanted={enabled}, got={final}"
            )
            return False
        except Exception as e:
            logger.error(f"Error toggling thinking switch: {e}")
            await self._close_model_dropdown()
            return False

    def _resolve_model_label(self, model: str) -> str:
        """Map a config value (``fable_5``) to its dropdown label (``Fable 5``)."""
        return self.MODEL_LABELS.get(
            model.lower(), model.replace("_", " ").strip()
        )

    async def _click_model_radio(self, label: str) -> bool:
        """Click the menuitemradio whose text starts with ``label``.

        Dropdown must be open. Checks the top level first, then the
        "More models" flyout. Uses JS dispatch throughout (flyouts
        intercept real pointer events)."""
        label_lower = label.lower()

        async def _try_click() -> bool:
            items = await self.page.query_selector_all('[role="menuitemradio"]')
            for item in items:
                try:
                    if not await item.is_visible():
                        continue
                    text = ((await item.text_content()) or "").strip().lower()
                    if text.startswith(label_lower):
                        await item.evaluate(self._JS_CLICK)
                        logger.info(f"Selected model radio: {label}")
                        await asyncio.sleep(1.2)
                        return True
                except Exception:
                    continue
            return False

        if await _try_click():
            return True

        # Expand "More models" and retry.
        more = await self.page.query_selector(
            '[role="menuitem"]:has-text("More models")'
        )
        if more and await more.is_visible():
            await more.evaluate(self._JS_HOVER)
            await asyncio.sleep(1.0)
            if await _try_click():
                return True
        return False

    async def ensure_effort(self, effort: str) -> bool:
        """Set the reasoning-effort level via the Effort flyout.

        Verified against the option's own aria-checked state after
        clicking, not the button label (label wording differs from the
        config keys, e.g. xhigh renders as "Extra").
        """
        effort = effort.lower()
        if effort not in self.EFFORT_LEVELS:
            logger.error(
                f"Unknown claude_web.effort {effort!r}. "
                f"Valid: {', '.join(self.EFFORT_LEVELS)}"
            )
            return False
        option_sel = f'[data-testid="effort-option-{effort}"]'
        try:
            if not await self._open_model_dropdown():
                return False
            if not await self._hover_effort_submenu():
                logger.error(
                    "Effort submenu trigger not found "
                    f"({self.EFFORT_TRIGGER_SELECTOR}) — UI drift?"
                )
                await self._log_dropdown_dom()
                await self._close_model_dropdown()
                return False

            option = await self.page.query_selector(option_sel)
            if not option or not await option.is_visible():
                logger.error(f"Effort option {option_sel} not found — UI drift?")
                await self._log_dropdown_dom()
                await self._close_model_dropdown()
                return False

            if (await option.get_attribute("aria-checked")) == "true":
                logger.info(f"Effort already set to {effort}")
                await self._close_model_dropdown()
                return True

            await option.evaluate(self._JS_CLICK)
            await asyncio.sleep(0.8)

            # Verify by re-opening and re-reading the option state.
            await self._close_model_dropdown()
            if not await self._open_model_dropdown():
                return False
            if not await self._hover_effort_submenu():
                await self._close_model_dropdown()
                return False
            option = await self.page.query_selector(option_sel)
            checked = (
                option is not None
                and (await option.get_attribute("aria-checked")) == "true"
            )
            await self._close_model_dropdown()

            if checked:
                logger.info(f"Effort set to {effort} (verified)")
                return True
            logger.error(f"Effort verification failed for {effort!r}")
            return False
        except Exception as e:
            logger.error(f"Error setting effort: {e}")
            await self._close_model_dropdown()
            return False

    async def ensure_model_config(
        self,
        model: Optional[str] = None,
        effort: Optional[str] = None,
        extended_thinking: bool = True,
    ) -> bool:
        """Configure model, effort, and thinking via the model dropdown.

        Args:
            model: Config value (``fable_5``, ``opus_4_6``, …) or a bare
                keyword (``opus``). ``None`` keeps the session's current
                model (no selection is attempted).
            effort: ``low|medium|high|xhigh|max`` or ``None`` to keep the
                current effort level.
            extended_thinking: Desired thinking-switch state, applied only
                for models that expose the switch (silently skipped for
                models like Fable 5 that don't).

        Returns:
            True if every applicable setting was reached.
        """
        try:
            # Model gates identity/results — a silent mismatch would
            # benchmark the wrong model, so fail loudly at every step.
            if model:
                label = self._resolve_model_label(model)
                logger.info(f"Configuring model={label!r}, effort={effort!r}...")

                current = (await self._model_button_label()).lower()
                if label.lower() not in current:
                    if not await self._open_model_dropdown():
                        return False
                    if not await self._click_model_radio(label):
                        logger.error(f"Model {label!r} not found in dropdown")
                        await self._log_dropdown_dom()
                        await self._close_model_dropdown()
                        return False
                    await self._close_model_dropdown()

                    current = (await self._model_button_label()).lower()
                    if label.lower() not in current:
                        logger.error(
                            f"Model not set after selection: wanted {label!r}, "
                            f"button reads {current!r}"
                        )
                        return False
                logger.info(f"Model verified: {label}")
                # Model may have changed — re-probe the thinking switch.
                self._thinking_switch_absent = False
            else:
                logger.info("claude_web.model not set — keeping session default")

            if effort:
                if not await self.ensure_effort(effort):
                    return False

            if not await self.ensure_extended_thinking(enabled=extended_thinking):
                return False

            return True

        except Exception as e:
            logger.error(f"Error configuring model: {e}")
            await self._close_model_dropdown()
            return False

    async def ensure_web_search_set(self, enabled: bool = False) -> bool:
        """
        Ensure Web Search is set to the desired state.

        Args:
            enabled: True to enable, False to disable (default: False)

        Returns:
            True if Web Search is in the desired state
        """
        desired = "enabled" if enabled else "disabled"
        try:
            logger.info(f"Checking Web Search status (want: {desired})...")

            # First, open the toggle menu (+ button)
            try:
                menu_btn = self.page.get_by_role(
                    "button", name="Add files, connectors, and more"
                )
                if await menu_btn.is_visible(timeout=3000):
                    await menu_btn.click()
                else:
                    logger.warning("Toggle menu button not visible")
                    return False
            except Exception as e:
                logger.debug(f"Role-based menu selector failed: {e}")
                # Fallback to CSS selector
                menu_btn = await self.page.query_selector(
                    self.SELECTORS["toggle_menu_button"]
                )
                if menu_btn and await menu_btn.is_visible():
                    await menu_btn.click()
                else:
                    logger.warning("Toggle menu button not found")
                    return False

            # Wait for the menu to open. [role=menu][data-open] is the
            # Base UI open signal; catches silent click failures.
            try:
                await self.page.wait_for_selector(
                    '[role="menu"][data-open]', timeout=3000
                )
            except Exception:
                logger.debug("Toggle menu did not show [data-open] in time")

            # Now find the Web Search checkbox
            toggled = False
            try:
                web_search = self.page.get_by_role(
                    "menuitemcheckbox", name="Web search"
                )
                if await web_search.is_visible(timeout=2000):
                    is_checked = (
                        await web_search.get_attribute("aria-checked") == "true"
                    )

                    if is_checked == enabled:
                        logger.info(f"Web Search is already {desired}")
                        await self.page.keyboard.press("Escape")
                        return True

                    # Click to toggle
                    await web_search.click()
                    await asyncio.sleep(0.3)
                    toggled = True
            except Exception as e:
                logger.debug(f"Role-based Web Search selector failed: {e}")

            if not toggled:
                # Fallback to CSS selector
                web_search = await self.page.query_selector(
                    self.SELECTORS["web_search_checkbox"]
                )
                if web_search and await web_search.is_visible():
                    is_checked = (
                        await web_search.get_attribute("aria-checked") == "true"
                    )

                    if is_checked == enabled:
                        logger.info(f"Web Search is already {desired}")
                        await self.page.keyboard.press("Escape")
                        return True

                    await web_search.click()
                    await asyncio.sleep(0.3)
                    toggled = True

            if not toggled:
                # Cowork surfaces have no Web search checkbox in the + menu
                # at all (verified live 2026-07-21: only Add files, Take a
                # screenshot, Skills, Add connector). If the desired state
                # is DISABLED, a missing control means there is nothing to
                # disable — succeed. Wanting it ENABLED with no control is
                # still a hard failure.
                if not enabled:
                    logger.info(
                        "Web Search control not present in this menu "
                        "(cowork surface) — nothing to disable"
                    )
                    await self.page.keyboard.press("Escape")
                    return True
                logger.error("Web Search checkbox not found")
                await self.page.keyboard.press("Escape")
                return False

            # Final verification: re-open the menu and re-read aria-checked
            await self.page.keyboard.press("Escape")
            await asyncio.sleep(0.3)
            try:
                menu_btn = self.page.get_by_role(
                    "button", name="Add files, connectors, and more"
                )
                if await menu_btn.is_visible(timeout=3000):
                    await menu_btn.click()
                else:
                    menu_btn_fallback = await self.page.query_selector(
                        self.SELECTORS["toggle_menu_button"]
                    )
                    if menu_btn_fallback and await menu_btn_fallback.is_visible():
                        await menu_btn_fallback.click()
                    else:
                        logger.error(
                            "Could not re-open toggle menu to verify Web Search"
                        )
                        return False
                try:
                    await self.page.wait_for_selector(
                        '[role="menu"][data-open]', timeout=3000
                    )
                except Exception:
                    logger.debug("Toggle menu did not show [data-open] during verify")

                verify_el = self.page.get_by_role("menuitemcheckbox", name="Web search")
                if await verify_el.is_visible(timeout=2000):
                    actual = await verify_el.get_attribute("aria-checked") == "true"
                else:
                    verify_el = await self.page.query_selector(
                        self.SELECTORS["web_search_checkbox"]
                    )
                    if not verify_el:
                        logger.error("Web Search checkbox missing during verification")
                        await self.page.keyboard.press("Escape")
                        return False
                    actual = await verify_el.get_attribute("aria-checked") == "true"
            finally:
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass

            if actual != enabled:
                logger.error(
                    f"Web Search mismatch — wanted {desired}, "
                    f"observed={'enabled' if actual else 'disabled'}"
                )
                return False

            logger.info(f"Web Search {desired} successfully (verified)")
            return True

        except Exception as e:
            logger.error(f"Error setting Web Search: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception:
                pass
            return False

    async def ensure_features_enabled(self) -> bool:
        """Configure mode, model, effort, thinking, and web search per config."""
        if not await self.ensure_mode():
            return False
        target_model = self.agent_config.get("model")
        target_effort = self.agent_config.get("effort")
        enable_et = self.agent_config.get("enable_extended_thinking", True)
        model_ok = await self.ensure_model_config(
            model=target_model,
            effort=target_effort,
            extended_thinking=enable_et,
        )
        enable_ws = self.agent_config.get("enable_web_search", False)
        ws_ok = await self.ensure_web_search_set(enabled=enable_ws)
        return model_ok and ws_ok

    async def get_state(self) -> WebAgentState:
        """
        Determine current state of Claude.ai interface.

        Returns:
            WebAgentState enum value
        """
        try:
            # Check for rate/usage limiting (text phrases — see
            # USAGE_LIMIT_PHRASES for why both old and new wordings are kept)
            try:
                limited = await self.page.evaluate(
                    "(phrases) => { const t = document.body.innerText || '';"
                    " return phrases.some(p => t.includes(p)); }",
                    list(self.USAGE_LIMIT_PHRASES),
                )
                if limited:
                    return WebAgentState.RATE_LIMITED
            except Exception:
                pass

            # Check for login requirement
            login_btn = await self.page.query_selector(self.SELECTORS["login_button"])
            if login_btn and await login_btn.is_visible():
                return WebAgentState.AUTH_REQUIRED

            # Check for stop button (means Claude is generating)
            for selector in [
                self.SELECTORS["stop_button"],
                self.SELECTORS["stop_button_alt"],
            ]:
                try:
                    stop_btn = await self.page.query_selector(selector)
                    if stop_btn and await stop_btn.is_visible():
                        return WebAgentState.RUNNING
                except Exception:
                    continue

            # Check if input is available
            for selector in [
                self.SELECTORS["chat_input"],
                self.SELECTORS["chat_input_alt"],
                self.SELECTORS["chat_textarea"],
            ]:
                try:
                    input_field = await self.page.query_selector(selector)
                    if input_field and await input_field.is_visible():
                        return WebAgentState.READY
                except Exception:
                    continue

            return WebAgentState.UNKNOWN

        except Exception as e:
            logger.debug(f"Error getting state: {e}")
            return WebAgentState.UNKNOWN

    async def _find_input_field(self):
        """Find the chat input field."""
        for selector in [
            self.SELECTORS["chat_input"],
            self.SELECTORS["chat_input_alt"],
            self.SELECTORS["chat_textarea"],
        ]:
            try:
                element = await self.page.query_selector(selector)
                if element and await element.is_visible():
                    return element
            except Exception:
                continue
        return None

    async def _clear_chat_input(self):
        """Clear any leftover text or attached files in the chat input."""
        try:
            input_field = await self._find_input_field()
            if not input_field:
                return

            # Clear text content
            text_content = await input_field.text_content()
            if text_content and text_content.strip():
                logger.info("Clearing leftover text from chat input...")
                await input_field.evaluate("el => el.focus()")
                select_all = "Meta+a" if sys.platform == "darwin" else "Control+a"
                await self.page.keyboard.press(select_all)
                await self.page.keyboard.press("Backspace")
                await asyncio.sleep(0.3)

            # Remove any attached file chips (X button inside file-thumbnail)
            try:
                remove_btns = await self.page.query_selector_all(
                    '[data-testid="file-thumbnail"] button.rounded-full'
                )
                for btn in remove_btns:
                    if await btn.is_visible():
                        logger.info("Removing leftover attached file...")
                        await btn.click()
                        await asyncio.sleep(0.3)
            except Exception:
                pass

        except Exception as e:
            logger.debug(f"Error clearing chat input: {e}")

    async def _find_send_button(self):
        """Find the send button."""
        for selector in [
            self.SELECTORS["send_button"],
            self.SELECTORS["send_button_alt"],
        ]:
            try:
                btn = await self.page.query_selector(selector)
                if btn and await btn.is_visible():
                    return btn
            except Exception:
                continue
        return None

    async def _wait_for_attachments(
        self, file_paths: list[str], timeout_sec: int = 900
    ) -> bool:
        """Block until every uploaded file shows up as a finished
        attachment chip in the composer.

        The old code slept a flat ``2 + len(files)`` seconds. That is far
        too short for large files — a 79MB CSV was still uploading when
        the engine submitted, and Claude keeps the Send button disabled
        while attachments are in flight, so the prompt silently never sent
        (observed live 2026-07-22 on task 321 Data-King).
        """
        from pathlib import Path as _Path

        names = [_Path(p).name for p in file_paths]
        deadline = asyncio.get_event_loop().time() + timeout_sec
        last_missing = None
        stable_polls = 0
        while asyncio.get_event_loop().time() < deadline:
            state = await self.page.evaluate(
                """(names) => {
                // Compare on alphanumerics only: the composer renders
                // "FMI Mini Exam - Case Materials.pdf" as "FMI Mini Exam
                // Case Materials.pdf" (separators stripped), so a raw
                // substring match never fires.
                const norm = s => (s || '').toLowerCase().replace(/[^a-z0-9]/g, '');
                // PDF attachments render as preview cards whose filename lives
                // ONLY in element attributes (data-testid / img alt / "Remove
                // <name>" aria-label), NOT in innerText — while xlsx/csv chips
                // DO surface the name as text. Searching innerText alone hangs
                // any multi-file upload that includes a PDF forever at
                // "N/total attached" -> 15min timeout -> retry loop that piles
                // up duplicate attachments (observed 2026-07-24, task 68
                // Vacation-Fun: 1/3 forever while 3 real files sat attached).
                let hay = document.body.innerText;
                for (const el of document.querySelectorAll(
                        '[data-testid],[alt],[aria-label],[title]')) {
                    hay += ' ' + (el.getAttribute('data-testid') || '')
                         + ' ' + (el.getAttribute('alt') || '')
                         + ' ' + (el.getAttribute('aria-label') || '')
                         + ' ' + (el.getAttribute('title') || '');
                }
                const text = norm(hay);
                const missing = names.filter(n => !text.includes(norm(n)));
                return {missing};
            }""",
                names,
            )
            # NOTE: do not gate on progress/aria-busy selectors — the
            # project page carries a permanent [role="progressbar"] div
            # as chrome, which pinned this loop until timeout
            # (observed live 2026-07-22 on task 354).
            if not state["missing"]:
                stable_polls += 1
                if stable_polls >= 2:  # names held steady ~3s
                    return True
            else:
                stable_polls = 0
            if state["missing"] != last_missing:
                last_missing = state["missing"]
                logger.info(
                    f"Waiting for attachments to finish "
                    f"({len(names) - len(state['missing'])}/{len(names)} "
                    f"attached)"
                )
            await asyncio.sleep(3)
        logger.error(
            f"Attachments never finished uploading within {timeout_sec}s: "
            f"{last_missing}"
        )
        return False

    async def upload_files(self, file_paths: list[str]) -> bool:
        """
        Upload files to the current conversation.

        The Claude.ai UI has a two-step flow:
        1. Click the "+" button (aria-label "Add files, connectors, and more")
           which opens a submenu.
        2. Click "Add files or photos" in the submenu, which triggers the
           browser file chooser.

        Falls back to a hidden ``input[type="file"]`` if available.

        Args:
            file_paths: List of file paths to upload

        Returns:
            True if all uploads succeeded
        """
        if not file_paths:
            return True

        try:
            logger.info(f"Uploading {len(file_paths)} file(s)...")

            # Strategy 1: hidden file input (fastest, no UI clicks needed)
            file_input = await self.page.query_selector(self.SELECTORS["file_input"])
            if file_input:
                try:
                    await file_input.set_input_files(file_paths)
                except Exception as e:
                    # Playwright refuses >50MB files over a CDP connection
                    # ("browser not co-located"), even though this Chrome IS
                    # local (hit live 2026-07-21 on an 83MB dataset CSV).
                    # Raw CDP DOM.setFileInputFiles has no size guard.
                    if "larger than 50Mb" not in str(e):
                        raise
                    logger.info(
                        "Files exceed Playwright's 50MB CDP cap — using "
                        "raw CDP DOM.setFileInputFiles"
                    )
                    cdp = await self.page.context.new_cdp_session(self.page)
                    try:
                        doc = await cdp.send("DOM.getDocument")
                        node = await cdp.send("DOM.querySelector", {
                            "nodeId": doc["root"]["nodeId"],
                            "selector": self.SELECTORS["file_input"],
                        })
                        if not node.get("nodeId"):
                            raise RuntimeError(
                                "file input not found via CDP DOM query")
                        await cdp.send("DOM.setFileInputFiles", {
                            "files": [str(p) for p in file_paths],
                            "nodeId": node["nodeId"],
                        })
                    finally:
                        await cdp.detach()
                if not await self._wait_for_attachments(file_paths):
                    return False
                logger.info(f"Uploaded {len(file_paths)} file(s) via file input")
                return True

            # Strategy 2: click "+" button -> "Add files or photos" submenu
            attach_btn = await self.page.query_selector(self.SELECTORS["attach_button"])
            if not attach_btn or not await attach_btn.is_visible():
                logger.error("Could not find attach button (+)")
                return False

            # Click "+" and wait for the menu to actually open.
            # [role=menu][data-open] is the Base UI open signal; catches
            # silent click failures that previously manifested as
            # "could not find Add files or photos".
            await attach_btn.click()
            try:
                await self.page.wait_for_selector(
                    '[role="menu"][data-open]', timeout=3000
                )
            except Exception:
                logger.debug("Attach menu did not show [data-open] in time")

            # Find "Add files or photos" via ARIA role+name (stable across
            # localization / text-content renames), not plain text.
            add_files_item = self.page.get_by_role(
                "menuitem", name="Add files or photos"
            )
            try:
                async with self.page.expect_file_chooser(timeout=5000) as fc:
                    await add_files_item.click(timeout=3000)
                chooser = await fc.value
                await chooser.set_files(file_paths)
            except Exception as e:
                logger.debug(f"Submenu approach failed: {e}")
                # Fallback: try clicking any visible "Add files" text
                try:
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(0.3)
                    await attach_btn.click()
                    await asyncio.sleep(0.5)
                    async with self.page.expect_file_chooser(timeout=5000) as fc:
                        # Try clicking the first menu item with a paperclip icon
                        menu_item = await self.page.query_selector(
                            'div[role="menuitem"]:has-text("file")'
                        )
                        if menu_item:
                            await menu_item.click()
                        else:
                            # Last resort: just click the attach button itself
                            await attach_btn.click()
                    chooser = await fc.value
                    await chooser.set_files(file_paths)
                except Exception as e2:
                    logger.error(f"File upload failed: {e2}")
                    await self.page.keyboard.press("Escape")
                    return False

            # Wait for uploads to complete
            await asyncio.sleep(2 + len(file_paths))
            return True

        except Exception as e:
            logger.error(f"File upload failed: {e}")
            return False

    async def submit_prompt(self, prompt: str, prompt_number: int = 1) -> bool:
        """
        Submit a prompt to Claude.

        Args:
            prompt: The prompt text to submit
            prompt_number: Prompt number for logging

        Returns:
            True if submission succeeded
        """
        try:
            logger.info(f"Submitting prompt #{prompt_number}: {prompt[:100]}...")

            # Find input field
            input_field = await self._find_input_field()
            if not input_field:
                logger.error("Could not find chat input field")
                return False

            # Click-free interaction (UI_DRIFT_PLAYBOOK §2): after an
            # artifact is emitted, claude.ai auto-opens a preview panel
            # OVER the composer. el.focus()/fill()/JS-dispatched clicks
            # skip Playwright's pointer-events actionability check, so an
            # overlay can't stall us for the full timeout. Escape-dismissal
            # is best-effort only — correctness must not depend on it.
            await input_field.evaluate("el => el.focus()")
            await asyncio.sleep(0.3)

            # A distinctive fragment of the prompt, used to verify the
            # message actually LANDED in the conversation (not merely that
            # the composer emptied — see below).
            probe_fragment = " ".join(prompt.split())[:60]

            async def _fill_composer() -> bool:
                """Fill the composer and confirm the text STUCK.

                Cowork re-renders the composer right after a turn
                completes; text filled during that window is silently
                wiped (verified live 2026-07-21: prompt filled → composer
                re-rendered empty → send button never appeared → Enter
                no-oped → 'composer emptied' false-passed). Re-find the
                field and re-fill until the text survives a settle delay.
                """
                nonlocal input_field
                # Post-turn, cowork can LOCK the composer for tens of
                # seconds while it finalizes outputs (checklist, Google
                # Drive sync) — short retries all land inside the lockout
                # (verified live 2026-07-21: 12 rapid attempts over ~45s
                # all wiped). Back off up to ~1.5 min and alternate the
                # input mechanism (fill vs real keyboard typing).
                backoffs = (2, 3, 5, 10, 20, 45)
                for fill_try, settle in enumerate(backoffs):
                    field = await self._find_input_field()
                    if field is not None:
                        input_field = field
                    use_keyboard = fill_try >= 2  # alternate mechanism late
                    try:
                        await input_field.evaluate("el => el.focus()")
                        await input_field.fill("")
                        await asyncio.sleep(0.1)
                        if use_keyboard:
                            await input_field.evaluate("el => el.focus()")
                            await self.page.keyboard.type(prompt, delay=5)
                        else:
                            try:
                                await input_field.fill(prompt)
                            except Exception:
                                await self.page.keyboard.type(prompt, delay=10)
                    except Exception as e:
                        logger.debug(f"Fill attempt {fill_try + 1} error: {e}")
                    await asyncio.sleep(1.5)  # let any re-render happen
                    field = await self._find_input_field()
                    current = (
                        ((await field.text_content()) or "").strip()
                        if field
                        else ""
                    )
                    if current:
                        if field is not None:
                            input_field = field
                        return True
                    # Log composer state for drift diagnosis before waiting.
                    try:
                        state = await self.page.evaluate(
                            """() => {
                            const e = document.querySelector('div[contenteditable="true"]');
                            if (!e) return 'NO COMPOSER IN DOM';
                            return JSON.stringify({
                                editable: e.getAttribute('contenteditable'),
                                ariaDisabled: e.getAttribute('aria-disabled'),
                                visible: e.getClientRects().length > 0,
                                cls: (e.className || '').slice(0, 60),
                            });
                        }"""
                        )
                    except Exception:
                        state = "?"
                    logger.warning(
                        f"Composer text did not stick (attempt "
                        f"{fill_try + 1}, via "
                        f"{'keyboard' if use_keyboard else 'fill'}); "
                        f"state={state}; waiting {settle}s"
                    )
                    await asyncio.sleep(settle)
                return False

            async def _prompt_in_conversation() -> bool:
                try:
                    return await self.page.evaluate(
                        "(frag) => ((document.querySelector('main') || "
                        "document.body).innerText || '')"
                        ".replace(/\\s+/g, ' ').includes(frag)",
                        probe_fragment,
                    )
                except Exception:
                    return False

            # Send — and VERIFY the message actually landed. Two distinct
            # failure modes both look like success under weaker checks:
            # (a) Enter no-ops and the text sits in the composer forever;
            # (b) a re-render wipes the composer so it LOOKS sent but the
            # message never entered the conversation. So require BOTH the
            # composer to empty AND the prompt text to appear in <main>.
            sent = False
            for send_attempt in range(3):
                if not await _fill_composer():
                    logger.error("Could not keep text in the composer")
                    continue

                send_btn = await self._find_send_button()
                if not send_btn:
                    # Short wait only: the cowork PROJECT-page composer has
                    # no Send button at all (Enter is its real submit), so
                    # a long wait just burns time; later attempts wait
                    # longer in case the session composer is busy.
                    wait_s = 10 if send_attempt == 0 else 45
                    deadline = asyncio.get_event_loop().time() + wait_s
                    while asyncio.get_event_loop().time() < deadline:
                        send_btn = await self._find_send_button()
                        if send_btn:
                            break
                        await asyncio.sleep(1.5)
                if send_btn:
                    await send_btn.evaluate(self._JS_CLICK)
                    logger.info("Clicked send button (JS dispatch)")
                else:
                    logger.warning(
                        "Send button never appeared — trying Enter as last resort"
                    )
                    try:
                        await input_field.evaluate("el => el.focus()")
                    except Exception:
                        pass
                    await self.page.keyboard.press("Enter")

                deadline = asyncio.get_event_loop().time() + 15
                while asyncio.get_event_loop().time() < deadline:
                    if await _prompt_in_conversation():
                        field = await self._find_input_field()
                        leftover = (
                            ((await field.text_content()) or "").strip()
                            if field
                            else ""
                        )
                        if not leftover:
                            sent = True
                            break
                    await asyncio.sleep(1)
                if sent:
                    break
                logger.warning(
                    f"Message not confirmed in conversation after send "
                    f"attempt {send_attempt + 1} — retrying"
                )

            if not sent:
                logger.error("Prompt never left the composer — send failed")
                return False

            # Record user message
            self.messages.append(
                ConversationMessage(
                    role="user",
                    content=prompt,
                    timestamp=datetime.now(),
                )
            )

            return True

        except Exception as e:
            logger.error(f"Failed to submit prompt: {e}")
            return False

    async def wait_for_response(self, prompt_number: int = 1) -> Optional[str]:
        """
        Wait for Claude to finish responding and extract the response.

        Args:
            prompt_number: Prompt number for logging

        Returns:
            The response text, or None if failed
        """
        logger.info(f"Waiting for response to prompt #{prompt_number}...")

        elapsed = 0
        saw_running = False
        # Persistence gate for rate-limit fast-fail: a transient toast (or
        # false-positive text match) must not abandon a healthy in-flight
        # response. Only fail after several consecutive sightings.
        rate_limited_streak = 0
        RATE_LIMIT_STREAK_REQUIRED = 3

        while elapsed < self.max_wait_per_prompt:
            # Check for shutdown
            if self.shutdown_event and self.shutdown_event.is_set():
                logger.warning("Shutdown signal received")
                return None

            await asyncio.sleep(self.check_interval)
            elapsed += self.check_interval

            state = await self.get_state()

            if state == WebAgentState.RUNNING:
                saw_running = True
                rate_limited_streak = 0
                if elapsed % 10 == 0:
                    logger.info(f"   [{elapsed}s] Claude is generating...")
                continue

            if state == WebAgentState.RATE_LIMITED:
                rate_limited_streak += 1
                logger.warning(
                    f"Rate/usage-limit phrase on page "
                    f"({rate_limited_streak}/{RATE_LIMIT_STREAK_REQUIRED} "
                    f"consecutive)"
                )
                if rate_limited_streak >= RATE_LIMIT_STREAK_REQUIRED:
                    logger.error("Rate limit persisted — aborting wait")
                    return None
                continue
            rate_limited_streak = 0

            if state == WebAgentState.READY:
                if not saw_running:
                    # Haven't seen Claude start yet via stop button
                    # Fallback: check if response content has appeared
                    try:
                        responses = await self.page.query_selector_all(
                            self.SELECTORS["response_content"]
                        )
                        if len(responses) > 1:  # More than just the initial prompt
                            saw_running = True
                            logger.info(
                                f"   [{elapsed}s] Detected response content (fallback)"
                            )
                        elif elapsed % 10 == 0:
                            logger.info(
                                f"   [{elapsed}s] Waiting for Claude to start..."
                            )
                            continue
                    except Exception:
                        if elapsed % 10 == 0:
                            logger.info(
                                f"   [{elapsed}s] Waiting for Claude to start..."
                            )
                        continue

                    if not saw_running:
                        continue

                # Claude finished!
                await asyncio.sleep(1)  # Brief stabilization

                # Verify still ready
                final_state = await self.get_state()
                if final_state == WebAgentState.READY:
                    logger.info(f"Prompt #{prompt_number} completed after {elapsed}s")

                    # Extract response
                    response = await self._extract_last_response()
                    if response:
                        self.messages.append(
                            ConversationMessage(
                                role="assistant",
                                content=response,
                                timestamp=datetime.now(),
                            )
                        )
                    return response

        logger.error(f"Timeout waiting for response to prompt #{prompt_number}")
        return None

    async def _extract_last_response(self) -> Optional[str]:
        """
        Extract the last assistant response from the page.

        Returns:
            The response text or None
        """
        try:
            # Claude.ai uses various selectors for messages
            # Try to find assistant messages
            selectors = [
                'div[data-is-streaming="false"]',
                "div.font-claude-message",
                'div[class*="prose"]',
                "article div",
            ]

            for selector in selectors:
                try:
                    elements = await self.page.query_selector_all(selector)
                    if elements:
                        # Get the last one
                        last_el = elements[-1]
                        text = await last_el.text_content()
                        if text and len(text.strip()) > 0:
                            return text.strip()
                except Exception:
                    continue

            # Fallback: try to extract all text from conversation area
            try:
                conversation = await self.page.query_selector("main")
                if conversation:
                    text = await conversation.text_content()
                    return text.strip() if text else None
            except Exception:
                pass

            return None

        except Exception as e:
            logger.debug(f"Error extracting response: {e}")
            return None

    async def process_all_prompts(self, files_to_upload: list = None) -> bool:
        """
        Process all prompts from config sequentially.

        Args:
            files_to_upload: Optional list of files to upload before prompts

        Returns:
            True if all prompts completed successfully
        """
        prompts = self.config.get("prompts", [])
        if not prompts:
            logger.error("No prompts found in config")
            return False

        if isinstance(prompts, str):
            prompts = [prompts]

        # Upload files first if provided
        if files_to_upload:
            logger.info(f"Uploading {len(files_to_upload)} file(s) before prompts...")
            if not await self.upload_files(files_to_upload):
                logger.error("File upload failed")
                return False
            await asyncio.sleep(5)  # Let uploads settle before first prompt

        # Process each prompt
        logger.info(f"Processing {len(prompts)} prompt(s)...")

        enable_et = self.agent_config.get("enable_extended_thinking", True)

        for i, prompt in enumerate(prompts, 1):
            # Check for shutdown
            if self.shutdown_event and self.shutdown_event.is_set():
                logger.warning(f"Shutdown signal before prompt #{i}")
                return False

            # Pause between prompts (not before the first one)
            if i > 1:
                logger.info("Pausing 5s before next prompt...")
                await asyncio.sleep(5)

            logger.info(f"\n{'='*60}")
            logger.info(f"PROMPT {i}/{len(prompts)}")
            logger.info(f"{'='*60}")

            # Re-assert Extended thinking before every submission — claude.ai
            # resets the toggle on each turn, so we must re-enable each time.
            if not await self.ensure_extended_thinking(enabled=enable_et):
                logger.warning(
                    f"Could not verify Extended thinking state before prompt #{i}"
                )

            # Start prompt logging
            if self.completion_logger:
                self.completion_logger.start_prompt(prompt)

            # Submit prompt
            if not await self.submit_prompt(prompt, i):
                logger.error(f"Failed to submit prompt #{i}")
                if self.completion_logger:
                    self.completion_logger.end_prompt(success=False)
                return False

            # Claude.ai flips the Extended thinking switch off mid-stream;
            # run a watcher during wait_for_response that re-enables it.
            et_stop = asyncio.Event()
            et_task = (
                asyncio.create_task(self._watch_extended_thinking(et_stop))
                if enable_et
                else None
            )
            try:
                response = await self.wait_for_response(i)
            finally:
                et_stop.set()
                if et_task:
                    try:
                        await asyncio.wait_for(et_task, timeout=5)
                    except (asyncio.TimeoutError, asyncio.CancelledError):
                        et_task.cancel()
            if response is None:
                logger.error(f"Failed to get response for prompt #{i}")
                if self.completion_logger:
                    self.completion_logger.end_prompt(success=False)
                return False

            logger.info(f"Prompt #{i} completed successfully")
            logger.info(f"Response preview: {response[:200]}...")

            # Surface in-response API errors as their own log line. The
            # turn still "completes" (the text is there), so these
            # otherwise hide inside the truncated preview above. Seen live
            # 2026-07-21: the pv9 prompt drove a cowork turn past the
            # 64k output-token cap; the Continue loop recovered it, but
            # nothing in the log said why a Continue was needed.
            for phrase in self.RESPONSE_ERROR_PHRASES:
                if phrase in response:
                    logger.warning(
                        f"Response contains an API error ({phrase!r}) — "
                        f"the turn was cut short; relying on the Continue "
                        f"loop to finish the artifact"
                    )
                    break

            # End prompt logging
            if self.completion_logger:
                self.completion_logger.end_prompt(
                    success=True, response_length=len(response)
                )

        logger.info(f"\nAll {len(prompts)} prompts completed!")
        return True

    async def get_conversation_history(self) -> list[dict]:
        """
        Get the full conversation history.

        Returns:
            List of message dictionaries with 'role' and 'content'
        """
        return [
            {
                "role": msg.role,
                "content": msg.content,
                "timestamp": (
                    msg.timestamp.isoformat()
                    if msg.timestamp
                    else datetime.now().isoformat()
                ),
            }
            for msg in self.messages
        ]

    async def get_last_response(self) -> Optional[str]:
        """Get the last assistant response."""
        for msg in reversed(self.messages):
            if msg.role == "assistant":
                return msg.content
        return None

    async def _setup_cdp_downloads(self, download_path: Path):
        """Route downloads through Chrome's native download manager.

        Claude's .xlsx artifacts are client-side blobs, and Playwright's
        ``expect_download()`` CANNOT capture a blob download over a CDP
        connection — it resolves and writes a 0-byte file, deterministically.
        Chrome's own download manager writes the full file (UI_DRIFT_PLAYBOOK
        §1a). Returns the CDP session or None if setup failed (non-CDP
        connections fall back to expect_download, which still works for
        plain HTTP downloads).
        """
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
            logger.info(f"CDP download path: {download_path.resolve()}")
            return cdp
        except Exception as e:
            logger.info(f"CDP download setup failed ({e}); using expect_download")
            return None

    async def _poll_for_new_file(
        self, download_path: Path, files_before: set, wait_sec: float = 20.0
    ) -> Optional[Path]:
        """Wait for a new, complete, non-empty file to appear in the dir."""
        deadline = asyncio.get_event_loop().time() + wait_sec
        while asyncio.get_event_loop().time() < deadline:
            fresh = [
                f
                for f in set(download_path.iterdir()) - files_before
                if not f.name.endswith(".crdownload")  # Chrome in-progress marker
                and f.is_file()
                and f.stat().st_size > 0  # 0 bytes = the blob trap, not a file
            ]
            if fresh:
                return fresh[0]
            await asyncio.sleep(0.3)
        return None

    @staticmethod
    def _artifact_target_name(aria_label: Optional[str], fallback: str) -> str:
        """Derive a filename from 'Download <name>' aria-labels."""
        if aria_label and aria_label.startswith("Download "):
            name = aria_label[len("Download "):].strip()
            if name:
                return name
        return fallback

    async def _find_download_buttons(self) -> list[tuple]:
        """Collect visible in-chat download buttons as (handle, label) pairs.

        Selector order matters: aria-prefix first (current UI ships
        aria-label="Download <filename>" with the visible text in a nested
        span, which breaks both exact-aria and :text-is matching), scoped
        to <main> to skip preview-panel duplicates. Deduped by aria-label —
        sibling Download buttons are usually the same artifact rendered as
        version cards (v1/v2/v3).
        """
        for selector in [
            'main button[aria-label^="Download "]',
            'button[aria-label^="Download "]',
            'main button:has-text("Download")',
            'button:has-text("Download")',
        ]:
            try:
                btns = await self.page.query_selector_all(selector)
            except Exception:
                continue
            found = []
            seen_labels = set()
            for b in btns:
                try:
                    if not await b.is_visible():
                        continue
                    label = await b.get_attribute("aria-label")
                    if label and label in seen_labels:
                        continue
                    if label:
                        seen_labels.add(label)
                    found.append((b, label))
                except Exception:
                    continue
            if found:
                logger.info(
                    f"Found {len(found)} download button(s) via: {selector}"
                )
                return found
        return []

    # Cowork surface: output files render as cards with a "Google Drive"
    # split-button whose chevron (aria-label below) opens a menu containing
    # a Download item (verified live 2026-07-21). There are NO inline
    # aria-"Download <name>" buttons on this surface.
    COWORK_CARD_MENU_SELECTOR = 'button[aria-label="More ways to open"]'

    async def _download_via_cowork_cards(self, download_path: Path) -> list[str]:
        """Download outputs from cowork file cards.

        Walks cards newest-first and downloads one file per card title —
        earlier cards for the same title are stale versions of the same
        workbook. Uses the CDP-native download manager (same blob rationale
        as the chat-mode path).
        """
        saved: list[str] = []
        try:
            chevrons = [
                c
                for c in await self.page.query_selector_all(
                    self.COWORK_CARD_MENU_SELECTOR
                )
                if await c.is_visible()
            ]
        except Exception:
            chevrons = []
        if not chevrons:
            return []
        self.last_download_saw_buttons = True
        logger.info(f"Cowork surface: {len(chevrons)} file-card menu(s) found")
        cdp = await self._setup_cdp_downloads(download_path)
        self._cowork_seen_digests: set = set()

        seen_titles: set[str] = set()
        for chev in reversed(chevrons):  # newest cards are last in the DOM
            try:
                title = await chev.evaluate(
                    """el => {
                        let card = el;
                        for (let i = 0; i < 6 && card; i++) {
                            card = card.parentElement;
                            if (card && (card.textContent || '').includes('Spreadsheet'))
                                break;
                        }
                        if (!card) return '';
                        return (card.textContent || '').trim()
                            .split('Spreadsheet')[0].trim();
                    }"""
                )
                if title and title in seen_titles:
                    logger.info(f"Skipping stale card for {title!r}")
                    continue
                if title:
                    seen_titles.add(title)

                files_before = set(download_path.iterdir())
                await chev.evaluate(self._JS_CLICK)
                await asyncio.sleep(1.2)
                item_handle = await self.page.evaluate_handle(
                    """() => Array.from(document.querySelectorAll('[role="menuitem"]'))
                        .filter(el => el.getClientRects().length > 0)
                        .find(el => (el.textContent || '').trim().endsWith('Download'))
                        || null"""
                )
                item = item_handle.as_element()
                if item is None:
                    logger.warning(
                        "Cowork card menu has no Download item — UI drift?"
                    )
                    await self.page.keyboard.press("Escape")
                    continue

                if cdp is not None:
                    await item.evaluate(self._JS_CLICK)
                    new_file = await self._poll_for_new_file(
                        download_path, files_before
                    )
                    if new_file is None:
                        logger.warning(f"No file appeared for card {title!r}")
                        continue
                    # Content-level dedupe: cards from successive turns are
                    # versions of the same workbook, and title extraction
                    # isn't reliable enough to dedupe on its own.
                    import hashlib

                    digest = hashlib.sha256(new_file.read_bytes()).hexdigest()
                    if digest in getattr(self, "_cowork_seen_digests", set()):
                        logger.info("Skipping duplicate cowork download (same bytes)")
                        new_file.unlink(missing_ok=True)
                        continue
                    self._cowork_seen_digests = getattr(
                        self, "_cowork_seen_digests", set()
                    )
                    self._cowork_seen_digests.add(digest)

                    base = _sanitize_for_filename(title) or "cowork_output"
                    with open(new_file, "rb") as fh:
                        magic = fh.read(2)
                    target = download_path / (
                        base + (".xlsx" if magic == b"PK" else ".bin")
                    )
                    counter = 1
                    while target.exists():
                        target = download_path / f"{base}_{counter}.xlsx"
                        counter += 1
                    new_file.rename(target)
                    saved.append(str(target))
                    logger.info(f"Downloaded cowork output: {target}")
                else:
                    async with self.page.expect_download(
                        timeout=20000
                    ) as download_info:
                        await item.evaluate(self._JS_CLICK)
                    download = await download_info.value
                    target = download_path / download.suggested_filename
                    await download.save_as(str(target))
                    if target.stat().st_size == 0:
                        logger.warning(f"Discarding 0-byte cowork download: {target}")
                        target.unlink(missing_ok=True)
                        continue
                    saved.append(str(target))
                    logger.info(f"Downloaded cowork output: {target}")
                await asyncio.sleep(0.5)
            except Exception as e:
                logger.warning(f"Cowork card download failed: {e}")
                try:
                    await self.page.keyboard.press("Escape")
                except Exception:
                    pass
                continue
        return saved

    async def download_artifact(
        self, download_dir: Optional[str] = None, timeout: int = 30000
    ) -> Optional[str]:
        """Download the first artifact from Claude's response."""
        files = await self.download_all_artifacts(
            download_dir=download_dir, timeout=timeout
        )
        return files[0] if files else None

    async def download_all_artifacts(
        self, download_dir: Optional[str] = None, timeout: int = 30000
    ) -> list[str]:
        """
        Download all artifacts from Claude's response.

        Uses Chrome's native download manager over CDP (blob artifacts
        yield 0 bytes via expect_download — see _setup_cdp_downloads), with
        expect_download kept as the fallback for non-CDP connections and
        legacy HTTP downloads. Sets ``last_download_saw_buttons`` so the
        caller can tell "model produced nothing" apart from "file exists
        but retrieval failed".

        Args:
            download_dir: Directory to save downloaded files.
            timeout: Maximum time to wait for downloads in milliseconds.

        Returns:
            List of paths to downloaded files.
        """
        downloaded_files: list[str] = []
        self.last_download_saw_buttons = False
        download_path = Path(download_dir) if download_dir else Path(".")
        download_path.mkdir(parents=True, exist_ok=True)

        try:
            logger.info("Looking for artifacts to download...")
            await asyncio.sleep(1)

            # Best-effort dismissal of the artifact preview panel. NOTE:
            # correctness does not depend on this — all clicks below are
            # JS-dispatched, so an overlay can't block them.
            for close_selector in [
                'button[aria-label="Close artifact"]',
                'button[aria-label="Close"]',
                '[data-testid="close-artifact"]',
                'button:has-text("Go back")',
            ]:
                try:
                    close_btn = await self.page.query_selector(close_selector)
                    if close_btn and await close_btn.is_visible():
                        await close_btn.evaluate(self._JS_CLICK)
                        await asyncio.sleep(1)
                        logger.info(
                            f"Closed artifact preview panel via: {close_selector}"
                        )
                        break
                except Exception:
                    continue
            else:
                try:
                    await self.page.keyboard.press("Escape")
                    await asyncio.sleep(1)
                except Exception:
                    pass

            buttons = await self._find_download_buttons()
            self.last_download_saw_buttons = bool(buttons)
            if not buttons:
                # Cowork surface: no inline Download buttons at all — files
                # are cards with a "More ways to open" > Download menu.
                cowork_files = await self._download_via_cowork_cards(
                    download_path
                )
                if cowork_files:
                    return cowork_files
                logger.warning("No download buttons found on page")
                return downloaded_files

            cdp = await self._setup_cdp_downloads(download_path)
            seen_filenames: set[str] = set()

            for i, (btn, aria_label) in enumerate(buttons):
                try:
                    logger.info(
                        f"Downloading artifact {i + 1}/{len(buttons)} "
                        f"(aria={aria_label!r})..."
                    )
                    if cdp is not None:
                        files_before = set(download_path.iterdir())
                        await btn.evaluate(self._JS_CLICK)
                        new_file = await self._poll_for_new_file(
                            download_path, files_before
                        )
                        if new_file is None:
                            logger.warning(
                                f"No file appeared for artifact {i + 1} "
                                f"(aria={aria_label!r})"
                            )
                            continue
                        # allowAndName saves under a GUID — rename to the
                        # artifact's own name; sniff .xlsx from zip magic.
                        target_name = self._artifact_target_name(
                            aria_label, new_file.name
                        )
                        if "." not in target_name:
                            with open(new_file, "rb") as fh:
                                magic = fh.read(2)
                            target_name += ".xlsx" if magic == b"PK" else ".bin"
                        if target_name in seen_filenames:
                            logger.info(f"Skipping duplicate: {target_name}")
                            new_file.unlink(missing_ok=True)
                            continue
                        seen_filenames.add(target_name)
                        save_path = download_path / target_name
                        counter = 1
                        while save_path.exists():
                            save_path = download_path / (
                                f"{Path(target_name).stem}_{counter}"
                                f"{Path(target_name).suffix}"
                            )
                            counter += 1
                        new_file.rename(save_path)
                    else:
                        # Non-CDP fallback: Playwright download events.
                        async with self.page.expect_download(
                            timeout=min(timeout, 20000)
                        ) as download_info:
                            await btn.evaluate(self._JS_CLICK)
                        download = await download_info.value
                        filename = download.suggested_filename
                        if filename in seen_filenames:
                            logger.info(f"Skipping duplicate download: {filename}")
                            await download.cancel()
                            continue
                        seen_filenames.add(filename)
                        save_path = download_path / filename
                        await download.save_as(str(save_path))
                        if save_path.stat().st_size == 0:
                            # The blob trap: resolved event, empty file.
                            logger.warning(
                                f"Discarding 0-byte download: {save_path.name}"
                            )
                            save_path.unlink(missing_ok=True)
                            continue

                    downloaded_files.append(str(save_path))
                    logger.info(f"Downloaded: {save_path}")
                    await asyncio.sleep(0.5)

                except Exception as e:
                    logger.warning(f"Failed to download artifact {i + 1}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Failed to download artifacts: {e}")

        return downloaded_files


# Backward compatibility
ClaudeWebState = WebAgentState
