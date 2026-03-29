"""
reconciliation/bank_portals.py
-------------------------------
Bank portal automation for verifying deposits:
  - Wells Fargo (KJLN)
  - Southern Bank (Mary's Home)
  - Bank of America (NHCS)

Each portal logs in, navigates to transaction history, and searches
for deposits matching ERA payment amounts.

These are commercial banking portals with strong bot detection.
Strategy: use morning_startup to log in headful, save session cookies,
then reuse cookies for automated checks.
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import date, timedelta
from typing import List, Optional

from sources.browser_base import BrowserSession
from logging_utils.logger import get_logger

logger = get_logger("bank_portals")


# ---------------------------------------------------------------------------
# Base bank portal
# ---------------------------------------------------------------------------

class BankPortalBase(BrowserSession):
    """Base for bank portal automation."""

    BANK_NAME: str = "unknown"
    PROGRAM: str = "UNKNOWN"

    async def get_recent_deposits(self, days: int = 14) -> List[dict]:
        """
        Get recent deposits/credits from the bank.
        Returns list of dicts: {date, amount, description, reference}
        """
        raise NotImplementedError

    async def find_deposit(self, amount: float, paid_date: str, tolerance: float = 0.01) -> Optional[dict]:
        """
        Search for a specific deposit by amount and approximate date.
        Returns deposit dict if found, None otherwise.
        """
        deposits = await self.get_recent_deposits(days=14)
        for dep in deposits:
            if abs(dep.get("amount", 0) - amount) <= tolerance:
                # Check date is within 3 business days
                if self._dates_close(dep.get("date", ""), paid_date, max_days=5):
                    return dep
        return None

    @staticmethod
    def _dates_close(date1: str, date2: str, max_days: int = 5) -> bool:
        """Check if two date strings are within max_days of each other."""
        try:
            from datetime import datetime
            formats = ["%m/%d/%Y", "%Y-%m-%d", "%m/%d/%y"]
            d1 = d2 = None
            for fmt in formats:
                try:
                    d1 = datetime.strptime(date1, fmt).date()
                    break
                except ValueError:
                    continue
            for fmt in formats:
                try:
                    d2 = datetime.strptime(date2, fmt).date()
                    break
                except ValueError:
                    continue
            if d1 and d2:
                return abs((d1 - d2).days) <= max_days
        except Exception:
            pass
        return True  # If we can't parse dates, don't exclude


# ---------------------------------------------------------------------------
# Wells Fargo (KJLN)
# ---------------------------------------------------------------------------

class WellsFargoPortal(BankPortalBase):
    """
    Wells Fargo Commercial Electronic Office (CEO) portal.
    URL: wellsfargo.com → Sign On → Commercial Electronic Office
    """
    SESSION_NAME = "wellsfargo"
    BANK_NAME = "Wells Fargo"
    PROGRAM = "KJLN"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._username = os.getenv("BANK_WELLSFARGO_USERNAME", "")
        self._password = os.getenv("BANK_WELLSFARGO_PASSWORD", "")

    @property
    def login_url(self) -> str:
        return os.getenv("BANK_WELLSFARGO_URL", "https://www.wellsfargo.com")

    async def _is_logged_in(self) -> bool:
        try:
            url = self.page.url.lower()
            # Wells Fargo dashboard indicators
            if any(kw in url for kw in ("login", "signin", "signon", "about:blank")):
                return False
            # Look for logged-in indicators
            for sel in [
                "a[href*='signoff']", "a[href*='logoff']",
                ".account-summary", ".dashboard",
                "#accountSummary", ".welcome-message",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    return True
            return "signon" not in url and "login" not in url
        except Exception:
            return False

    async def _perform_login(self) -> bool:
        if not self._username:
            raise RuntimeError("Wells Fargo credentials not configured")

        await self.page.goto(self.login_url, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Wells Fargo has multiple login paths — try the main sign-on
        # Look for "Sign On" link first
        sign_on = await self.page.query_selector(
            "a:has-text('Sign On'), a[href*='signon'], "
            "a[href*='login'], button:has-text('Sign On')"
        )
        if sign_on:
            await sign_on.click()
            await asyncio.sleep(3)

        # Fill username
        for sel in [
            "input[id='j_username']", "input[name='j_username']",
            "input[id='userid']", "input[name='userid']",
            "input[type='text'][name*='user']",
            "input[aria-label*='username' i]",
            "input[aria-label*='user' i]",
        ]:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(self._username)
                self.logger.info("Filled username", selector=sel)
                break

        # Fill password
        for sel in [
            "input[id='j_password']", "input[name='j_password']",
            "input[type='password']",
        ]:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(self._password)
                self.logger.info("Filled password")
                break

        # Submit
        for sel in [
            "button[type='submit']", "input[type='submit']",
            "button:has-text('Sign On')", "a:has-text('Sign On')",
            "#btnSignon",
        ]:
            btn = await self.page.query_selector(sel)
            if btn:
                await btn.click()
                break

        await asyncio.sleep(5)

        # Wells Fargo may have security questions or MFA
        if not await self._is_logged_in():
            # Check for security challenge
            challenge = await self.page.query_selector(
                "input[name*='answer'], input[placeholder*='answer']"
            )
            if challenge:
                self.logger.warning(
                    "WELLS FARGO SECURITY QUESTION detected. "
                    "Please answer in the browser window.",
                    portal="wellsfargo",
                )
                return await self._wait_for_manual_completion()

            # Check for MFA/verification code
            code_field = await self.page.query_selector(
                "input[name*='code'], input[name*='otp'], "
                "input[placeholder*='code'], input[maxlength='6']"
            )
            if code_field:
                self.logger.warning(
                    "WELLS FARGO MFA CODE required. "
                    "Enter the code in the browser window.",
                    portal="wellsfargo",
                )
                return await self._wait_for_manual_completion()

        return await self._is_logged_in()

    async def _wait_for_manual_completion(self, timeout_seconds: int = 180) -> bool:
        """Wait for human to complete login challenge."""
        for i in range(timeout_seconds // 5):
            await asyncio.sleep(5)
            if await self._is_logged_in():
                self.logger.info("Manual login completion detected")
                return True
            if i % 6 == 0:
                self.logger.info("Waiting for manual login...",
                                 seconds_elapsed=(i + 1) * 5)
        return False

    async def get_recent_deposits(self, days: int = 14) -> List[dict]:
        """Navigate to account activity and pull recent deposits."""
        deposits = []
        try:
            # Navigate to account activity/history
            for nav_sel in [
                "a:has-text('Account Activity')",
                "a:has-text('View Activity')",
                "a:has-text('Transaction History')",
                "a[href*='activity']",
                "a[href*='history']",
            ]:
                try:
                    await self.page.click(nav_sel, timeout=3000)
                    await asyncio.sleep(2)
                    break
                except Exception:
                    continue

            # Try to filter for deposits/credits only
            deposit_filter = await self.page.query_selector(
                "select[name*='type'], select[name*='filter']"
            )
            if deposit_filter:
                try:
                    await self.page.select_option(
                        "select[name*='type'], select[name*='filter']",
                        label="Credits"
                    )
                    await asyncio.sleep(1)
                except Exception:
                    pass

            # Set date range
            from_date = (date.today() - timedelta(days=days)).strftime("%m/%d/%Y")
            to_date = date.today().strftime("%m/%d/%Y")

            for sel in [
                "input[name*='from'], input[name*='start']",
                "input[id*='fromDate'], input[id*='startDate']",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(from_date)
                    break

            for sel in [
                "input[name*='to'], input[name*='end']",
                "input[id*='toDate'], input[id*='endDate']",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(to_date)
                    break

            # Search
            search_btn = await self.page.query_selector(
                "button:has-text('Search'), button:has-text('Go'), "
                "input[type='submit']"
            )
            if search_btn:
                await search_btn.click()
                await asyncio.sleep(3)

            # Parse transaction rows
            deposits = await self._parse_transaction_rows()

        except Exception as e:
            self.logger.error("Failed to get Wells Fargo deposits", error=str(e))
            await self.screenshot("wellsfargo_deposits_error")

        return deposits

    async def _parse_transaction_rows(self) -> List[dict]:
        """Parse transaction table for deposit/credit entries."""
        deposits = []
        try:
            rows = await self.page.query_selector_all(
                "table.transaction-table tbody tr, "
                ".transaction-row, .activity-row, "
                "table tbody tr"
            )
            for row in rows:
                text = await row.inner_text()
                # Look for credit/deposit indicators
                if any(kw in text.lower() for kw in (
                    "credit", "deposit", "eft", "ach", "wire",
                    "incoming", "received"
                )):
                    # Try to extract amount and date
                    amount_match = re.search(
                        r'\$?([\d,]+\.\d{2})', text
                    )
                    date_match = re.search(
                        r'(\d{1,2}/\d{1,2}/\d{2,4})', text
                    )
                    if amount_match:
                        deposits.append({
                            "date": date_match.group(1) if date_match else "",
                            "amount": float(
                                amount_match.group(1).replace(",", "")
                            ),
                            "description": text.strip()[:200],
                            "reference": "",
                            "bank": self.BANK_NAME,
                        })
        except Exception as e:
            self.logger.warning("Transaction parse error", error=str(e))
        return deposits


# ---------------------------------------------------------------------------
# Southern Bank (Mary's Home)
# ---------------------------------------------------------------------------

class SouthernBankPortal(BankPortalBase):
    """
    Southern Bank ebanking-services portal.
    URL: southernbank.ebanking-services.com
    Has Company ID field in addition to username/password.
    """
    SESSION_NAME = "southernbank"
    BANK_NAME = "Southern Bank"
    PROGRAM = "MARYS_HOME"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._company_id = os.getenv("BANK_SOUTHERN_COMPANY_ID", "")
        self._username = os.getenv("BANK_SOUTHERN_USERNAME", "")
        self._password = os.getenv("BANK_SOUTHERN_PASSWORD", "")

    @property
    def login_url(self) -> str:
        return os.getenv(
            "BANK_SOUTHERN_URL",
            "https://southernbank.ebanking-services.com/eAM/Credential/Index"
            "?appId=beb&brand=southernbank"
        )

    async def _is_logged_in(self) -> bool:
        try:
            url = self.page.url.lower()
            # Not logged in if on login, credential, or OTP pages
            if any(kw in url for kw in (
                "credential", "login", "signin", "about:blank",
                "ooba",  # OTP/MFA challenge page
            )):
                return False
            for sel in [
                "a[href*='logoff']", "a[href*='logout']",
                "a[href*='signoff']", ".logout",
                ".account-list", ".dashboard",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    return True
            return "credential" not in url and "login" not in url
        except Exception:
            return False

    async def _perform_login(self) -> bool:
        if not self._username:
            raise RuntimeError("Southern Bank credentials not configured")

        await self.page.goto(self.login_url, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # Southern BusinessPro is a MULTI-STEP login:
        # Step 1: Company ID field → fill and see if username appears
        # Step 2: Username field → fill
        # Step 3: Click "Continue"
        # Step 4: Password field appears on next page → fill and submit

        # Dismiss cookie banner if present
        cookie_btn = await self.page.query_selector(
            "button:has-text('Accept'), button:has-text('Close'), "
            "button.close, button[aria-label='Close']"
        )
        if cookie_btn:
            try:
                await cookie_btn.click()
                await asyncio.sleep(0.5)
            except Exception:
                pass

        # Get all visible input fields on the page
        inputs = await self.page.query_selector_all("input[type='text'], input:not([type])")
        self.logger.info("Found input fields", count=len(inputs))

        # Fill Company ID — first text input
        if len(inputs) >= 1 and self._company_id:
            await inputs[0].fill(self._company_id)
            self.logger.info("Filled Company ID (first field)")
            await asyncio.sleep(0.3)

        # Fill Username — second text input
        if len(inputs) >= 2:
            await inputs[1].fill(self._username)
            self.logger.info("Filled Username (second field)")
            await asyncio.sleep(0.3)

        # Check "Remember Company ID" if available
        remember = await self.page.query_selector(
            "input[type='checkbox'][name*='remember' i], "
            "input[type='checkbox'][id*='remember' i]"
        )
        if remember:
            try:
                await remember.check()
            except Exception:
                pass

        # Click Continue button — use expect_navigation to handle page reload
        for sel in [
            "button:has-text('Continue')", "input[value*='Continue']",
            "button:has-text('Log In')", "button:has-text('Sign In')",
            "button[type='submit']", "input[type='submit']",
        ]:
            btn = await self.page.query_selector(sel)
            if btn:
                try:
                    async with self.page.expect_navigation(
                        timeout=15000,
                        wait_until="domcontentloaded",
                    ):
                        await btn.click()
                    self.logger.info(
                        "Clicked continue and navigation completed",
                        selector=sel,
                    )
                except Exception:
                    # Navigation may not always trigger
                    await asyncio.sleep(3)
                    self.logger.info("Clicked continue", selector=sel)
                break

        await asyncio.sleep(3)

        # Step 2: Southern BusinessPro OTP challenge
        # First page: "Continue with Security Code" button
        continue_otp = await self.page.query_selector(
            "button:has-text('Continue with Security Code'), "
            "input[value*='Continue with Security'], "
            "button:has-text('Continue with Security')"
        )
        if continue_otp:
            self.logger.info("Clicking 'Continue with Security Code'")
            try:
                async with self.page.expect_navigation(
                    timeout=15000, wait_until="domcontentloaded",
                ):
                    await continue_otp.click()
            except Exception:
                await asyncio.sleep(3)
            await asyncio.sleep(3)

        # Second page: Phone number selection + Text/Call option
        # Select "Text the selected number" radio button
        text_radio = await self.page.query_selector(
            "input[type='radio'][value*='text' i], "
            "input[type='radio'][value*='sms' i], "
            "input[type='radio'][value*='Text' i]"
        )
        if text_radio:
            await text_radio.click()
            self.logger.info("Selected 'Text' delivery option")
            await asyncio.sleep(0.5)
        else:
            # Try clicking the label text instead
            try:
                await self.page.click(
                    "label:has-text('Text the selected')",
                    timeout=3000,
                )
                self.logger.info("Selected 'Text' via label click")
                await asyncio.sleep(0.5)
            except Exception:
                self.logger.info(
                    "Could not find Text radio — may default to call"
                )

        # Click Continue to send the OTP
        continue_btn = await self.page.query_selector(
            "button:has-text('Continue'), input[value*='Continue']"
        )
        if continue_btn:
            self.logger.info("Clicking Continue to send OTP")
            try:
                async with self.page.expect_navigation(
                    timeout=15000, wait_until="domcontentloaded",
                ):
                    await continue_btn.click()
            except Exception:
                await asyncio.sleep(3)
            await asyncio.sleep(3)
            await self.screenshot("southernbank_otp_code_entry")

        # Now look for the OTP input field
        otp_field = await self.page.query_selector(
            "input[name*='code' i], input[name*='otp' i], "
            "input[name*='security' i], input[name*='token' i], "
            "input[placeholder*='code' i], input[placeholder*='security' i], "
            "input[maxlength='6'], input[maxlength='8'], "
            "input[type='text'], input[type='tel']"
        )
        if otp_field or "ooba" in self.page.url.lower():
            self.logger.warning(
                "SOUTHERN BANK OTP CODE required. "
                "A text/call with a one-time code has been sent. "
                "Enter the code in the browser window.",
                portal="southernbank",
            )
            # Wait for human to enter OTP (up to 3 minutes)
            for i in range(36):
                await asyncio.sleep(5)
                # Check if we moved past OTP page
                current_otp = await self.page.query_selector(
                    "input[name*='code' i], input[name*='otp' i], "
                    "input[name*='security' i], input[name*='token' i]"
                )
                if not current_otp:
                    # OTP page gone — moved to next step
                    self.logger.info("OTP step completed")
                    break
                if await self._is_logged_in():
                    return True
                if i % 6 == 0 and i > 0:
                    self.logger.info(
                        "Waiting for OTP entry...",
                        seconds_elapsed=(i + 1) * 5,
                    )
            await asyncio.sleep(2)

        # Step 3: Password page (after OTP)
        try:
            await self.page.wait_for_load_state(
                "domcontentloaded", timeout=5000
            )
        except Exception:
            pass
        await asyncio.sleep(2)

        pw_field = await self.page.query_selector("input[type='password']")
        if pw_field:
            await pw_field.fill(self._password)
            self.logger.info("Filled password (step 3)")
            await asyncio.sleep(0.3)

            for sel in [
                "button:has-text('Sign In')", "button:has-text('Log In')",
                "button:has-text('Continue')", "button:has-text('Submit')",
                "button[type='submit']", "input[type='submit']",
                "input[value*='Sign In']", "input[value*='Log In']",
            ]:
                btn = await self.page.query_selector(sel)
                if btn:
                    await btn.click()
                    self.logger.info("Submitted password", selector=sel)
                    break

            await asyncio.sleep(5)

        # Final check
        if not await self._is_logged_in():
            await self.screenshot("southernbank_post_login")
            # Generic fallback for any remaining challenge
            self.logger.warning(
                "SOUTHERN BANK: Login not complete. "
                "Please finish in the browser window.",
                portal="southernbank",
            )
            return await self._wait_for_manual_completion()

        return await self._is_logged_in()

    async def _wait_for_manual_completion(self, timeout_seconds: int = 180) -> bool:
        for i in range(timeout_seconds // 5):
            await asyncio.sleep(5)
            if await self._is_logged_in():
                self.logger.info("Manual login completion detected")
                return True
            if i % 6 == 0:
                self.logger.info("Waiting for manual login...",
                                 seconds_elapsed=(i + 1) * 5)
        return False

    async def get_recent_deposits(self, days: int = 14) -> List[dict]:
        """Navigate to account history and pull recent deposits."""
        deposits = []
        try:
            # Navigate to account activity
            for nav_sel in [
                "a:has-text('Account Activity')",
                "a:has-text('Transaction History')",
                "a:has-text('Activity')",
                "a[href*='activity']",
                "a[href*='history']",
                "a[href*='transaction']",
            ]:
                try:
                    await self.page.click(nav_sel, timeout=3000)
                    await asyncio.sleep(2)
                    break
                except Exception:
                    continue

            # Set date range
            from_date = (date.today() - timedelta(days=days)).strftime("%m/%d/%Y")
            to_date = date.today().strftime("%m/%d/%Y")

            for sel in [
                "input[name*='from' i]", "input[name*='start' i]",
                "input[id*='from' i]", "input[id*='start' i]",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(from_date)
                    break

            for sel in [
                "input[name*='to' i]", "input[name*='end' i]",
                "input[id*='to' i]", "input[id*='end' i]",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(to_date)
                    break

            # Search
            search_btn = await self.page.query_selector(
                "button:has-text('Search'), button:has-text('Go'), "
                "button:has-text('View'), input[type='submit']"
            )
            if search_btn:
                await search_btn.click()
                await asyncio.sleep(3)

            # Parse rows
            deposits = await self._parse_transaction_rows()

        except Exception as e:
            self.logger.error("Failed to get Southern Bank deposits",
                              error=str(e))
            await self.screenshot("southernbank_deposits_error")

        return deposits

    async def _parse_transaction_rows(self) -> List[dict]:
        deposits = []
        try:
            rows = await self.page.query_selector_all(
                "table tbody tr, .transaction-row, .activity-row"
            )
            for row in rows:
                text = await row.inner_text()
                if any(kw in text.lower() for kw in (
                    "credit", "deposit", "eft", "ach", "wire",
                    "incoming", "received"
                )):
                    amount_match = re.search(r'\$?([\d,]+\.\d{2})', text)
                    date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})', text)
                    if amount_match:
                        deposits.append({
                            "date": date_match.group(1) if date_match else "",
                            "amount": float(
                                amount_match.group(1).replace(",", "")
                            ),
                            "description": text.strip()[:200],
                            "reference": "",
                            "bank": self.BANK_NAME,
                        })
        except Exception as e:
            self.logger.warning("Transaction parse error", error=str(e))
        return deposits


# ---------------------------------------------------------------------------
# Bank of America (NHCS)
# ---------------------------------------------------------------------------

class BankOfAmericaPortal(BankPortalBase):
    """
    Bank of America Business banking portal.
    URL: bankofamerica.com/business/
    """
    SESSION_NAME = "bankofamerica"
    BANK_NAME = "Bank of America"
    PROGRAM = "NHCS"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._username = os.getenv("BANK_BOA_USERNAME", "")
        self._password = os.getenv("BANK_BOA_PASSWORD", "")

    @property
    def login_url(self) -> str:
        return os.getenv(
            "BANK_BOA_URL",
            "https://www.bankofamerica.com/business/"
        )

    async def _is_logged_in(self) -> bool:
        try:
            url = self.page.url.lower()
            if any(kw in url for kw in (
                "login", "signin", "signon", "auth", "about:blank"
            )):
                return False
            for sel in [
                "a[href*='signoff']", "a[href*='logout']",
                "a[id*='signOff']", ".sign-off",
                "#signOff", ".accounts-overview",
                ".account-summary",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    return True
            return "login" not in url and "signin" not in url
        except Exception:
            return False

    async def _perform_login(self) -> bool:
        if not self._username:
            raise RuntimeError("Bank of America credentials not configured")

        await self.page.goto(self.login_url, wait_until="domcontentloaded")
        await asyncio.sleep(3)

        # BofA may show a business login page — look for Sign In
        sign_in = await self.page.query_selector(
            "a:has-text('Sign In'), a:has-text('Log In'), "
            "a[href*='signin'], a[href*='login']"
        )
        if sign_in:
            await sign_in.click()
            await asyncio.sleep(3)

        # Fill Online ID (username)
        for sel in [
            "input[id='onlineId1']", "input[name='onlineId1']",
            "input[id='enterID-input']",
            "input[name*='user' i]", "input[id*='user' i]",
            "input[aria-label*='Online ID' i]",
            "input[placeholder*='Online ID' i]",
        ]:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(self._username)
                self.logger.info("Filled Online ID", selector=sel)
                break

        # Fill Passcode (password)
        for sel in [
            "input[id='passcode1']", "input[name='passcode1']",
            "input[id='tlpvt-passcode-input']",
            "input[type='password']",
        ]:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(self._password)
                self.logger.info("Filled passcode")
                break

        # Submit
        for sel in [
            "button[id='signIn']", "input[id='signIn']",
            "button:has-text('Sign In')", "button:has-text('Log In')",
            "button[type='submit']", "input[type='submit']",
        ]:
            btn = await self.page.query_selector(sel)
            if btn:
                await btn.click()
                break

        await asyncio.sleep(5)

        # BofA often asks to verify identity (send code, security question)
        if not await self._is_logged_in():
            # Check for "Send code" / verification prompt
            send_code = await self.page.query_selector(
                "button:has-text('Send'), button:has-text('Text'), "
                "button:has-text('Call'), a:has-text('Send')"
            )
            if send_code:
                # Click send to get verification code
                await send_code.click()
                await asyncio.sleep(2)
                self.logger.warning(
                    "BANK OF AMERICA VERIFICATION CODE sent. "
                    "Enter the code in the browser window.",
                    portal="bankofamerica",
                )
                return await self._wait_for_manual_completion()

            # Check for security question
            answer_field = await self.page.query_selector(
                "input[name*='answer'], input[placeholder*='answer']"
            )
            if answer_field:
                self.logger.warning(
                    "BANK OF AMERICA SECURITY QUESTION detected. "
                    "Answer in the browser window.",
                    portal="bankofamerica",
                )
                return await self._wait_for_manual_completion()

        return await self._is_logged_in()

    async def _wait_for_manual_completion(self, timeout_seconds: int = 180) -> bool:
        for i in range(timeout_seconds // 5):
            await asyncio.sleep(5)
            if await self._is_logged_in():
                self.logger.info("Manual login completion detected")
                return True
            if i % 6 == 0:
                self.logger.info("Waiting for manual login...",
                                 seconds_elapsed=(i + 1) * 5)
        return False

    async def get_recent_deposits(self, days: int = 14) -> List[dict]:
        """Navigate to account activity and pull recent deposits."""
        deposits = []
        try:
            # Navigate to account details/activity
            for nav_sel in [
                "a:has-text('Account Activity')",
                "a:has-text('View Activity')",
                "a:has-text('Transaction History')",
                "a[href*='activity']",
                "a[href*='AccountDetails']",
            ]:
                try:
                    await self.page.click(nav_sel, timeout=3000)
                    await asyncio.sleep(2)
                    break
                except Exception:
                    continue

            # Try to filter by transaction type (deposits/credits)
            type_filter = await self.page.query_selector(
                "select[name*='type'], select[name*='filter'], "
                "select[id*='transType']"
            )
            if type_filter:
                try:
                    await self.page.select_option(
                        "select[name*='type'], select[name*='filter'], "
                        "select[id*='transType']",
                        label="Deposits"
                    )
                    await asyncio.sleep(1)
                except Exception:
                    pass

            # Set date range
            from_date = (date.today() - timedelta(days=days)).strftime(
                "%m/%d/%Y"
            )
            to_date = date.today().strftime("%m/%d/%Y")

            for sel in [
                "input[name*='from' i]", "input[name*='start' i]",
                "input[id*='from' i]", "input[id*='start' i]",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(from_date)
                    break

            for sel in [
                "input[name*='to' i]", "input[name*='end' i]",
                "input[id*='to' i]", "input[id*='end' i]",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(to_date)
                    break

            # Search
            search_btn = await self.page.query_selector(
                "button:has-text('Search'), button:has-text('Go'), "
                "button:has-text('View'), input[type='submit']"
            )
            if search_btn:
                await search_btn.click()
                await asyncio.sleep(3)

            deposits = await self._parse_transaction_rows()

        except Exception as e:
            self.logger.error("Failed to get BofA deposits", error=str(e))
            await self.screenshot("boa_deposits_error")

        return deposits

    async def _parse_transaction_rows(self) -> List[dict]:
        deposits = []
        try:
            rows = await self.page.query_selector_all(
                "table tbody tr, .transaction-row, "
                ".activity-row, .trans-row"
            )
            for row in rows:
                text = await row.inner_text()
                if any(kw in text.lower() for kw in (
                    "credit", "deposit", "eft", "ach", "wire",
                    "incoming", "received"
                )):
                    amount_match = re.search(r'\$?([\d,]+\.\d{2})', text)
                    date_match = re.search(r'(\d{1,2}/\d{1,2}/\d{2,4})', text)
                    if amount_match:
                        deposits.append({
                            "date": date_match.group(1) if date_match else "",
                            "amount": float(
                                amount_match.group(1).replace(",", "")
                            ),
                            "description": text.strip()[:200],
                            "reference": "",
                            "bank": self.BANK_NAME,
                        })
        except Exception as e:
            self.logger.warning("Transaction parse error", error=str(e))
        return deposits


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

BANK_PORTAL_MAP = {
    "KJLN": WellsFargoPortal,
    "MARYS_HOME": SouthernBankPortal,
    "NHCS": BankOfAmericaPortal,
}


def get_bank_portal(program: str, headless: bool = True) -> Optional[BankPortalBase]:
    """Get the bank portal for a given program."""
    cls = BANK_PORTAL_MAP.get(program)
    return cls(headless=headless) if cls else None


def get_all_bank_portals(headless: bool = True) -> List[BankPortalBase]:
    """Get all bank portal instances."""
    return [cls(headless=headless) for cls in BANK_PORTAL_MAP.values()]
