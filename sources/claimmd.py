"""
sources/claimmd.py
------------------
Full Claim.MD automation:
  - Login / session management
  - Pull denied/rejected claim list
  - Parse denial codes
  - Step 1: Correct & retransmit
  - Step 2: Generate reconsideration (all MCO forms)
  - Step 3: Appeal submission
  - Write claim notes (without Save — keeps off transmit queue)
  - Download ERA files
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

from playwright.async_api import TimeoutError as PWTimeout

from config.models import Claim, ClaimStatus, DenialCode, MCO, ResolutionAction
from config.settings import DRY_RUN, get_credentials
from notes.formatter import (
    format_note,
    note_correction,
    note_reconsideration_submitted,
    note_appeal_submitted,
    note_write_off,
    note_human_review_needed,
    get_recon_reason,
    AUTOMATION_INITIALS,
)
from sources.browser_base import BrowserSession
from logging_utils.logger import get_logger


# ---------------------------------------------------------------------------
# Denial code parser — maps Claim.MD red text to DenialCode enum
# ---------------------------------------------------------------------------

_DENIAL_PATTERNS: List[Tuple[re.Pattern, DenialCode]] = [
    # Auth / precert denials
    (re.compile(r"no auth|authorization.*not.*found|auth.*on file|precertification|submitted authorization.*missing|not deemed.*medical necessity", re.I), DenialCode.NO_AUTH),
    (re.compile(r"auth.*expir|expired.*auth|exceeded.*precert", re.I), DenialCode.AUTH_EXPIRED),
    (re.compile(r"exceed.*unit|exceeded.*authorization", re.I), DenialCode.EXCEEDED_UNITS),
    # Coverage / enrollment
    (re.compile(r"coverage.*terminat|not.*enrolled.*managed care|expenses.*incurred.*after.*coverage", re.I), DenialCode.COVERAGE_TERMINATED),
    (re.compile(r"not.*enrolled|not.*eligible|benefit.*not.*covered", re.I), DenialCode.NOT_ENROLLED),
    # Data errors
    (re.compile(r"duplicate|dup claim|duplicate.*claim.*submitted|included in.*payment.*another", re.I), DenialCode.DUPLICATE),
    (re.compile(r"invalid.*id|member.*id.*invalid|id.*not.*found|invalid member|member id|id.*does not exist", re.I), DenialCode.INVALID_ID),
    (re.compile(r"invalid.*dob|date.*of.*birth|dob.*not.*match", re.I), DenialCode.INVALID_DOB),
    (re.compile(r"invalid.*npi|npi.*not.*valid|provider.*not.*found", re.I), DenialCode.INVALID_NPI),
    (re.compile(r"invalid.*diag|diagnosis.*code|diagnosis.*pointer.*blank", re.I), DenialCode.INVALID_DIAG),
    (re.compile(r"diagnosis.*pointer.*blank|references blank diagnosis", re.I), DenialCode.DIAGNOSIS_BLANK),
    (re.compile(r"billing.*company|wrong.*provider|provider.*mismatch", re.I), DenialCode.WRONG_BILLING_CO),
    # Provider / procedure issues
    (re.compile(r"provider.*not.*certified|not.*eligible.*paid|not.*certified", re.I), DenialCode.PROVIDER_NOT_CERTIFIED),
    (re.compile(r"not otherwise classified|unlisted.*procedure|missing.*procedure.*code|invalid.*procedure", re.I), DenialCode.UNLISTED_PROCEDURE),
    (re.compile(r"national provider.*missing|missing.*rendering.*provider|invalid.*rendering.*provider", re.I), DenialCode.MISSING_NPI_RENDERING),
    # Financial
    (re.compile(r"timely.*filing|filing.*limit|past.*timely", re.I), DenialCode.TIMELY_FILING),
    (re.compile(r"rural.*rate|rrr|rate.*reduction", re.I), DenialCode.RURAL_RATE_REDUCTION),
    (re.compile(r"recoup", re.I), DenialCode.RECOUPMENT),
    (re.compile(r"underpay|under.*pay|partial.*pay", re.I), DenialCode.UNDERPAID),
    # Escalated
    (re.compile(r"recon.*denied|reconsidered.*denied", re.I), DenialCode.RECON_DENIED),
]


def parse_denial_codes(raw_text: str) -> List[DenialCode]:
    codes = []
    for pattern, code in _DENIAL_PATTERNS:
        if pattern.search(raw_text):
            codes.append(code)
    return codes if codes else [DenialCode.UNKNOWN]


# ---------------------------------------------------------------------------
# Claim.MD form selectors (may need updating if UI changes)
# ---------------------------------------------------------------------------

# Selectors use CSS only (no text= syntax — that's for page.click locators)
# For click actions, we use page.click() with locator syntax separately
SELECTORS = {
    "username_field":     "input[name='userlogin'], input[id*='user'], input[type='text']",
    "password_field":     "input[name='password'], input[type='password']",
    "login_button":       "input[type='submit'], button[type='submit']",
    "logged_in_marker":   "a[href*='logout'], .logout-link, #logout",
    "manage_claims_link": "a[href*='manage']",
    "denied_tab":         "a[href*='rejected'], #rejected_tab",
    "claim_rows":         "tr.claim-row, .claim-item, tr[data-claim-id]",
    "denial_code_text":   ".denial-code, .rejection-code, span.red, td.denial",
    "claim_id_cell":      "td.claim-id, .claim-number",
    "client_name_cell":   "td.patient-name, .member-name",
    "dos_cell":           "td.dos, td.date-of-service",
    "amount_cell":        "td.amount, td.billed-amount",
    "notes_field":        "textarea#notes, textarea[name='notes'], #claim_notes",
    "save_button":        "input[value='Save']",
    "approve_transmit":   "input[value*='Approve Transmit']",
    "other_actions":      "a[href*='actions'], button[class*='action']",
    "manage_appeals":     "a[href*='appeal']",
    "appeal_form_select": "select#appeal_form, select[name*='appeal']",
    "include_cms1500":    "input[id*='cms1500'], input[value*='CMS']",
    "include_era":        "input[id*='era'], input[value*='ERA']",
    "include_history":    "input[id*='history'], input[value*='History']",
    "upload_files":       "input[type='file']",
    "generate_appeal":    "input[value*='Generate']",
    "appeal_reason_text": "textarea#reason, textarea[name*='reason']",
    "sign_submit":        "input[value*='Submit']",
    "show_history":       "a[href*='history']",
}

# Claim.MD uses divs with onclick="loadmain('/url')" for navigation
# These are the actual navigation URLs loaded into the mainframe iframe
CLAIMMD_PAGES = {
    "summary":       "/overview.plx",
    "upload_files":  "/inbound.plx",
    "manage_claims": "/monitor.plx",
    "rejected":      "/monitor.plx?l=rejected",
    "view_era":      "/era.plx",
    "reporting":     "/report.plx",
    "eligibility":   "/elig.plx",
    "search":        "/monitor.plx?search=1",
}


# ---------------------------------------------------------------------------
# MCO form names in Claim.MD appeal dropdown
# ---------------------------------------------------------------------------

MCO_APPEAL_FORM_NAMES = {
    MCO.SENTARA:  "Sentara Provider Reconsideration Form",
    MCO.AETNA:    "Aetna Better Health of Virginia Provider Dispute and Resubmission Form",
    MCO.ANTHEM:   "Anthem",
    MCO.HUMANA:   "Humana",
    MCO.UNITED:   None,   # United uses their own portal — handled separately
    MCO.MOLINA:   "Molina",
}

# Required document checkboxes for all MCOs
# March 2026 update: ALL reconsiderations need these docs (not just Aetna)
REQUIRED_DOCS = ["cms1500", "era", "history"]

# ALL reconsiderations should include these supporting documents.
# Assessment and ISP removed — only authorization, progress note (lauris_note),
# and DMAS regulations are required.
RECON_SUPPORTING_DOCS = ["authorization", "lauris_note", "dmas_regulations"]

# Legacy alias for backwards compatibility
AETNA_EXTRA_DOCS = RECON_SUPPORTING_DOCS


# ---------------------------------------------------------------------------
# Main Claim.MD session class
# ---------------------------------------------------------------------------

class ClaimMDSession(BrowserSession):
    SESSION_NAME = "claimmd"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().claimmd

    @property
    def login_url(self) -> str:
        return self._creds.url if self._creds else "https://www.claim.md/"

    async def _is_logged_in(self) -> bool:
        try:
            await self.page.goto(
                "https://www.claim.md/login.plx?base=1",
                wait_until="domcontentloaded",
                timeout=15000,
            )
            await asyncio.sleep(3)

            url = self.page.url.lower()

            # If URL no longer has 'login' in it, we got redirected to dashboard
            if "login" not in url:
                self.logger.info("Session valid — redirected away from login", url=url)
                return True

            # Check for dashboard elements using individual CSS selectors
            for sel in ["a[href*='logout']", ".logout", "a[href*='manage']",
                        ".dashboard", "#main-content"]:
                el = await self.page.query_selector(sel)
                if el:
                    self.logger.info("Session valid — found dashboard element", selector=sel)
                    return True

            # Check page content for logged-in indicators
            content = await self.page.content()
            if "Manage Claims" in content or "logout" in content.lower():
                self.logger.info("Session valid — found logged-in text in page")
                return True

            self.logger.info("Session expired — still on login page", url=url)
            return False
        except Exception as e:
            self.logger.warning("Login check failed", error=str(e))
            return False

    async def _perform_login(self) -> bool:
        if not self._creds or not self._creds.username:
            raise RuntimeError("Claim.MD credentials not configured")

        await self.page.goto(self.login_url, wait_until="domcontentloaded")
        await self.safe_fill(SELECTORS["username_field"], self._creds.username)
        await self.safe_fill(SELECTORS["password_field"], self._creds.password)
        await self.safe_click(SELECTORS["login_button"])
        await asyncio.sleep(2)

        if self._creds.mfa_type != "none":
            await self.handle_mfa(self._creds.mfa_type)

        return await self._is_logged_in()

    # ------------------------------------------------------------------
    # Mainframe navigation helpers
    # ------------------------------------------------------------------

    def _get_mainframe(self):
        """Get the mainframe iframe where all Claim.MD content lives."""
        for frame in self.page.frames:
            if "mainframe" in frame.name or "monitor.plx" in frame.url or "era.plx" in frame.url:
                return frame
        # Fallback to page itself
        return self.page

    async def _navigate_mainframe(self, path: str):
        """Navigate the mainframe iframe to a new page via JavaScript."""
        url = f"https://www.claim.md{path}"
        await self.page.evaluate(
            f"document.getElementById('mainframe').src = '{url}'"
        )
        await asyncio.sleep(2)

    # ------------------------------------------------------------------
    # Claim list retrieval
    # ------------------------------------------------------------------

    async def get_denied_claims(self) -> List[Claim]:
        """Navigate to Manage Claims → Rejected Claims and parse all rows."""
        self.logger.info("Fetching denied/rejected claims from Claim.MD")
        claims = []

        if DRY_RUN:
            self.logger.info("DRY_RUN: Skipping live claim fetch — returning empty list")
            return claims

        try:
            # Navigate directly to the rejected claims page
            await self._navigate_mainframe(CLAIMMD_PAGES["rejected"])
            await asyncio.sleep(3)
            await asyncio.sleep(2)
        except PWTimeout:
            await self.screenshot("navigate_to_denied")
            self.logger.error("Could not navigate to denied claims tab")
            return []

        # Paginate through all pages
        page_num = 1
        while True:
            page_claims = await self._parse_claim_rows()
            claims.extend(page_claims)
            self.logger.info(f"Page {page_num}: found {len(page_claims)} claims")

            # Check for next page (inside mainframe)
            next_btn = await frame.query_selector("a[href*='next'], .next-page, .pagination a:last-child")
            if not next_btn:
                break
            await next_btn.click()
            await asyncio.sleep(1)
            page_num += 1

        self.logger.info(f"Total denied claims retrieved: {len(claims)}")
        return claims

    async def _parse_claim_rows(self) -> List[Claim]:
        """Parse all claim rows on the current page (inside mainframe)."""
        frame = self._get_mainframe()
        rows = await frame.query_selector_all(SELECTORS["claim_rows"])
        claims = []
        for row in rows:
            try:
                claim = await self._parse_single_row(row)
                if claim:
                    claims.append(claim)
            except Exception as e:
                self.logger.warning("Failed to parse claim row", error=str(e))
        return claims

    async def _parse_single_row(self, row) -> Optional[Claim]:
        """Extract claim data from a table row."""
        try:
            claim_id   = await self._cell_text(row, "td:nth-child(1)")
            client     = await self._cell_text(row, "td:nth-child(2)")
            dos_str    = await self._cell_text(row, "td:nth-child(3)")
            mco_str    = await self._cell_text(row, "td:nth-child(4)")
            amount_str = await self._cell_text(row, "td:nth-child(5)")
            denial_raw = await self._cell_text(row, ".denial-code, td.red, td:nth-child(6)")

            dos = _parse_date(dos_str)
            billed = float(re.sub(r"[^\d.]", "", amount_str) or "0")
            mco = _parse_mco(mco_str)
            denial_codes = parse_denial_codes(denial_raw)
            status = (
                ClaimStatus.REJECTED if denial_codes[0] in {
                    DenialCode.INVALID_ID, DenialCode.INVALID_DOB,
                    DenialCode.INVALID_NPI, DenialCode.INVALID_DIAG
                }
                else ClaimStatus.DENIED
            )
            age_days = (date.today() - dos).days if dos else 0

            # Try to get URL
            link = await row.query_selector("a[href*='claim']")
            claim_url = await link.get_attribute("href") if link else ""

            return Claim(
                claim_id=claim_id,
                client_name=client,
                client_id="",          # Populated when claim is opened
                dos=dos or date.today(),
                mco=mco,
                program=_infer_program(client, mco_str),
                billed_amount=billed,
                status=status,
                denial_codes=denial_codes,
                denial_reason_raw=denial_raw,
                date_denied=date.today(),
                age_days=age_days,
                claimmd_url=claim_url,
            )
        except Exception as e:
            self.logger.warning("Row parse error", error=str(e))
            return None

    async def _cell_text(self, row, selector: str) -> str:
        try:
            el = await row.query_selector(selector)
            return (await el.inner_text()).strip() if el else ""
        except Exception:
            return ""

    # ------------------------------------------------------------------
    # Step 1: Claim Correction
    # ------------------------------------------------------------------

    async def correct_and_resubmit(self, claim: Claim, corrections: Dict[str, str]) -> bool:
        """
        Open claim, fix specified fields, save, and approve transmit.
        corrections: {field_name: new_value}
          field_name options: "member_id", "dob", "npi", "diag", "billing_region", "auth_number"
        """
        self.logger.info("Correcting claim", claim_id=claim.claim_id, corrections=list(corrections.keys()))

        if DRY_RUN:
            self.logger.info("DRY_RUN: Would correct claim", claim_id=claim.claim_id)
            return True

        if not await self._open_claim(claim):
            return False

        field_selectors = {
            "member_id":      "input[name*='member_id'], input[id*='member'], input[name*='insured_id']",
            "dob":            "input[name*='dob'], input[id*='dob'], input[name*='birth']",
            "npi":            "input[name*='npi'], input[id*='npi']",
            "diag":           "input[name*='diag'], input[id*='diagnosis']",
            "billing_region": "select[name*='region'], select[id*='region'], input[name*='billing_region']",
            "auth_number":    "input[name*='auth'], input[id*='auth'], input[name*='prior_auth']",
            "rendering_npi":  "input[name*='rendering'], input[id*='rendering_npi'], input[name*='render_npi']",
        }

        for field, value in corrections.items():
            sel = field_selectors.get(field)
            if sel:
                try:
                    await self.safe_fill(sel, value)
                    self.logger.info("Field corrected", field=field, value=value)
                except Exception as e:
                    self.logger.warning("Could not fill field", field=field, error=str(e))

        # Save
        try:
            await self.safe_click(SELECTORS["save_button"])
            await asyncio.sleep(1)
            await self.safe_click(SELECTORS["approve_transmit"])
            await asyncio.sleep(1)
        except PWTimeout:
            await self.screenshot(f"save_failed_{claim.claim_id}")
            return False

        # Write note (do NOT save — keeps off transmit queue)
        correction_desc = ", ".join(f"{k}→{v}" for k, v in corrections.items())
        note = note_correction(correction_desc)
        await self._write_note_no_save(note)

        self.logger.info("Claim corrected and retransmitted", claim_id=claim.claim_id)
        return True

    # ------------------------------------------------------------------
    # Step 2: Reconsideration
    # ------------------------------------------------------------------

    async def submit_reconsideration(
        self,
        claim: Claim,
        auth_pdf_path: Optional[str] = None,
        extra_docs: Optional[List[str]] = None,
    ) -> bool:
        """
        Submit a reconsideration via Claim.MD.
        auth_pdf_path: local path to the MCO authorization PDF
        extra_docs: list of additional PDF paths (required for Aetna)
        """
        # United uses their own portal
        if claim.mco == MCO.UNITED:
            self.logger.info("United recon goes through UHC portal — routing there", claim_id=claim.claim_id)
            return False  # Caller handles via united.py

        form_name = MCO_APPEAL_FORM_NAMES.get(claim.mco)
        if not form_name:
            self.logger.warning("No Claim.MD form for MCO", mco=claim.mco.value)
            return False

        self.logger.info("Submitting reconsideration", claim_id=claim.claim_id, mco=claim.mco.value)

        if DRY_RUN:
            self.logger.info("DRY_RUN: Would submit reconsideration", claim_id=claim.claim_id)
            return True

        if not await self._open_claim(claim):
            return False

        # Open Other Actions → Manage Appeals
        try:
            frame = self._get_mainframe()
            await frame.click("a:has-text('Other Actions'), button:has-text('Other Actions')", timeout=10000)
            await asyncio.sleep(0.5)
            await frame.click("a:has-text('Manage Appeals'), a[href*='appeal']", timeout=10000)
            await asyncio.sleep(1)
        except PWTimeout:
            await self.screenshot(f"manage_appeals_failed_{claim.claim_id}")
            return False

        # Select MCO form from dropdown
        try:
            await self.page.select_option(SELECTORS["appeal_form_select"], label=form_name)
            await asyncio.sleep(0.5)
        except Exception as e:
            self.logger.warning("Could not select appeal form", form=form_name, error=str(e))

        # Check required document boxes: CMS-1500, ERA, Claim History
        for doc in REQUIRED_DOCS:
            sel = SELECTORS.get(f"include_{doc}")
            if sel:
                try:
                    cb = await self.page.query_selector(sel)
                    if cb and not await cb.is_checked():
                        await cb.check()
                except Exception:
                    pass

        # Upload auth PDF
        if auth_pdf_path:
            try:
                await self.page.set_input_files(SELECTORS["upload_files"], auth_pdf_path)
                await asyncio.sleep(1)
            except Exception as e:
                self.logger.warning("Auth PDF upload failed", error=str(e), path=auth_pdf_path)

        # Upload supporting docs — ALL reconsiderations need these (March 2026 update)
        # All MCOs require: authorization, progress note (lauris_note),
        # and DMAS regulations (assessment and ISP removed).
        if extra_docs:
            for doc_path in extra_docs:
                try:
                    await self.page.set_input_files(SELECTORS["upload_files"], doc_path)
                    await asyncio.sleep(0.5)
                except Exception as e:
                    self.logger.warning("Extra doc upload failed", error=str(e), path=doc_path)

        # Generate Appeal (opens the form)
        try:
            frame = self._get_mainframe()
            await frame.click("button:has-text('Generate'), input[value*='Generate']", timeout=10000)
            await asyncio.sleep(2)
        except PWTimeout:
            await self.screenshot(f"generate_appeal_failed_{claim.claim_id}")
            return False

        # Fill reconsideration reason
        reason_text = get_recon_reason(
            claim.denial_codes[0].value if claim.denial_codes else "no_auth",
            claim.mco.value,
        )
        try:
            await self.safe_fill(SELECTORS["appeal_reason_text"], reason_text)
        except Exception as e:
            self.logger.warning("Could not fill reason text", error=str(e))

        # Sign and submit
        try:
            frame = self._get_mainframe()
            await frame.click("button:has-text('Submit'), input[value*='Submit']", timeout=10000)
            await asyncio.sleep(2)
        except PWTimeout:
            await self.screenshot(f"submit_failed_{claim.claim_id}")
            return False

        # Write note (NO SAVE — critical to not move to transmit queue)
        note = note_reconsideration_submitted(claim.mco.value)
        await self._write_note_no_save(note)

        self.logger.info("Reconsideration submitted", claim_id=claim.claim_id, mco=claim.mco.value)
        return True

    # ------------------------------------------------------------------
    # Step 3: Appeal
    # ------------------------------------------------------------------

    async def submit_appeal(self, claim: Claim, auth_pdf_path: Optional[str] = None) -> bool:
        """Submit a formal appeal (used when reconsideration was denied or timed out)."""
        self.logger.info("Submitting appeal", claim_id=claim.claim_id)

        if DRY_RUN:
            self.logger.info("DRY_RUN: Would submit appeal", claim_id=claim.claim_id)
            return True

        # Appeals follow same Claim.MD path as reconsiderations but escalated
        # Detailed appeal form navigation depends on MCO — flag Magellan and DMAS for human
        if claim.mco in {MCO.MAGELLAN, MCO.DMAS}:
            note = note_human_review_needed(
                f"Appeal for {claim.mco.value} requires human — DMAS/Magellan manual process"
            )
            await self._open_claim(claim)
            await self._write_note_no_save(note)
            return False

        success = await self.submit_reconsideration(claim, auth_pdf_path)
        if success:
            note = note_appeal_submitted(claim.mco.value)
            # Note was already written by submit_reconsideration, update it
        return success

    # ------------------------------------------------------------------
    # Write-off note in Claim.MD
    # ------------------------------------------------------------------

    async def write_claimmd_writeoff_note(self, claim: Claim, reason: str, extra: str = "") -> bool:
        """Add a write-off documentation note to the claim."""
        if not await self._open_claim(claim):
            return False
        note = note_write_off(reason, extra)
        await self._write_note_no_save(note)
        return True

    # ------------------------------------------------------------------
    # ERA download
    # ------------------------------------------------------------------

    async def download_eras(self, download_dir: str) -> List[str]:
        """
        Download all available ERA (835) files from Claim.MD.
        Returns list of downloaded file paths.
        """
        from pathlib import Path
        dl_dir = Path(download_dir)
        dl_dir.mkdir(parents=True, exist_ok=True)

        downloaded = []
        self.logger.info("Downloading ERAs from Claim.MD")

        if DRY_RUN:
            self.logger.info("DRY_RUN: Skipping live ERA download — returning empty list")
            return downloaded

        try:
            # Navigate mainframe to ERA page
            await self._navigate_mainframe(CLAIMMD_PAGES["view_era"])
            await asyncio.sleep(1)

            frame = self._get_mainframe()
            era_rows = await frame.query_selector_all(".era-row, tr.era, tr[data-era-id], tbody tr")
            for row in era_rows:
                try:
                    # Check if already downloaded/uploaded
                    status = await self._cell_text(row, ".era-status, td.status")
                    if "uploaded" in status.lower() or "processed" in status.lower():
                        continue

                    # Click download
                    dl_btn = await row.query_selector("a:has-text('Download'), a[href*='download']")
                    if dl_btn:
                        async with self.page.expect_download() as dl_info:
                            await dl_btn.click()
                        dl = await dl_info.value
                        dest = dl_dir / dl.suggested_filename
                        await dl.save_as(str(dest))
                        downloaded.append(str(dest))
                        self.logger.info("ERA downloaded", file=dl.suggested_filename)
                except Exception as e:
                    self.logger.warning("ERA download error", error=str(e))
        except Exception as e:
            self.logger.error("ERA section navigation failed", error=str(e))

        self.logger.info(f"Downloaded {len(downloaded)} ERA files")
        return downloaded

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _open_claim(self, claim: Claim) -> bool:
        """Navigate to a specific claim's detail page."""
        if claim.claimmd_url:
            url = claim.claimmd_url
            if not url.startswith("http"):
                url = f"https://www.claim.md/{url}"
            await self.page.goto(url, wait_until="domcontentloaded")
            await asyncio.sleep(1)
            return True

        # Search by claim ID
        try:
            search = await self.page.query_selector("input[name*='search'], input[placeholder*='claim']")
            if search:
                await search.fill(claim.claim_id)
                await self.page.keyboard.press("Enter")
                await asyncio.sleep(1)
                row = await self.page.query_selector(f"tr:has-text('{claim.claim_id}')")
                if row:
                    link = await row.query_selector("a")
                    if link:
                        await link.click()
                        await asyncio.sleep(1)
                        return True
        except Exception as e:
            self.logger.warning("Could not open claim", claim_id=claim.claim_id, error=str(e))
        return False

    async def _write_note_no_save(self, note_text: str):
        """
        Write a note to the claim notes field.
        CRITICAL: Do NOT click Save — this keeps the claim off the 'ready to transmit' queue.
        """
        try:
            notes_field = await self.page.query_selector(SELECTORS["notes_field"])
            if notes_field:
                # Append to existing note
                existing = await notes_field.input_value()
                new_text = f"{existing}\n{note_text}".strip() if existing else note_text
                await notes_field.fill(new_text)
                self.logger.info("Note written (no save)", note_preview=note_text[:60])
            else:
                self.logger.warning("Notes field not found on claim page")
        except Exception as e:
            self.logger.error("Failed to write claim note", error=str(e))

    async def write_and_save_note(self, claim: "Claim", note_text: str) -> bool:
        """Write a note and click 'Add Note / Reminder' to save it.

        This saves the note WITHOUT resubmitting the claim — uses the
        dedicated 'Add Note / Reminder' button, not the main Save.
        """
        if not await self._open_claim(claim):
            self.logger.error("Could not open claim for note", claim_id=claim.claim_id)
            return False

        try:
            # Write the note text
            notes_field = await self.page.query_selector(SELECTORS["notes_field"])
            if not notes_field:
                self.logger.warning("Notes field not found")
                return False

            existing = await notes_field.input_value()
            new_text = f"{existing}\n{note_text}".strip() if existing else note_text
            await notes_field.fill(new_text)

            # Click "Add Note / Reminder" button
            add_note_btn = await self.page.query_selector(
                "input[value*='Add Note'], button:has-text('Add Note'), "
                "a:has-text('Add Note'), input[value*='add note']"
            )
            if add_note_btn:
                await add_note_btn.click()
                await asyncio.sleep(2)
                self.logger.info(
                    "Note saved via Add Note/Reminder",
                    claim_id=claim.claim_id,
                    note_preview=note_text[:60],
                )
                return True
            else:
                self.logger.warning(
                    "Add Note/Reminder button not found",
                    claim_id=claim.claim_id,
                )
                return False
        except Exception as e:
            self.logger.error(
                "Failed to save note",
                claim_id=claim.claim_id,
                error=str(e),
            )
            return False


async def post_claim_note(
    claim_id: str, note_text: str, pcn: str = "",
) -> bool:
    """Write and save a note on a Claim.MD claim via browser.

    Uses persistent Chrome profile with anti-automation detection to
    get the full Claim.MD interface (Search, notes, etc.).

    Flow: Search by PCN → open claim → write note → click Add Note/Reminder.
    """
    from pathlib import Path
    from datetime import date as _date
    from playwright.async_api import async_playwright

    _log = get_logger("claimmd_note")

    if not pcn:
        _log.warning("PCN required for note posting", claim_id=claim_id)
        return False

    user_data = Path("/tmp/claimmd_chrome_profile")
    user_data.mkdir(exist_ok=True)
    session_dir = Path("sessions")
    session_dir.mkdir(exist_ok=True)
    session_file = session_dir / f"claimmd_{_date.today().isoformat()}.json"

    pw = None
    context = None
    try:
        pw = await async_playwright().start()

        # Use persistent context with anti-detection
        context = await pw.chromium.launch_persistent_context(
            str(user_data),
            headless=True,
            viewport={"width": 1920, "height": 1080},
            args=[
                "--disable-blink-features=AutomationControlled",
            ],
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            ignore_default_args=["--enable-automation"],
        )

        page = context.pages[0] if context.pages else await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        # Navigate to Claim.MD
        await page.goto(
            "https://www.claim.md/login", wait_until="domcontentloaded", timeout=20000,
        )
        await asyncio.sleep(3)

        # Check if already logged in (persistent profile may have session)
        logged_in = False
        search_link = await page.query_selector("a:has-text('Search')")
        logout_link = await page.query_selector("a:has-text('LOGOUT')")
        if search_link or logout_link:
            logged_in = True
            _log.info("Claim.MD session active from persistent profile")

        if not logged_in:
            # Try auto-login (may fail on CAPTCHA)
            try:
                await page.fill("input[name='userlogin']", os.getenv("CLAIMMD_USERNAME", ""))
                await page.fill("input[name='password']", os.getenv("CLAIMMD_PASSWORD", ""))
                await page.click("input[type='submit']")
                await asyncio.sleep(5)
                search_link = await page.query_selector("a:has-text('Search')")
                if search_link:
                    logged_in = True
            except Exception:
                pass

        if not logged_in:
            _log.warning("Claim.MD login failed — CAPTCHA may be required")
            await context.close()
            await pw.stop()
            return False

        # Click "Search" in sidebar
        search_link = await page.query_selector("a:has-text('Search')")
        if not search_link:
            _log.warning("Search link not found in sidebar")
            await context.close()
            await pw.stop()
            return False

        await search_link.click()
        await asyncio.sleep(2)

        # Fill "Acct # / PCN" field (first input in Search Claims dialog)
        # The dialog has fields in order: Acct#/PCN, Policy#, Patient Last Name, etc.
        pcn_input = await page.query_selector(
            "input[name*='pcn'], input[name*='acct'], "
            "input[name*='account'], input[name*='search_pcn']"
        )
        if not pcn_input:
            # Fallback: first visible input in the search dialog
            dialog_inputs = await page.query_selector_all(
                ".search-dialog input[type='text'], "
                "#searchDialog input[type='text'], "
                "div:has-text('Primary Search') input[type='text']"
            )
            if dialog_inputs:
                pcn_input = dialog_inputs[0]

        if not pcn_input:
            # Last resort: find by position — first text input after "Acct # / PCN" text
            all_inputs = await page.query_selector_all("input[type='text']")
            for inp in all_inputs:
                if await inp.is_visible():
                    pcn_input = inp
                    break

        if not pcn_input:
            _log.warning("PCN search field not found")
            await page.screenshot(path="logs/screenshots/claimmd_no_pcn_field.png")
            await context.close()
            await pw.stop()
            return False

        await pcn_input.fill(pcn)
        await asyncio.sleep(0.5)

        # Click Search button in dialog
        search_btn = await page.query_selector(
            "input[value='Search'], button:has-text('Search')"
        )
        if search_btn:
            await search_btn.click()
            await asyncio.sleep(3)

        # Click on claim row in results
        claim_row = await page.query_selector(f"td:has-text('{pcn}')")
        if claim_row:
            await claim_row.click()
            await asyncio.sleep(3)
        else:
            # Try clicking first row link
            first_link = await page.query_selector("table a, tr a")
            if first_link:
                await first_link.click()
                await asyncio.sleep(3)

        # Now on claim detail — find notes textarea
        notes_field = await page.query_selector(
            "textarea#notes, textarea[name='notes'], "
            "textarea[name*='note'], textarea"
        )
        if not notes_field or not await notes_field.is_visible():
            _log.warning("Notes textarea not found/visible on claim page")
            await page.screenshot(path="logs/screenshots/claimmd_no_notes.png")
            await context.close()
            await pw.stop()
            return False

        # Write note
        existing = await notes_field.input_value()
        new_text = f"{existing}\n{note_text}".strip() if existing else note_text
        await notes_field.fill(new_text)

        # Click "Add Note / Reminder"
        add_btn = await page.query_selector(
            "input[value*='Add Note'], input[value*='Reminder'], "
            "button:has-text('Add Note'), a:has-text('Add Note')"
        )
        if add_btn:
            await add_btn.click()
            await asyncio.sleep(3)
            _log.info("Note saved via browser", claim_id=claim_id, pcn=pcn)
            await context.close()
            await pw.stop()
            return True
        else:
            _log.warning("Add Note/Reminder button not found")
            await page.screenshot(path="logs/screenshots/claimmd_no_add_note_btn.png")
            await context.close()
            await pw.stop()
            return False

    except Exception as exc:
        _log.warning("post_claim_note failed", claim_id=claim_id, error=str(exc)[:150])
        if context:
            try:
                await context.close()
            except Exception:
                pass
        if pw:
            try:
                await pw.stop()
            except Exception:
                pass
        return False


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> Optional[date]:
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


def _parse_mco(mco_str: str) -> MCO:
    s = mco_str.upper()
    if "UNITED" in s or "UHC" in s:
        return MCO.UNITED
    if "SENTARA" in s:
        return MCO.SENTARA
    if "AETNA" in s:
        return MCO.AETNA
    if "ANTHEM" in s:
        return MCO.ANTHEM
    if "MOLINA" in s:
        return MCO.MOLINA
    if "HUMANA" in s:
        return MCO.HUMANA
    if "MAGELLAN" in s:
        return MCO.MAGELLAN
    if "MEDICAID" in s or "DMAS" in s:
        return MCO.DMAS
    return MCO.UNKNOWN


def _infer_program(client_name: str, mco_str: str) -> "Program":
    from config.models import Program
    # Program is typically determined by billing region / MCO approval
    # This is a heuristic; Lauris is the source of truth
    return Program.UNKNOWN
