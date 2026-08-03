"""
Browser management for Claude Web Agent.

Uses Chrome Canary CDP mode by default to bypass Cloudflare detection.
Adapted from adam_excel_opener/core/browser_manager.py
"""

import asyncio
import logging
import os
import platform
import socket
import subprocess
import time
from pathlib import Path

logger = logging.getLogger(__name__)

# Chrome Canary CDP Configuration
CDP_PORT = 9222
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
CHROME_CANARY_PROFILE_DIR = os.path.expanduser("~/.chrome-canary-claude-web")

# Chrome paths (tries Canary first, then regular Chrome)
CHROME_CDP_PATHS = [
    # Chrome Canary (preferred)
    "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",  # macOS
    os.path.expanduser(
        "~/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"
    ),
    "/usr/bin/google-chrome-canary",  # Linux
    "/usr/bin/google-chrome-unstable",  # Linux alt
    # Windows Chrome Canary
    os.path.join(
        os.environ.get("LOCALAPPDATA", ""),
        "Google",
        "Chrome SxS",
        "Application",
        "chrome.exe",
    ),
    # Regular Chrome (fallback)
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",  # macOS
    os.path.expanduser("~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
    "/usr/bin/google-chrome",  # Linux
    "/usr/bin/google-chrome-stable",
    "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",  # Windows
    "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
]


def kill_chrome_cdp(cdp_port: int | None = None) -> None:
    """Kill the Chrome instance on ONE CDP port so a fresh one can start.

    Port-scoped on purpose: parallel runs keep several debug Chromes alive
    (e.g. Claude on 9223, ChatGPT on 9226), and the old unscoped
    ``pkill -f chrome.*remote-debugging-port`` cross-killed every one of
    them on any single stale-CDP retry.
    """
    system = platform.system()
    port = cdp_port or CDP_PORT
    logger.warning(f"Killing stale Chrome on CDP port {port}...")
    try:
        if system == "Windows":
            # No per-port scoping via taskkill; nuke-all is the only option.
            logger.warning("Windows: killing ALL chrome.exe (no port scoping)")
            subprocess.run(
                ["taskkill", "/F", "/IM", "chrome.exe"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            subprocess.run(
                ["pkill", "-f", f"remote-debugging-port={port}($| )"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    except Exception as e:
        logger.warning(f"kill_chrome_cdp error (non-fatal): {e}")
    time.sleep(2)


def find_chrome() -> str | None:
    """Find Chrome installation path."""
    for path in CHROME_CDP_PATHS:
        if os.path.exists(path):
            return path
    return None


def is_cdp_available() -> bool:
    """Check if Chrome with debugging port is already running."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    result = sock.connect_ex(("127.0.0.1", CDP_PORT))
    sock.close()
    return result == 0


def launch_chrome_cdp(
    headless: bool = False, profile_dir: str = None, cdp_port: int = None
) -> subprocess.Popen | None:
    """Launch Chrome with remote debugging enabled.

    Chrome is launched detached (start_new_session=True) so it survives
    after the calling Python process exits.
    """
    chrome_path = find_chrome()

    if not chrome_path:
        logger.error("Chrome not found! Please install Chrome or Chrome Canary")
        return None

    effective_profile = profile_dir or CHROME_CANARY_PROFILE_DIR
    effective_port = cdp_port or CDP_PORT

    args = [
        chrome_path,
        f"--remote-debugging-port={effective_port}",
        f"--user-data-dir={effective_profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--no-sandbox",
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--remote-allow-origins=*",
    ]

    if headless:
        args.append("--headless=new")

    logger.info(f"Launching Chrome with CDP: {chrome_path}")
    logger.info(f"Profile: {effective_profile}")

    process = subprocess.Popen(
        args,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # Detach so Chrome survives parent exit
    )

    return process


async def wait_for_chrome_ready(timeout: int = 30) -> bool:
    """Wait for Chrome to be ready for CDP connection."""
    start_time = time.time()

    while time.time() - start_time < timeout:
        if is_cdp_available():
            logger.info("Chrome is ready for CDP connection")
            return True
        await asyncio.sleep(0.5)

    logger.error(f"Chrome didn't start within {timeout} seconds")
    return False


class WebBrowserManager:
    """
    Manages browser instances for web agent automation.

    Supports both Claude.ai and ChatGPT. Uses Chrome CDP mode by default
    to bypass Cloudflare.
    """

    def __init__(self, config: dict):
        """
        Initialize browser manager.

        Args:
            config: Configuration dictionary with browser settings
        """
        self.config = config
        # Support both claude_web and chatgpt_web config keys
        browser_config = config.get("chatgpt_web", {}).get("browser", {}) or config.get(
            "claude_web", {}
        ).get("browser", {})

        self.browser_type = browser_config.get("type", "chrome_canary").lower()
        self.headless = browser_config.get("headless", False)
        self.timeout = browser_config.get("timeout", 30000)
        self.cdp_port = browser_config.get("cdp_port", CDP_PORT)
        self.profile_dir = os.path.expanduser(
            browser_config.get("profile_dir", CHROME_CANARY_PROFILE_DIR)
        )

    def is_cdp_mode(self) -> bool:
        """Check if using Chrome CDP mode."""
        return self.browser_type in ("chrome_canary", "cdp", "chrome")

    async def launch_browser(self, playwright):
        """
        Launch browser with automatic mode detection.

        Args:
            playwright: Playwright instance

        Returns:
            tuple: (browser, context) instances
        """
        if self.is_cdp_mode():
            return await self._launch_browser_cdp(playwright)
        else:
            return await self._launch_browser_classic(playwright)

    async def _launch_browser_cdp(self, playwright):
        """
        Launch browser using Chrome CDP mode.

        This connects to a real Chrome instance via Chrome DevTools Protocol,
        which bypasses Cloudflare and other bot detection.
        """
        cdp_url = f"http://127.0.0.1:{self.cdp_port}"
        logger.info(
            f"Using Chrome CDP mode on port {self.cdp_port} (bypasses Cloudflare)"
        )

        # Check if Chrome is already running with CDP
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        cdp_running = sock.connect_ex(("127.0.0.1", self.cdp_port)) == 0
        sock.close()

        if not cdp_running:
            # On EC2 workers, Chrome is owned by gui-agents-chrome.service so
            # cookies survive task-unit cgroup teardown. Never self-launch —
            # that would put Chrome back into the task's cgroup and defeat
            # the whole point. Fail loudly instead so systemd can restart
            # chrome.service on its own policy.
            if os.environ.get("GUI_AGENTS_CHROME_MANAGED") == "1":
                raise RuntimeError(
                    f"Chrome not reachable on CDP port {self.cdp_port}. "
                    "Managed mode: check `systemctl status gui-agents-chrome.service`."
                )
            if self.cdp_port != CDP_PORT:
                raise RuntimeError(
                    f"Chrome not running on port {self.cdp_port}. "
                    f"Launch it with: chrome --remote-debugging-port={self.cdp_port}"
                )
            logger.info("Chrome not running with CDP, launching...")

            # Determine if we need headless mode
            display = os.environ.get("DISPLAY", "")
            use_headless = self.headless or (
                not display and platform.system() != "Darwin"
            )

            process = launch_chrome_cdp(
                headless=use_headless,
                profile_dir=self.profile_dir,
                cdp_port=self.cdp_port,
            )
            if not process:
                raise RuntimeError(
                    "Chrome not found. Please install Chrome or Chrome Canary"
                )

            if not await wait_for_chrome_ready():
                process.terminate()
                raise RuntimeError("Chrome failed to start with CDP")
        else:
            logger.info(f"Chrome already running on port {self.cdp_port}")

        # Connect via CDP (retry once if stale Chrome causes protocol error)
        try:
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
            logger.info(f"Connected to Chrome via CDP ({cdp_url})")
        except Exception as e:
            error_msg = str(e).lower()
            stale = (
                "setdownloadbehavior" in error_msg or "context management" in error_msg
            )
            if stale and os.environ.get("GUI_AGENTS_CHROME_MANAGED") == "1":
                # Managed mode owns Chrome's lifecycle — don't kill/relaunch
                # ourselves. Let systemd's restart policy handle it.
                logger.error(
                    f"CDP handshake rejected (stale Chrome): {e}. "
                    "Managed mode: not self-restarting; "
                    "try `systemctl restart gui-agents-chrome.service`."
                )
                raise
            if stale:
                logger.warning(
                    f"CDP handshake rejected (stale Chrome): {e}. "
                    f"Killing Chrome on port {self.cdp_port} and retrying..."
                )
                kill_chrome_cdp(self.cdp_port)

                display = os.environ.get("DISPLAY", "")
                use_headless = self.headless or (
                    not display and platform.system() != "Darwin"
                )
                process = launch_chrome_cdp(
                    headless=use_headless,
                    profile_dir=self.profile_dir,
                    cdp_port=self.cdp_port,
                )
                if not process:
                    raise RuntimeError("Chrome not found after restart")
                if not await wait_for_chrome_ready():
                    process.terminate()
                    raise RuntimeError("Chrome failed to start after restart")

                try:
                    browser = await playwright.chromium.connect_over_cdp(cdp_url)
                    logger.info("Connected to Chrome via CDP (after restart)")
                except Exception as e2:
                    logger.error(f"CDP connection failed after restart: {e2}")
                    raise
            else:
                logger.error(f"Failed to connect to Chrome: {e}")
                raise

        # Get existing context or create new one
        contexts = browser.contexts
        if contexts:
            context = contexts[0]
            logger.info(f"Using existing context ({len(context.pages)} page(s))")
        else:
            context = await browser.new_context(ignore_https_errors=True)
            logger.info("Created new browser context")

        context.set_default_timeout(self.timeout)
        return browser, context

    async def _launch_browser_classic(self, playwright):
        """
        Launch browser using classic Playwright mode.

        This uses Playwright-bundled browsers but may encounter Cloudflare issues.
        """
        logger.info(f"Using {self.browser_type} browser (classic mode)")

        if self.browser_type == "firefox":
            browser_instance = playwright.firefox
        elif self.browser_type == "webkit":
            browser_instance = playwright.webkit
        else:
            browser_instance = playwright.chromium

        # Get auth state path
        auth_state_path = self._get_auth_state_path()

        if auth_state_path.exists():
            logger.info(f"Loading auth state from: {auth_state_path}")
            import json

            with open(auth_state_path, "r") as f:
                storage_state = json.load(f)

            browser = await browser_instance.launch(headless=self.headless)
            context = await browser.new_context(
                storage_state=storage_state, ignore_https_errors=True
            )
        else:
            logger.warning(f"No auth state found at: {auth_state_path}")
            logger.warning("You may need to log in manually")

            browser = await browser_instance.launch(headless=self.headless)
            context = await browser.new_context(ignore_https_errors=True)

        context.set_default_timeout(self.timeout)
        return browser, context

    def _get_auth_state_path(self) -> Path:
        """Get path to auth state file."""
        project_root = Path(__file__).parent.parent
        return project_root / "claude_web_auth_state.json"

    async def save_auth_state(self, context) -> bool:
        """
        Save browser auth state for future sessions.

        Args:
            context: Browser context to save state from

        Returns:
            True if save succeeded
        """
        try:
            auth_state_path = self._get_auth_state_path()
            storage_state = await context.storage_state()

            import json

            with open(auth_state_path, "w") as f:
                json.dump(storage_state, f, indent=2)

            logger.info(f"Saved auth state to: {auth_state_path}")
            return True
        except Exception as e:
            logger.error(f"Failed to save auth state: {e}")
            return False

    async def close_browser(self, context, browser=None):
        """Close browser resources."""
        if self.is_cdp_mode():
            # CDP mode: keep shared context alive
            logger.debug("CDP mode: keeping shared context alive")
            return

        if context:
            try:
                await context.close()
                logger.info("Browser context closed")
            except Exception as e:
                logger.warning(f"Error closing context: {e}")

        if browser:
            try:
                await browser.close()
            except Exception:
                pass
