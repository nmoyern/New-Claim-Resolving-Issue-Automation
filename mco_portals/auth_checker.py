"""
mco_portals/auth_checker.py
----------------------------
Auth status verification across all MCO portals:
  - Sentara (direct portal + Duo MFA)
  - United (uhcprovider.com)
  - Molina, Anthem, Aetna (all via Availity)
  - Kepro/Atrezzo
  - United reconsideration submission (TrackIt)

Each MCO class follows the step-by-step navigation from Admin Manual.
"""
from __future__ import annotations

import asyncio
from abc import abstractmethod
from datetime import date
from typing import Optional, Tuple

from config.models import AuthRecord, Claim, MCO, Program
from config.settings import (
    DRY_RUN,
    get_credentials,
    MARYS_HOME_NPI,
    MARYS_HOME_TAX_ID,
    ORG_MARYS_HOME,
)
from sources.browser_base import BrowserSession
from logging_utils.logger import get_logger


# ---------------------------------------------------------------------------
# Base MCO portal
# ---------------------------------------------------------------------------

class MCOPortalBase(BrowserSession):
    """Base for MCO-specific portal automation."""

    MCO_NAME: MCO = MCO.UNKNOWN

    async def check_auth(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """
        Check if an auth exists in the MCO portal for this claim's DOS.
        Returns: (found, AuthRecord or None)
        """
        raise NotImplementedError

    async def _is_logged_in(self) -> bool:
        try:
            url = self.page.url.lower()
            # If on a login/signin page or MFA page, not logged in
            if any(kw in url for kw in (
                "login", "signin", "authenticate", "logon", "about:blank",
                "duosecurity", "duo.com", "frame/v4/auth",
            )):
                return False
            # Check for common logged-in indicators
            for sel in [
                "a[href*='logout']", "a[href*='signout']",
                ".user-menu", ".signed-in", ".nav-user",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    return True
            # If we're not on a login page, cookies probably worked
            return "login" not in url
        except Exception:
            return False


# ---------------------------------------------------------------------------
# Sentara
# ---------------------------------------------------------------------------

class SentaraPortal(MCOPortalBase):
    SESSION_NAME = "sentara"
    MCO_NAME = MCO.SENTARA
    PORTAL_URL = "https://apps.sentarahealthplans.com/providers/login/login.aspx"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().sentara

    @property
    def login_url(self) -> str:
        return self.PORTAL_URL

    async def _perform_login(self) -> bool:
        if not self._creds:
            raise RuntimeError("Sentara credentials not configured")
        await self.page.goto(
            self.login_url, wait_until="load", timeout=30000
        )
        await asyncio.sleep(2)
        # Sentara ASP.NET form — exact selectors from live page
        await self.page.fill(
            "#BodyContentPlaceHolder_ctrlLogin_UserName",
            self._creds.username,
        )
        await self.page.fill(
            "#BodyContentPlaceHolder_ctrlLogin_Password",
            self._creds.password,
        )
        await self.page.click(
            "#BodyContentPlaceHolder_ctrlLogin_LoginImageButton"
        )
        await asyncio.sleep(5)
        # Sentara uses SMS/text MFA — select text option to Nick's phone
        if not await self._is_logged_in():
            await self._handle_sentara_sms_mfa()
        return await self._is_logged_in()

    async def _handle_sentara_sms_mfa(self):
        """
        Sentara MFA: select 'text' option, choose Nick's number (276-806-4418),
        then wait for the code to be entered.
        """
        NICK_PHONE = "276-806-4418"
        try:
            # Look for text/SMS option in MFA selection screen
            # Try clicking the text/SMS option
            text_options = [
                f"text='{NICK_PHONE}'",
                "label:has-text('Text')", "label:has-text('SMS')",
                "input[value*='sms']", "input[value*='text']",
                "button:has-text('Text')", "button:has-text('Send text')",
                "a:has-text('Text me')", "a:has-text('text message')",
                f"option:has-text('{NICK_PHONE}')",
            ]
            clicked = False
            for sel in text_options:
                try:
                    el = await self.page.query_selector(sel)
                    if el:
                        await el.click()
                        clicked = True
                        self.logger.info("Selected SMS/text MFA option", selector=sel)
                        break
                except Exception:
                    continue

            if not clicked:
                # Try to find and click any element containing Nick's phone number
                try:
                    await self.page.click(f"text='{NICK_PHONE}'", timeout=3000)
                    clicked = True
                except Exception:
                    pass

            if not clicked:
                # Try radio buttons or checkboxes near the phone number
                try:
                    phone_el = await self.page.query_selector(f"*:has-text('{NICK_PHONE}')")
                    if phone_el:
                        radio = await phone_el.query_selector("input[type='radio'], input[type='checkbox']")
                        if radio:
                            await radio.click()
                            clicked = True
                except Exception:
                    pass

            if clicked:
                # Click send/submit button
                await asyncio.sleep(0.5)
                for btn_sel in [
                    "button:has-text('Send')", "button:has-text('Continue')",
                    "button:has-text('Submit')", "input[type='submit']",
                ]:
                    try:
                        await self.page.click(btn_sel, timeout=2000)
                        break
                    except Exception:
                        continue

            # Now wait for the code to be entered — poll for login success
            self.logger.warning(
                "SENTARA SMS MFA: Code sent to 276-806-4418. "
                "Enter the code in the browser when received.",
                portal="sentara",
            )
            # Wait for human to enter the SMS code (up to 3 minutes)
            for i in range(36):
                await asyncio.sleep(5)
                if await self._is_logged_in():
                    self.logger.info("Sentara MFA completed successfully")
                    return
                # Check if there's a code input field and try to find it
                code_field = await self.page.query_selector(
                    "input[name*='code'], input[name*='otp'], input[placeholder*='code'], "
                    "input[type='tel'], input[maxlength='6']"
                )
                if code_field and i > 0:
                    self.logger.info("Waiting for SMS code entry...", seconds_elapsed=(i+1)*5)

            self.logger.warning("Sentara MFA timed out after 3 minutes")

        except Exception as e:
            self.logger.error("Sentara SMS MFA failed", error=str(e))
            await self.screenshot("sentara_mfa_error")

    async def check_auth(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        self.logger.info("Checking Sentara auth", client=claim.client_name, dos=str(claim.dos))

        try:
            # Menu → My Members → search by last name
            await self.safe_click("text='Menu', a:has-text('Menu')")
            await asyncio.sleep(0.5)
            await self.safe_click("text='My Members', a:has-text('My Members')")
            await asyncio.sleep(1)

            # Search by member last name
            last_name = claim.client_name.split()[-1]
            await self.safe_fill("input[name*='last'], input[id*='last_name']", last_name)
            await self.safe_click("button:has-text('Search'), input[value*='Search']")
            await asyncio.sleep(2)

            # Find the member row and click View Member Abstract
            member_row = await self.page.query_selector(
                f"tr:has-text('{claim.client_name.split()[0]}'), .member-row"
            )
            if not member_row:
                self.logger.info("Member not found in Sentara portal", client=claim.client_name)
                return False, None

            # Look for authorization with correct provider
            gear = await member_row.query_selector(".gear, button[aria-label*='gear'], button.action")
            if gear:
                await gear.click()
                await asyncio.sleep(0.5)
                await self.safe_click("text='View Member Abstract'")
                await asyncio.sleep(1)

            # Find matching auth (by DOS range and procedure code)
            auth = await self._find_auth_in_page(claim)
            if auth:
                self.logger.info("Sentara auth found", auth_number=auth.auth_number)
                return True, auth

            return False, None

        except Exception as e:
            self.logger.error("Sentara auth check failed", error=str(e))
            await self.screenshot(f"sentara_auth_error_{claim.claim_id}")
            return False, None

    async def _find_auth_in_page(self, claim: Claim) -> Optional[AuthRecord]:
        """Scan current page for an auth matching claim's DOS."""
        try:
            rows = await self.page.query_selector_all("tr.auth-row, .authorization-row, tbody tr")
            for row in rows:
                text = await row.inner_text()
                # Check if DOS falls within auth date range in this row
                # and procedure code matches
                if claim.dos.strftime("%m/%d/%Y") in text or str(claim.dos.year) in text:
                    auth_num_match = __import__("re").search(r"[A-Z0-9]{8,}", text)
                    if auth_num_match:
                        return AuthRecord(
                            client_id=claim.client_id,
                            client_name=claim.client_name,
                            mco=MCO.SENTARA,
                            program=claim.program,
                            auth_number=auth_num_match.group(0),
                            proc_code="",
                            start_date=claim.dos,
                            end_date=claim.dos,
                            status="approved",
                            source="portal",
                        )
        except Exception as e:
            self.logger.warning("Auth page scan error", error=str(e))
        return None


# ---------------------------------------------------------------------------
# United / UHC
# ---------------------------------------------------------------------------

class UnitedPortal(MCOPortalBase):
    SESSION_NAME = "united"
    MCO_NAME = MCO.UNITED
    PORTAL_URL = "https://www.uhcprovider.com"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().united

    @property
    def login_url(self) -> str:
        return self.PORTAL_URL

    async def _perform_login(self) -> bool:
        if not self._creds:
            raise RuntimeError("United credentials not configured")
        await self.page.goto(self.login_url)
        await self.safe_fill("input[name*='user'], input[id*='user']", self._creds.username)
        await self.safe_fill("input[type='password']", self._creds.password)
        await self.safe_click("button[type='submit'], input[type='submit']")
        await asyncio.sleep(2)
        if self._creds.mfa_type != "none":
            await self.handle_mfa(self._creds.mfa_type)
        return await self._is_logged_in()

    async def check_auth(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """
        United: Prior Authorizations → search by actual authorization dates
        (not just last 7 days).
        Note: United auths are NOT faxed. If not found here,
              create urgent ClickUp — do NOT check fax.
        """
        self.logger.info("Checking United auth", client=claim.client_name)

        try:
            # Ensure correct company is selected
            company_sel = (
                "select.company, .company-selector, input[name*='company']"
            )
            company_el = await self.page.query_selector(company_sel)
            if company_el:
                await self.page.select_option(
                    company_sel, label=claim.program.value
                )
                await asyncio.sleep(0.5)

            # Navigate to Prior Authorizations
            await self.safe_click(
                "a:has-text('Prior Auth'), "
                "a[href*='prior-auth'], "
                "text='Prior Authorizations'"
            )
            await asyncio.sleep(1)

            # Scroll to search criteria
            await self.page.evaluate("window.scrollTo(0, 500)")

            # Search by actual DOS date range (not "last 7 days")
            from datetime import timedelta
            search_start = (claim.dos - timedelta(days=30)).strftime("%m/%d/%Y")
            search_end = (claim.dos + timedelta(days=30)).strftime("%m/%d/%Y")

            # Try date range fields if available
            for sel in [
                "input[name*='from_date']", "input[name*='start_date']",
                "input[id*='fromDate']", "input[id*='startDate']",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(search_start)
                    break

            for sel in [
                "input[name*='to_date']", "input[name*='end_date']",
                "input[id*='toDate']", "input[id*='endDate']",
            ]:
                el = await self.page.query_selector(sel)
                if el:
                    await el.fill(search_end)
                    break

            # If date-range fields not found, try "All" option
            all_opt = await self.page.query_selector(
                "input[value*='all'], label:has-text('All'), "
                "option:has-text('All')"
            )
            if all_opt:
                try:
                    await all_opt.click()
                except Exception:
                    pass
            await asyncio.sleep(0.5)

            # Fill member ID or name
            if claim.client_id:
                await self.safe_fill(
                    "input[name*='member_id'], input[id*='memberId']",
                    claim.client_id,
                )
            await self.safe_click(
                "button:has-text('Search'), input[value*='Search']"
            )
            await asyncio.sleep(2)

            auth = await self._find_auth_in_page(claim)
            if auth:
                self.logger.info(
                    "United auth found", auth_number=auth.auth_number
                )
                return True, auth

            self.logger.info(
                "United auth NOT found — this is NOT a fax issue for United. "
                "Trigger urgent ClickUp to resend.",
                client=claim.client_name,
            )
            return False, None

        except Exception as e:
            self.logger.error("United auth check failed", error=str(e))
            return False, None

    async def submit_reconsideration_trackit(self, claim: Claim, auth_pdf_path: str) -> bool:
        """
        Submit reconsideration via United's TrackIt page (not Claim.MD).
        """
        if DRY_RUN:
            self.logger.info("DRY_RUN: Would submit United recon via TrackIt", claim_id=claim.claim_id)
            return True

        try:
            await self.safe_click("a:has-text('TrackIt'), a[href*='trackit']")
            await asyncio.sleep(1)

            # Find claim and submit reconsideration
            await self.safe_fill("input[name*='claim'], input[id*='claim']", claim.claim_id)
            await self.safe_click("button:has-text('Find'), button:has-text('Search')")
            await asyncio.sleep(1)

            recon_btn = await self.page.query_selector(
                "button:has-text('Reconsideration'), a:has-text('Submit Reconsideration')"
            )
            if recon_btn:
                await recon_btn.click()
                await asyncio.sleep(1)

                # Upload auth PDF
                await self.page.set_input_files("input[type='file']", auth_pdf_path)
                await asyncio.sleep(0.5)

                await self.safe_click("button:has-text('Submit'), input[value*='Submit']")
                await asyncio.sleep(2)
                self.logger.info("United recon submitted via TrackIt", claim_id=claim.claim_id)
                return True
        except Exception as e:
            self.logger.error("United TrackIt recon failed", error=str(e))
        return False

    async def _find_auth_in_page(self, claim: Claim) -> Optional[AuthRecord]:
        try:
            rows = await self.page.query_selector_all("tr.auth, .auth-row, tbody tr")
            for row in rows:
                text = await row.inner_text()
                if claim.client_name.split()[-1].lower() in text.lower():
                    import re
                    auth_match = re.search(r"\b[A-Z0-9]{8,}\b", text)
                    if auth_match:
                        return AuthRecord(
                            client_id=claim.client_id,
                            client_name=claim.client_name,
                            mco=MCO.UNITED,
                            program=claim.program,
                            auth_number=auth_match.group(0),
                            proc_code="",
                            start_date=claim.dos,
                            end_date=claim.dos,
                            status="approved",
                            source="portal",
                        )
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Availity (covers Molina, Anthem, Aetna)
# ---------------------------------------------------------------------------

class AvailityPortal(MCOPortalBase):
    SESSION_NAME = "availity"
    PORTAL_URL = "https://apps.availity.com/"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().availity

    @property
    def login_url(self) -> str:
        return self._creds.url if self._creds else self.PORTAL_URL

    async def _perform_login(self) -> bool:
        if not self._creds:
            raise RuntimeError("Availity credentials not configured")
        await self.page.goto(
            self.login_url, wait_until="load", timeout=30000
        )
        await asyncio.sleep(3)
        # Availity login form — exact selectors from live page
        await self.page.fill("#userId", self._creds.username)
        await self.page.fill("#password", self._creds.password)
        await self.page.click("button[type='submit']")
        await asyncio.sleep(5)

        # Handle 2-Step Authentication — select "Text me at 4418"
        try:
            # Look for the text option with 4418
            text_option = await self.page.query_selector(
                "label:has-text('4418'), input[value*='4418'], "
                "label:has-text('Text me')"
            )
            if text_option:
                await text_option.click()
                self.logger.info("Selected 'Text me at 4418' for Availity MFA")
                await asyncio.sleep(1)
                # Click Continue
                continue_btn = await self.page.query_selector(
                    "button:has-text('Continue'), input[value*='Continue']"
                )
                if continue_btn:
                    await continue_btn.click()
                    await asyncio.sleep(3)

                # Wait for code entry — poll for login completion
                self.logger.warning(
                    "AVAILITY MFA: Code sent to 4418. "
                    "Enter the code in the browser.",
                    portal="availity",
                )
                for i in range(36):  # 3 minutes
                    await asyncio.sleep(5)
                    if await self._is_logged_in():
                        self.logger.info("Availity MFA completed")
                        return True
                    if i % 6 == 0 and i > 0:
                        self.logger.info(
                            "Waiting for Availity MFA...",
                            seconds_elapsed=(i + 1) * 5,
                        )
            else:
                # No MFA page — might already be logged in
                self.logger.info("No Availity MFA prompt detected")
        except Exception as e:
            self.logger.warning("Availity MFA handling error", error=str(e))

        return await self._is_logged_in()

    async def check_auth_molina(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """
        Availity → Payer Spaces → Molina → Prior Auths
        """
        self.logger.info("Checking Molina auth via Availity", client=claim.client_name)
        try:
            await self.safe_click("a:has-text('Payer Spaces')")
            await asyncio.sleep(1)
            await self.safe_click("a:has-text('Molina')")
            await asyncio.sleep(1)
            await self.safe_click("a:has-text('Prior Auths'), a:has-text('Prior Authorization')")
            await asyncio.sleep(1)

            # Fill org fields (auto-populated once org is selected)
            org_sel = "select.organization, select[name*='org']"
            org_opt = await self.page.query_selector(org_sel)
            if org_opt:
                options = await self.page.eval_on_selector_all(f"{org_sel} option", "els => els.map(e => e.textContent)")
                if options:
                    await self.page.select_option(org_sel, index=1)  # First real option
                await asyncio.sleep(0.5)

            await self.safe_click("option:has-text('Service Request'), input[value*='Service Request']")
            await asyncio.sleep(0.3)
            await self.safe_click("button:has-text('Submit')")
            await asyncio.sleep(1)

            # Search by Refer to Provider + facility
            await self.safe_click("option:has-text('Refer to Provider'), input[value*='Refer']")
            await asyncio.sleep(0.3)

            # Set submission date range (few days before/after auth send date)
            await self._fill_date_range_around_dos(claim.dos, days_buffer=7)

            await self.safe_click("button:has-text('Search')")
            await asyncio.sleep(2)

            auth = await self._generic_find_auth(claim, MCO.MOLINA)
            return (auth is not None), auth

        except Exception as e:
            self.logger.error("Molina auth check failed", error=str(e))
            return False, None

    async def check_auth_anthem(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """
        Availity -> Patient Registration -> Authorizations and Referrals
        Check ALL three orgs (Mary's Home, KJLN, NHCS) since the
        authorization could be under any company.
        Payer: "Anthem - VA", Request Type: "Outpatient Authorization"
        """
        from config.settings import ORG_KJLN, ORG_NHCS
        orgs_to_check = [ORG_MARYS_HOME, ORG_KJLN, ORG_NHCS]

        self.logger.info(
            "Checking Anthem auth via Availity (all orgs)",
            client=claim.client_name,
        )

        for org_name in orgs_to_check:
            try:
                await self.safe_click(
                    "a:has-text('Patient Registration')"
                )
                await asyncio.sleep(0.5)
                await self.safe_click(
                    "a:has-text('Authorizations and Referrals')"
                )
                await asyncio.sleep(1)

                await self.page.select_option(
                    "select[name*='org']", label=org_name
                )
                await asyncio.sleep(0.3)
                await self.page.select_option(
                    "select[name*='payer']", label="Anthem - VA"
                )
                await asyncio.sleep(0.3)
                await self.page.select_option(
                    "select[name*='type']",
                    label="Outpatient Authorization",
                )
                await asyncio.sleep(0.3)

                await self.safe_fill(
                    "input[name*='npi']", MARYS_HOME_NPI
                )
                await self.safe_fill(
                    "input[name*='tax']", MARYS_HOME_TAX_ID
                )

                if claim.client_id:
                    await self.safe_fill(
                        "input[name*='member'], input[id*='memberId']",
                        claim.client_id,
                    )
                if claim.dos:
                    await self.safe_fill(
                        "input[name*='from_date'], "
                        "input[name*='service_from']",
                        claim.dos.strftime("%m/%d/%Y"),
                    )

                await self.safe_click("button:has-text('Submit')")
                await asyncio.sleep(2)

                auth = await self._generic_find_auth(claim, MCO.ANTHEM)
                if auth:
                    self.logger.info(
                        "Anthem auth found under org",
                        org=org_name,
                        auth=auth.auth_number,
                    )
                    return True, auth

                self.logger.info(
                    "Anthem auth not found under org, trying next",
                    org=org_name,
                )
            except Exception as e:
                self.logger.warning(
                    "Anthem auth check failed for org",
                    org=org_name,
                    error=str(e),
                )

        self.logger.info("Anthem auth not found under any org")
        return False, None

    async def check_auth_aetna(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """
        Availity → Patient Registration → Authorizations and Referrals
        Payer: "Aetna Better Health All Plans and NJ-VA MAPD-DSNP"
        """
        self.logger.info("Checking Aetna auth via Availity", client=claim.client_name)
        try:
            await self.safe_click("a:has-text('Patient Registration')")
            await asyncio.sleep(0.5)
            await self.safe_click("a:has-text('Authorizations and Referrals')")
            await asyncio.sleep(1)

            await self.page.select_option("select[name*='org']", label=ORG_MARYS_HOME)
            await asyncio.sleep(0.3)
            await self.page.select_option(
                "select[name*='payer']",
                label="Aetna Better Health All Plans and NJ-VA MAPD-DSNP",
            )
            await asyncio.sleep(0.3)
            await self.page.select_option("select[name*='type']", label="Outpatient Authorization")
            await asyncio.sleep(0.3)

            if claim.client_id:
                await self.safe_fill("input[name*='member'], input[id*='memberId']", claim.client_id)
            await self.safe_fill("input[name*='npi']", MARYS_HOME_NPI)

            await self.safe_click("button:has-text('Submit')")
            await asyncio.sleep(2)

            auth = await self._generic_find_auth(claim, MCO.AETNA)
            return (auth is not None), auth

        except Exception as e:
            self.logger.error("Aetna auth check failed", error=str(e))
            return False, None

    async def check_auth(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """Route to the right Availity sub-flow based on MCO."""
        if claim.mco == MCO.MOLINA:
            return await self.check_auth_molina(claim)
        if claim.mco == MCO.ANTHEM:
            return await self.check_auth_anthem(claim)
        if claim.mco == MCO.AETNA:
            return await self.check_auth_aetna(claim)
        if claim.mco == MCO.HUMANA:
            return await self.check_auth_humana(claim)
        self.logger.warning("MCO not handled by Availity portal", mco=claim.mco.value)
        return False, None

    async def check_auth_humana(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """Check Humana auth via Availity."""
        try:
            await self.page.goto(
                "https://apps.availity.com/availity/web/public.elegant.login",
                wait_until="load", timeout=30000,
            )
            await asyncio.sleep(3)
            # Navigate to auth lookup for Humana
            # Similar flow to Aetna
            return await self._generic_availity_auth(claim, MCO.HUMANA, "Humana")
        except Exception as e:
            self.logger.warning("Humana auth check failed", error=str(e)[:60])
            return False, None

    async def check_claim_status(
        self, claim: Claim
    ) -> Optional[dict]:
        """
        Check claim status in Availity for Aetna, Anthem, Humana, Molina.

        Returns dict with status info or None if unavailable:
        {
            "status": "paid" | "denied" | "pending" | "received" | "unknown",
            "paid_amount": float,
            "check_number": str,
            "denial_reason": str,
            "mco": str,
        }
        """
        if claim.mco not in (
            MCO.AETNA, MCO.ANTHEM, MCO.HUMANA, MCO.MOLINA,
        ):
            return None

        try:
            # Navigate to Claim Status page in Availity
            await self.page.goto(
                "https://apps.availity.com/public/apps/claim-status",
                wait_until="load",
                timeout=30000,
            )
            await asyncio.sleep(5)

            # Select payer
            payer_names = {
                MCO.AETNA: "Aetna",
                MCO.ANTHEM: "Anthem",
                MCO.HUMANA: "Humana",
                MCO.MOLINA: "Molina",
            }
            payer_name = payer_names.get(claim.mco, "")

            # Fill payer search
            payer_input = await self.page.query_selector(
                "input[name*='payer'], input[id*='payer'], "
                "input[placeholder*='payer' i], input[placeholder*='Payer' i]"
            )
            if payer_input:
                await payer_input.fill(payer_name)
                await asyncio.sleep(2)
                # Select from dropdown
                option = await self.page.query_selector(
                    f"li:has-text('{payer_name}'), "
                    f"div.option:has-text('{payer_name}')"
                )
                if option:
                    await option.click()
                    await asyncio.sleep(1)

            # Fill member ID
            member_input = await self.page.query_selector(
                "input[name*='member'], input[id*='member'], "
                "input[name*='subscriber'], input[placeholder*='Member' i]"
            )
            if member_input:
                await member_input.fill(claim.client_id)

            # Fill DOS
            dos_input = await self.page.query_selector(
                "input[name*='date'], input[id*='service_date'], "
                "input[placeholder*='Date' i]"
            )
            if dos_input:
                await dos_input.fill(claim.dos.strftime("%m/%d/%Y"))

            # Submit
            submit = await self.page.query_selector(
                "button[type='submit'], button:has-text('Submit'), "
                "button:has-text('Search')"
            )
            if submit:
                await submit.click()
                await asyncio.sleep(5)

            # Parse results
            body = await self.page.inner_text("body")
            body_lower = body.lower()

            result = {
                "status": "unknown",
                "paid_amount": 0.0,
                "check_number": "",
                "denial_reason": "",
                "mco": claim.mco.value,
            }

            if "paid" in body_lower or "finalized" in body_lower:
                result["status"] = "paid"
                # Try to extract amount
                import re
                amt = re.search(r"\$?([\d,]+\.\d{2})", body)
                if amt:
                    result["paid_amount"] = float(
                        amt.group(1).replace(",", "")
                    )
                chk = re.search(r"check\s*#?\s*(\d{6,})", body_lower)
                if chk:
                    result["check_number"] = chk.group(1)
            elif "denied" in body_lower or "reject" in body_lower:
                result["status"] = "denied"
                # Try to extract denial reason
                import re
                reason = re.search(
                    r"(?:denied|rejection|reason)[:\s]*([^\n]{10,60})",
                    body_lower,
                )
                if reason:
                    result["denial_reason"] = reason.group(1).strip()
            elif "pending" in body_lower or "processing" in body_lower:
                result["status"] = "pending"
            elif "received" in body_lower:
                result["status"] = "received"

            self.logger.info(
                "Availity claim status checked",
                claim_id=claim.claim_id,
                mco=claim.mco.value,
                status=result["status"],
            )
            return result

        except Exception as e:
            self.logger.warning(
                "Availity claim status check failed",
                claim_id=claim.claim_id,
                error=str(e)[:60],
            )
            return None

    async def _generic_availity_auth(
        self, claim: Claim, mco: MCO, payer_name: str,
    ) -> Tuple[bool, Optional[AuthRecord]]:
        """Generic Availity auth check flow."""
        try:
            # Navigate to auth/referral page
            await self.page.goto(
                "https://apps.availity.com/public/apps/auth-referral",
                wait_until="load", timeout=30000,
            )
            await asyncio.sleep(5)
            await self._fill_date_range_around_dos(claim.dos)
            auth = await self._generic_find_auth(claim, mco)
            if auth:
                return True, auth
        except Exception:
            pass
        return False, None

    async def _fill_date_range_around_dos(self, dos: date, days_buffer: int = 7):
        from datetime import timedelta
        start = dos - timedelta(days=days_buffer)
        end = dos + timedelta(days=days_buffer)
        start_str = start.strftime("%m/%d/%Y")
        end_str = end.strftime("%m/%d/%Y")
        try:
            await self.safe_fill("input[name*='start'], input[id*='start_date']", start_str)
            await self.safe_fill("input[name*='end'], input[id*='end_date']", end_str)
        except Exception:
            pass

    async def _generic_find_auth(self, claim: Claim, mco: MCO) -> Optional[AuthRecord]:
        """Generic auth finder for Availity result pages."""
        import re
        try:
            rows = await self.page.query_selector_all("tr.result, .auth-result, tbody tr")
            for row in rows:
                text = await row.inner_text()
                name_part = claim.client_name.split()[-1].lower()
                if name_part in text.lower():
                    auth_match = re.search(r"\b[A-Z0-9]{8,}\b", text)
                    status = "approved" if "approved" in text.lower() else "pending"
                    if auth_match:
                        return AuthRecord(
                            client_id=claim.client_id,
                            client_name=claim.client_name,
                            mco=mco,
                            program=claim.program,
                            auth_number=auth_match.group(0),
                            proc_code="",
                            start_date=claim.dos,
                            end_date=claim.dos,
                            status=status,
                            source="portal",
                        )
        except Exception:
            pass
        return None


# ---------------------------------------------------------------------------
# Kepro / Atrezzo
# ---------------------------------------------------------------------------

class KoproPortal(MCOPortalBase):
    SESSION_NAME = "kepro"
    MCO_NAME = MCO.DMAS
    PORTAL_URL = "https://portal.kepro.com/Home/Index"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().kepro

    @property
    def login_url(self) -> str:
        return self.PORTAL_URL

    async def _perform_login(self) -> bool:
        if not self._creds:
            raise RuntimeError("Kepro credentials not configured")
        await self.page.goto(self.login_url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)

        # Kepro redirects to Microsoft Azure AD login
        # Step 1: Enter email on Microsoft login page
        for sel in [
            "input[name='loginfmt']", "input[type='email']",
            "input[name*='user']", "input[id*='email']",
        ]:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(self._creds.username)
                break

        # Click Next on Microsoft page
        for sel in [
            "input[value='Next']", "button:has-text('Next')",
            "input[type='submit']", "button[type='submit']",
        ]:
            btn = await self.page.query_selector(sel)
            if btn:
                await btn.click()
                break
        await asyncio.sleep(3)

        # Step 2: Enter password
        for sel in [
            "input[name='passwd']", "input[type='password']",
            "input[name='Password']",
        ]:
            el = await self.page.query_selector(sel)
            if el:
                await el.fill(self._creds.password)
                break

        # Click Sign In
        for sel in [
            "input[value='Sign in']", "button:has-text('Sign in')",
            "input[type='submit']", "button[type='submit']",
        ]:
            btn = await self.page.query_selector(sel)
            if btn:
                await btn.click()
                break
        await asyncio.sleep(3)

        # Handle "Stay signed in?" prompt if it appears
        stay_btn = await self.page.query_selector(
            "input[value='Yes'], button:has-text('Yes')"
        )
        if stay_btn:
            await stay_btn.click()
            await asyncio.sleep(2)

        return await self._is_logged_in()

    async def check_auth(self, claim: Claim) -> Tuple[bool, Optional[AuthRecord]]:
        """
        Kepro: Cases → Case Type "UM" → date range search → View procedures
        """
        self.logger.info("Checking Kepro/DMAS auth", client=claim.client_name)
        try:
            # Verify correct org context
            context_indicator = await self.page.query_selector(".org-context, .context-selector")
            if context_indicator:
                org_text = (await context_indicator.inner_text()).lower()
                if claim.program.value.lower() not in org_text:
                    await self.safe_click("a:has-text('Change Context'), button:has-text('Change Context')")
                    await asyncio.sleep(0.5)
                    await self.safe_click(f"a:has-text('{claim.program.value}')")
                    await asyncio.sleep(1)

            await self.safe_click("a:has-text('Cases'), nav a[href*='case']")
            await asyncio.sleep(1)

            # Case Type = UM
            await self.page.select_option("select[name*='type']", label="UM")

            # Date range: few days before/after auth send date
            from datetime import timedelta
            start = (claim.dos - timedelta(days=10)).strftime("%m/%d/%Y")
            end = (claim.dos + timedelta(days=10)).strftime("%m/%d/%Y")
            await self.safe_fill("input[name*='start']", start)
            await self.safe_fill("input[name*='end']", end)

            # Add member search if possible
            if claim.client_id:
                member_field = await self.page.query_selector("input[name*='member']")
                if member_field:
                    await member_field.fill(claim.client_id)

            await self.safe_click("button:has-text('Search')")
            await asyncio.sleep(2)

            # Find matching case and View procedures
            import re
            rows = await self.page.query_selector_all("tr.case-row, tbody tr")
            for row in rows:
                text = await row.inner_text()
                if claim.client_name.split()[-1].lower() in text.lower():
                    # Click request number to view procedures + download letters
                    req_link = await row.query_selector("a.request-number, td:first-child a")
                    if req_link:
                        await req_link.click()
                        await asyncio.sleep(1)
                        # Look for View Procedures
                        view_proc = await self.page.query_selector("a:has-text('View procedures'), button:has-text('procedures')")
                        if view_proc:
                            await view_proc.click()
                            await asyncio.sleep(1)

                    status_text = text.lower()
                    status = "approved" if "approved" in status_text else (
                        "denied" if "denied" in status_text else "submitted"
                    )
                    auth_match = re.search(r"\b[A-Z0-9]{8,}\b", text)
                    if auth_match:
                        return True, AuthRecord(
                            client_id=claim.client_id,
                            client_name=claim.client_name,
                            mco=MCO.DMAS,
                            program=claim.program,
                            auth_number=auth_match.group(0),
                            proc_code="",
                            start_date=claim.dos,
                            end_date=claim.dos,
                            status=status,
                            source="portal",
                        )

            return False, None

        except Exception as e:
            self.logger.error("Kepro auth check failed", error=str(e))
            return False, None


# ---------------------------------------------------------------------------
# Auth checker factory
# ---------------------------------------------------------------------------

def get_auth_checker(mco: MCO, headless: bool = True) -> Optional[MCOPortalBase]:
    """Return the right portal class for a given MCO."""
    mapping = {
        MCO.SENTARA: SentaraPortal,
        MCO.UNITED:  UnitedPortal,
        MCO.MOLINA:  AvailityPortal,
        MCO.ANTHEM:  AvailityPortal,
        MCO.AETNA:   AvailityPortal,
        MCO.DMAS:    KoproPortal,
    }
    cls = mapping.get(mco)
    return cls(headless=headless) if cls else None
