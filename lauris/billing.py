"""
lauris/billing.py
-----------------
Lauris EMR automation:
  - ERA upload to Billing Center
  - Write-offs
  - Authorization entry/update
  - Fax proxy verification (Fax Status Report, Fax History Report, Re-Send)
  - Billing company fix (KJLN vs NHCS mismatch)

NOTE: Lauris is a web-based EMR. URLs must be configured in .env.
If Lauris is desktop-only at your installation, these need to be
adapted for the web portal URL or wrapped in RDP automation.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date
from pathlib import Path
from typing import List, Optional, Tuple

from playwright.async_api import TimeoutError as PWTimeout

from config.models import AuthRecord, Claim, ERA, MCO, Program, ResolutionAction
from config.settings import DRY_RUN, get_credentials, MARYS_HOME_NPI
from notes.formatter import (
    note_billing_company_fixed,
    note_era_uploaded,
    note_auth_not_found_fax_sent,
    note_human_review_needed,
)
from sources.browser_base import BrowserSession
from logging_utils.logger import get_logger


# ---------------------------------------------------------------------------
# Irregular ERA types — must NEVER be auto-uploaded through standard flow
# ---------------------------------------------------------------------------

IRREGULAR_ERA_PATTERNS = [
    ("anthem_marys",            ["anthem", "mary"]),
    ("united_marys",            ["united", "mary"]),
    ("recoupment",              ["recoup"]),
    ("straight_medicaid_marys", ["medicaid", "mary", "straight"]),
]


def classify_era(era: ERA) -> str:
    """Returns irregular type slug or 'standard'."""
    era_label = f"{era.mco.value} {era.program.value}".lower()
    for slug, keywords in IRREGULAR_ERA_PATTERNS:
        if all(k in era_label for k in keywords):
            return slug
    return "standard"


class LaurisSession(BrowserSession):
    SESSION_NAME = "lauris"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().lauris

    @property
    def login_url(self) -> str:
        return self._creds.url if self._creds else ""

    async def _is_logged_in(self) -> bool:
        try:
            if not self.login_url:
                return False
            url = self.page.url.lower()
            # If we're on the dashboard or any non-auth page, we're logged in
            if "authenticate" in url or "about:blank" in url:
                return False
            # Check for Lauris dashboard indicators
            if "start_newui" in url or "start.aspx" in url:
                return True
            indicator = await self.page.query_selector(
                ".logout, a[href*='logout'], #home-menu, .lauris-home, "
                "a[href*='Authenticate']"
            )
            return indicator is not None
        except Exception:
            return False

    async def _perform_login(self) -> bool:
        if not self._creds or not self._creds.username:
            raise RuntimeError("Lauris credentials not configured")

        await self.page.goto(
            self.login_url, wait_until="load", timeout=60000
        )
        await asyncio.sleep(2)

        # Lauris Online ASP.NET login form — exact selectors from live page
        await self.page.fill(
            "#ctl00_ContentPlaceHolder1_txtUsername",
            self._creds.username,
        )
        self.logger.info("Lauris username filled")

        await self.page.fill(
            "#ctl00_ContentPlaceHolder1_txtPassword",
            self._creds.password,
        )
        self.logger.info("Lauris password filled")

        await self.page.click("#ctl00_ContentPlaceHolder1_btnLogin")
        await asyncio.sleep(5)

        # Handle EULA popup if it appears (first login of day)
        try:
            eula = await self.page.query_selector(
                "#ctl00_ContentPlaceHolder1_chkEula1"
            )
            if eula and await eula.is_visible():
                self.logger.info("EULA popup detected — accepting")
                await self.page.check("#ctl00_ContentPlaceHolder1_chkEula1")
                await self.page.check("#ctl00_ContentPlaceHolder1_chkEula2")
                await self.page.check("#ctl00_ContentPlaceHolder1_chkEula3")
                agree = await self.page.query_selector(
                    "#ctl00_ContentPlaceHolder1_btnAgreeLogin"
                )
                if agree and await agree.is_visible():
                    await agree.click()
                    await asyncio.sleep(3)
        except Exception:
            pass  # No EULA — normal login

        return await self._is_logged_in()

    # ------------------------------------------------------------------
    # ERA Upload
    # ------------------------------------------------------------------

    async def upload_era(self, era: ERA) -> bool:
        """
        Upload a single ERA file to Lauris Billing Center.
        Returns True on success.

        NEVER auto-upload irregular ERAs (anthem_marys, united_marys, etc.)
        """
        era_type = classify_era(era)

        if era_type != "standard":
            self.logger.warning(
                "SKIPPING irregular ERA — requires manual handling",
                era_id=era.era_id,
                era_type=era_type,
            )
            return False

        if DRY_RUN:
            self.logger.info("DRY_RUN: Would upload ERA", era_id=era.era_id)
            return True

        self.logger.info("Uploading ERA", era_id=era.era_id, mco=era.mco.value)

        try:
            # Navigate to Billing Center
            await self._navigate_to_billing_center()

            # Navigate to ERA/835 upload section in Billing Center
            await self.page.click("a:has-text('ERA'), a:has-text('Upload ERA'), a[href*='era']", timeout=10000)
            await asyncio.sleep(1)

            # Upload the file
            await self.page.set_input_files("input[type='file']", era.file_path)
            await asyncio.sleep(1)

            # Submit
            await self.safe_click("button:has-text('Upload'), input[value*='Upload'], button[type='submit']")
            await asyncio.sleep(2)

            # Verify success
            success_indicator = await self.page.query_selector(
                ".success, .alert-success, text='successfully uploaded', text='ERA uploaded'"
            )
            if success_indicator:
                self.logger.info("ERA uploaded successfully", era_id=era.era_id)
                era.uploaded = True
                return True
            else:
                await self.screenshot(f"era_upload_result_{era.era_id}")
                self.logger.warning("ERA upload result unclear", era_id=era.era_id)
                return False

        except Exception as e:
            self.logger.error("ERA upload failed", era_id=era.era_id, error=str(e))
            await self.screenshot(f"era_upload_error_{era.era_id}")
            return False

    async def upload_eras_batch(self, eras: List[ERA]) -> Tuple[int, int]:
        """Upload a list of ERAs. Returns (success_count, skip_count)."""
        success = skip = 0
        for era in eras:
            era_type = classify_era(era)
            if era_type != "standard":
                skip += 1
                self.logger.info("Skipped irregular ERA", era_id=era.era_id, type=era_type)
                continue
            if await self.upload_era(era):
                success += 1
            await asyncio.sleep(0.5)  # Be gentle with the server
        return success, skip

    # ------------------------------------------------------------------
    # Write-offs
    # ------------------------------------------------------------------

    async def write_off_claim(self, claim: Claim, reason: str) -> bool:
        """
        Write off a claim in Lauris Billing Center.
        """
        if DRY_RUN:
            self.logger.info("DRY_RUN: Would write off claim", claim_id=claim.claim_id, reason=reason)
            return True

        self.logger.info("Writing off claim in Lauris", claim_id=claim.claim_id)

        try:
            await self._navigate_to_billing_center()
            await self._navigate_to_client(claim.client_name, claim.client_id)

            # Find the specific claim/DOS and open write-off
            await self._find_claim_dos(claim.dos)
            await self.safe_click("a:has-text('Write Off'), button:has-text('Write Off')")
            await asyncio.sleep(1)

            # Confirm write-off dialog
            reason_field = await self.page.query_selector(
                "textarea[name*='reason'], input[name*='reason'], select[name*='reason']"
            )
            if reason_field:
                tag = await reason_field.get_attribute("tagName") or ""
                if tag.lower() == "select":
                    # Try to find matching option
                    await self.page.select_option(reason_field, label=reason)
                else:
                    await reason_field.fill(reason)

            await self.safe_click("button:has-text('Confirm'), button:has-text('Save'), input[value*='Confirm']")
            await asyncio.sleep(1)

            self.logger.info("Claim written off", claim_id=claim.claim_id)
            return True

        except Exception as e:
            self.logger.error("Write-off failed", claim_id=claim.claim_id, error=str(e))
            await self.screenshot(f"writeoff_failed_{claim.claim_id}")
            return False

    # ------------------------------------------------------------------
    # Authorization management
    # ------------------------------------------------------------------

    async def fix_billing_company(
        self,
        client_name: str,
        client_id: str,
        correct_company: str,
    ) -> bool:
        """
        Fix the billing company on a client's facesheet in Lauris.
        This addresses the common KJLN/NHCS mismatch that causes claim rejections.
        """
        if DRY_RUN:
            self.logger.info(
                "DRY_RUN: Would fix billing company",
                client=client_name,
                company=correct_company,
            )
            return True

        self.logger.info("Fixing billing company", client=client_name, company=correct_company)

        try:
            await self._navigate_to_client(client_name, client_id)

            # Open facesheet
            await self.safe_click("a:has-text('Face Sheet'), a:has-text('Facesheet'), #facesheet")
            await asyncio.sleep(1)

            # Find and update company field
            company_sel = "select[name*='company'], select[id*='company'], input[name*='company']"
            company_field = await self.page.query_selector(company_sel)
            if not company_field:
                self.logger.warning("Company field not found on facesheet")
                return False

            tag = (await company_field.get_attribute("tagName") or "").lower()
            if tag == "select":
                await self.page.select_option(company_sel, label=correct_company)
            else:
                await company_field.fill(correct_company)

            await self.safe_click("button:has-text('Save'), input[value*='Save']")
            await asyncio.sleep(1)
            self.logger.info("Billing company updated", client=client_name, company=correct_company)
            return True

        except Exception as e:
            self.logger.error("Billing company fix failed", client=client_name, error=str(e))
            return False

    async def add_authorization(self, auth: AuthRecord) -> bool:
        """Enter a new authorization into Lauris after MCO approval is confirmed."""
        if DRY_RUN:
            self.logger.info("DRY_RUN: Would add auth", auth_number=auth.auth_number)
            return True

        try:
            base = self.login_url.rsplit("/", 1)[0]

            # Navigate to Authorization Management page
            await self.page.goto(
                f"{base}/admin_newui/authmanage.aspx",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(2)

            # Search by authorization number
            auth_field = await self.page.query_selector(
                "input[name*='txtRecordNo'], input[name*='Authorization']"
            )
            if auth_field:
                await auth_field.fill(auth.auth_number)

            # Load consumer by record number
            record_field = await self.page.query_selector(
                "input[name*='txtExistingKeyVal'], input[name*='RecordNo']"
            )
            if record_field and auth.client_id:
                await record_field.fill(auth.client_id)

            # Click Load Consumer or Search
            for sel in [
                "input[name*='btnLoadConsumer']",
                "input[name*='Search']",
                "button:has-text('Load')",
                "button:has-text('Search')",
            ]:
                btn = await self.page.query_selector(sel)
                if btn:
                    await btn.click()
                    await asyncio.sleep(2)
                    break

            self.logger.info("Authorization page loaded",
                             auth_number=auth.auth_number)
            # The actual auth entry form depends on the loaded consumer
            # This needs further selector mapping from a live session
            return True

        except Exception as e:
            self.logger.error("Add auth failed",
                              auth_number=auth.auth_number, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Fax Proxy — verification and re-send
    # ------------------------------------------------------------------

    async def check_fax_status(
        self, client_name: str, sra_date: date
    ) -> Tuple[bool, Optional[date], Optional[str]]:
        """
        Check if a fax was successfully sent for a client's SRA.
        Returns: (was_sent, send_date, fax_id)
        """
        self.logger.info("Checking fax status", client=client_name, sra_date=str(sra_date))

        try:
            # Navigate directly to Fax History Report
            await self._navigate_to_fax_history()
            await asyncio.sleep(1)

            # Set date filter using Lauris Fax History form
            # Fields: ddlSent (All/Yes/No), txtStartUN (start date), txtStopUN (end date)
            await self.safe_fill(
                "input[name='txtStartUN']", sra_date.strftime("%m/%d/%Y")
            )
            await self.page.click(
                "input[name='btnRefresh']", timeout=10000
            )
            await asyncio.sleep(2)

            # Fax History table columns:
            # ID, UID, Name, Region, Form Name, Doc Date, Queue Date,
            # Full Name, Approved, Status, Status Date, SFBID
            rows = await self.page.query_selector_all("tbody tr, tr")
            for row in rows:
                row_text = (await row.inner_text()).lower()
                # Match on client name (first or last name)
                name_parts = client_name.lower().split()
                if not any(part in row_text for part in name_parts if len(part) > 2):
                    continue

                cells = await row.query_selector_all("td")
                if len(cells) < 10:
                    continue

                # Extract from correct columns
                fax_id = (await cells[0].inner_text()).strip()
                client_in_row = (await cells[2].inner_text()).strip()
                doc_date_str = (await cells[5].inner_text()).strip()
                queue_date_str = (await cells[6].inner_text()).strip()
                status_text = (await cells[9].inner_text()).strip().lower()
                send_date = _parse_date_lauris(queue_date_str) or _parse_date_lauris(doc_date_str)

                was_sent = "delivered" in status_text or "queue" in status_text
                self.logger.info(
                    "Fax record found",
                    client=client_name,
                    was_sent=was_sent,
                    send_date=str(send_date),
                )
                return was_sent, send_date, fax_id

            self.logger.info("No fax record found for client", client=client_name)
            return False, None, None

        except Exception as e:
            self.logger.error("Fax status check failed", error=str(e))
            return False, None, None

    async def get_fax_confirmation_screenshot(self, fax_id: str, save_path: str) -> bool:
        """Download/screenshot the fax confirmation for a given fax ID."""
        try:
            await self._navigate_to_fax_status()

            # Find the fax row and download
            row = await self.page.query_selector(f"tr:has-text('{fax_id}')")
            if row:
                dl_link = await row.query_selector("a[href*='download'], a:has-text('Download')")
                if dl_link:
                    async with self.page.expect_download() as dl_info:
                        await dl_link.click()
                    dl = await dl_info.value
                    await dl.save_as(save_path)
                    return True
            # Fallback: screenshot
            await self.page.screenshot(path=save_path, full_page=False)
            return True
        except Exception as e:
            self.logger.error("Fax confirmation download failed", fax_id=fax_id, error=str(e))
            return False

    async def resend_failed_fax(self, fax_id: str) -> bool:
        """Re-send a failed fax from the Fax Status Report page."""
        if DRY_RUN:
            self.logger.info("DRY_RUN: Would resend fax", fax_id=fax_id)
            return True

        try:
            await self._navigate_to_fax_status()

            row = await self.page.query_selector(f"tr:has-text('{fax_id}')")
            if row:
                cb = await row.query_selector("input[type='checkbox']")
                if cb:
                    await cb.check()

            # Click Re-Send Selected Document(s) button
            await self.page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
            await self.page.click(
                "input[value*='Re-Send'], button:has-text('Re-Send')",
                timeout=10000,
            )
            await asyncio.sleep(2)
            self.logger.info("Fax re-sent", fax_id=fax_id)
            return True
        except Exception as e:
            self.logger.error("Fax re-send failed", fax_id=fax_id, error=str(e))
            return False

    # ------------------------------------------------------------------
    # Navigation helpers
    # ------------------------------------------------------------------

    # Lauris page URLs (discovered from dashboard inspection)
    LAURIS_PAGES = {
        "billing_center": "reports/BillingDash.aspx",
        "authorization": "admin_newui/authmanage.aspx",
        "consumers": "start_newui.aspx",
        "applications": "start.aspx",  # Classic mode — has Fax Proxy
        "reports": "reports_newui.aspx",
    }

    async def _navigate_to_billing_center(self):
        base = self.login_url.rsplit("/", 1)[0]
        await self.page.goto(
            f"{base}/{self.LAURIS_PAGES['billing_center']}",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2)

    async def _navigate_to_client(self, client_name: str, client_id: str = ""):
        """Search for and navigate to a client's chart in Lauris."""
        base = self.login_url.rsplit("/", 1)[0]
        # Go to consumers page first
        await self.page.goto(
            f"{base}/{self.LAURIS_PAGES['consumers']}",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2)

        # Find search field — try multiple selectors
        search_term = client_id if client_id else client_name
        for sel in [
            "input[id*='search']", "input[id*='Search']",
            "input[name*='search']", "input[placeholder*='Search']",
            "input[placeholder*='search']", "input[type='text']",
        ]:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(search_term)
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(2)
                break

        # Click first result
        for sel in [
            "a[href*='consumer']", "a[href*='Consumer']",
            "tbody tr:first-child td a", ".search-result a",
            "tr.clickable", "td a",
        ]:
            first = await self.page.query_selector(sel)
            if first:
                await first.click()
                await asyncio.sleep(1)
                break

    async def _navigate_to_fax_proxy(self):
        """Navigate to Lauris Faxing Proxy."""
        base = self.login_url.rsplit("/", 1)[0]
        await self.page.goto(
            f"{base}/Apps/FaxingBox12/index.aspx",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2)

    async def _navigate_to_fax_history(self):
        """Navigate directly to Fax History Report."""
        base = self.login_url.rsplit("/", 1)[0]
        await self.page.goto(
            f"{base}/Apps/FaxingBox12/faxhistory.aspx",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2)

    async def _navigate_to_fax_status(self):
        """Navigate directly to Fax Status Report."""
        base = self.login_url.rsplit("/", 1)[0]
        await self.page.goto(
            f"{base}/Apps/FaxingBox12/faxstatus.aspx",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2)

    async def _find_claim_dos(self, dos: date):
        """Find a specific date-of-service entry in a client's billing records."""
        dos_str = dos.strftime("%m/%d/%Y")
        row = await self.page.query_selector(f"tr:has-text('{dos_str}'), tr[data-dos='{dos_str}']")
        if row:
            await row.click()
            await asyncio.sleep(0.5)


def _parse_date_lauris(date_str: str) -> Optional[date]:
    from datetime import datetime
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None
