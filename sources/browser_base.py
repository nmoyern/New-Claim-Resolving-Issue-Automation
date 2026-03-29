"""
sources/browser_base.py
-----------------------
Base class for all browser automation sessions.
Handles:
  - Playwright async context
  - Session cookie persistence (login once/day, reuse)
  - MFA routing (manual, TOTP, Duo push)
  - Screenshot on error
  - DRY_RUN mode (navigates but doesn't submit)
"""
from __future__ import annotations

import asyncio
import json
import os
import pickle
from abc import ABC, abstractmethod
from datetime import date
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    async_playwright,
    Browser,
    BrowserContext,
    Page,
    Playwright,
    TimeoutError as PWTimeout,
)

from config.settings import DRY_RUN, SESSION_DIR, LOG_DIR
from logging_utils.logger import get_logger

SESSION_DIR.mkdir(parents=True, exist_ok=True)
SCREENSHOT_DIR = LOG_DIR / "screenshots"
SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)


class BrowserSession(ABC):
    """
    Abstract base for any portal automation.
    Subclasses implement: login_url, _perform_login, _is_logged_in
    """

    SESSION_NAME: str = "base"     # override in subclass
    DEFAULT_TIMEOUT: int = 30_000  # ms

    def __init__(self, headless: bool = True):
        self.headless = headless
        self.logger = get_logger(self.SESSION_NAME)
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._session_file = SESSION_DIR / f"{self.SESSION_NAME}_{date.today().isoformat()}.json"

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "BrowserSession":
        if DRY_RUN:
            self.logger.info("DRY_RUN: Skipping browser launch", portal=self.SESSION_NAME)
            return self

        self._pw = await async_playwright().start()
        self._browser = await self._pw.chromium.launch(
            headless=self.headless,
            args=["--disable-blink-features=AutomationControlled"],
        )

        # Try to restore a saved session (from grab_session.py or previous run)
        if self._session_file.exists():
            try:
                self._context = await self._browser.new_context(
                    storage_state=str(self._session_file),
                )
                self.logger.info("Restored browser session", session=self.SESSION_NAME)
            except Exception as e:
                self.logger.warning("Session restore failed, starting fresh", error=str(e))
                self._context = None

        if self._context is None:
            self._context = await self._browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                )
            )
            # Remove webdriver flag to help pass bot detection
            await self._context.add_init_script(
                'Object.defineProperty(navigator, "webdriver", { get: () => undefined });'
            )

        self._context.set_default_timeout(self.DEFAULT_TIMEOUT)
        self.page = await self._context.new_page()

        # Navigate to the portal URL so cookies get sent to the server
        try:
            await self.page.goto(
                self.login_url,
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(2)
        except Exception:
            pass

        # Check if session cookies kept us logged in; if not, re-login
        if not await self._is_logged_in():
            await self._do_login()

        return self

    async def __aexit__(self, *_):
        if DRY_RUN:
            return
        if self._context:
            # Persist session for today
            await self._context.storage_state(path=str(self._session_file))
            self.logger.info("Session persisted", session=self.SESSION_NAME)
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._pw:
            await self._pw.stop()

    # ------------------------------------------------------------------
    # Abstract interface
    # ------------------------------------------------------------------

    @property
    @abstractmethod
    def login_url(self) -> str:
        ...

    @abstractmethod
    async def _perform_login(self) -> bool:
        """Navigate to login page and submit credentials. Return True on success."""
        ...

    @abstractmethod
    async def _is_logged_in(self) -> bool:
        """Check if we currently have a valid session."""
        ...

    # ------------------------------------------------------------------
    # Internal login flow
    # ------------------------------------------------------------------

    async def _do_login(self):
        self.logger.info("Logging in", portal=self.SESSION_NAME)
        try:
            success = await self._perform_login()
        except Exception as e:
            await self.screenshot(f"login_error_{self.SESSION_NAME}")
            raise RuntimeError(
                f"Login failed for {self.SESSION_NAME}: {e}\n"
                f"If this portal has CAPTCHA, run: bash ~/claims.sh {self.SESSION_NAME}\n"
                f"to log in manually and save the session."
            )
        if not success:
            await self.screenshot(f"login_failed_{self.SESSION_NAME}")
            raise RuntimeError(
                f"Login failed for {self.SESSION_NAME}.\n"
                f"This may be due to CAPTCHA or expired credentials.\n"
                f"Run: bash ~/claims.sh {self.SESSION_NAME}\n"
                f"to log in manually and save the session."
            )
        self.logger.info("Login successful", portal=self.SESSION_NAME)

    # ------------------------------------------------------------------
    # MFA helpers
    # ------------------------------------------------------------------

    async def handle_mfa(self, mfa_type: str) -> bool:
        """
        Route to the right MFA handler.
        mfa_type: "manual" | "totp" | "duo_push" | "none"
        """
        if mfa_type == "none":
            return True
        if mfa_type == "manual":
            return await self._mfa_manual()
        if mfa_type == "totp":
            return await self._mfa_totp()
        if mfa_type == "duo_push":
            return await self._mfa_duo_push()
        self.logger.warning("Unknown MFA type", mfa_type=mfa_type)
        return await self._mfa_manual()

    async def _mfa_manual(self) -> bool:
        """
        Pause for human to complete MFA.
        In production: send a notification (email/Slack) to the operator,
        then poll for completion (max 5 minutes).
        """
        self.logger.warning(
            "MANUAL MFA REQUIRED",
            portal=self.SESSION_NAME,
            message="Complete MFA in the browser window. Automation will resume.",
        )
        if not self.headless:
            # Give human 5 minutes to complete MFA
            for i in range(60):
                await asyncio.sleep(5)
                if await self._is_logged_in():
                    return True
                self.logger.info("Waiting for MFA completion...", seconds_elapsed=(i+1)*5)
        return False

    async def _mfa_totp(self) -> bool:
        """Use TOTP secret to generate code and enter it."""
        import hmac, hashlib, struct, time, base64
        secret_env = f"{self.SESSION_NAME.upper()}_TOTP_SECRET"
        totp_secret = os.getenv(secret_env, "")
        if not totp_secret:
            self.logger.warning("No TOTP secret found, falling back to manual", env_var=secret_env)
            return await self._mfa_manual()

        # TOTP implementation (RFC 6238)
        key = base64.b32decode(totp_secret.upper().replace(" ", ""))
        msg = struct.pack(">Q", int(time.time()) // 30)
        h = hmac.new(key, msg, hashlib.sha1).digest()
        offset = h[-1] & 0x0F
        code = str((struct.unpack(">I", h[offset:offset+4])[0] & 0x7FFFFFFF) % 1_000_000).zfill(6)

        # Try to find and fill OTP field
        try:
            otp_field = await self.page.wait_for_selector(
                "input[type='text'], input[name*='otp'], input[name*='code'], input[placeholder*='code']",
                timeout=5000,
            )
            await otp_field.fill(code)
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(2)
            return await self._is_logged_in()
        except Exception as e:
            self.logger.error("TOTP entry failed", error=str(e))
            return await self._mfa_manual()

    async def _mfa_duo_push(self) -> bool:
        """
        For Duo push: click "Send Push" button on the Duo frame.
        Then poll for login success.
        """
        try:
            duo_frame = await self.page.wait_for_selector("iframe[id*='duo'], iframe[src*='duo']", timeout=5000)
            frame = await duo_frame.content_frame()
            if frame:
                push_btn = await frame.wait_for_selector("button:has-text('Send Me a Push')", timeout=5000)
                await push_btn.click()
                self.logger.info("Duo push sent — waiting for approval")
                for _ in range(24):  # 2 minutes
                    await asyncio.sleep(5)
                    if await self._is_logged_in():
                        return True
        except Exception as e:
            self.logger.warning("Duo push automation failed, falling back to manual", error=str(e))
        return await self._mfa_manual()

    # ------------------------------------------------------------------
    # Utility helpers
    # ------------------------------------------------------------------

    async def screenshot(self, name: str):
        """Take a debug screenshot."""
        path = SCREENSHOT_DIR / f"{self.SESSION_NAME}_{name}_{date.today().isoformat()}.png"
        try:
            await self.page.screenshot(path=str(path), full_page=True)
            self.logger.info("Screenshot saved", path=str(path))
        except Exception:
            pass

    async def safe_click(self, selector: str, timeout: int = 10_000):
        """Click with error handling and screenshot on failure."""
        try:
            await self.page.click(selector, timeout=timeout)
        except PWTimeout:
            await self.screenshot(f"click_timeout_{selector[:30]}")
            raise

    async def safe_fill(self, selector: str, value: str, timeout: int = 10_000):
        """Fill a field with error handling."""
        try:
            await self.page.fill(selector, value, timeout=timeout)
        except PWTimeout:
            await self.screenshot(f"fill_timeout_{selector[:30]}")
            raise

    async def wait_and_get_text(self, selector: str, timeout: int = 10_000) -> str:
        elem = await self.page.wait_for_selector(selector, timeout=timeout)
        return (await elem.inner_text()).strip()
