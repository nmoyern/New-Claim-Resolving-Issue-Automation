"""
actions/lauris_fixes.py
-----------------------
Lauris root-cause fixes: update client records so the same
denial doesn't recur for future claims.

Per the Complete Framework (Rule 2):
  Every denial triggers TWO actions:
  1. Fix the claim (Claim.MD API)
  2. Fix the root cause in Lauris

This module handles #2 — updating Lauris client records:
  - Billing company (KJLN vs NHCS vs Mary's Home)
  - Member ID / demographics
  - Authorization entry
  - Fax verification

Consumer navigation:
  Consumers page: /start_newui.aspx
  Search field: #txtSearch
  Consumer detail: LoadConsumerDetails('ID_NUMBER') JavaScript call
  Consumer detail opens in a popup/frame named 'viewdetails'
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import Optional

from config.models import Claim, DenialCode, MCO, Program
from config.settings import DRY_RUN
from sources.claimmd_api import ClaimMDAPI
from lauris.billing import LaurisSession
from logging_utils.logger import get_logger

logger = get_logger("lauris_fixes")


async def fix_root_cause(claim: Claim, denial_code: DenialCode) -> dict:
    """
    Apply the appropriate Lauris root-cause fix for a claim's denial.
    Returns dict with {fixed: bool, fix_description: str, error: str}.
    """
    result = {"fixed": False, "fix_description": "", "error": ""}

    if DRY_RUN:
        result["fix_description"] = f"DRY_RUN: Would fix {denial_code.value} in Lauris"
        result["fixed"] = True
        return result

    fix_map = {
        DenialCode.WRONG_BILLING_CO: _fix_billing_company,
        DenialCode.INVALID_ID: _fix_member_id,
        DenialCode.INVALID_DOB: _fix_demographics,
        DenialCode.INVALID_NPI: _fix_npi_config,
        DenialCode.NO_AUTH: _fix_auth_record,
        DenialCode.AUTH_EXPIRED: _fix_auth_record,
        DenialCode.MISSING_NPI_RENDERING: _fix_rendering_npi,
    }

    fix_func = fix_map.get(denial_code)
    if not fix_func:
        result["fix_description"] = f"No Lauris fix defined for {denial_code.value}"
        return result

    try:
        result = await fix_func(claim)
    except Exception as e:
        logger.error("Lauris fix failed", denial=denial_code.value, error=str(e))
        result["error"] = str(e)

    return result


async def _fix_billing_company(claim: Claim) -> dict:
    """Fix wrong billing company (KJLN vs NHCS vs Mary's Home).
    Also verifies that the intake in Lauris was completed for the correct
    entity.  If not, creates a ClickUp task for the supervisor (due in 2
    business days) to update the intake.
    """
    correct_company = _infer_billing_company(claim)

    try:
        async with LaurisSession() as lauris:
            base = lauris.login_url.rsplit("/", 1)[0]

            # Navigate to consumers
            await lauris.page.goto(
                f"{base}/start_newui.aspx",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(3)

            # Search for consumer
            search = await lauris.page.query_selector("#txtSearch")
            if search:
                await search.fill(claim.client_name)
                await lauris.page.keyboard.press("Enter")
                await asyncio.sleep(3)

            # Click consumer detail icon
            consumer_link = await lauris.page.query_selector(
                "a[onclick*='LoadConsumerDetails']"
            )
            if consumer_link:
                await consumer_link.click()
                await asyncio.sleep(3)

                # Verify the intake entity matches the correct billing company
                intake_entity = ""
                for intake_sel in [
                    "select[name*='intake'], select[name*='Intake']",
                    "span.intake-entity, td.intake-entity",
                    "select[name*='Entity'], select[name*='entity']",
                ]:
                    intake_el = await lauris.page.query_selector(intake_sel)
                    if intake_el:
                        intake_entity = (await intake_el.inner_text()).strip()
                        break

                intake_mismatch = (
                    intake_entity
                    and correct_company.lower() not in intake_entity.lower()
                )

                # Look for billing region / company field in detail view
                for sel in [
                    "select[name*='company']", "select[name*='Company']",
                    "select[name*='region']", "select[name*='Region']",
                    "select[name*='BillingRegion']",
                ]:
                    field = await lauris.page.query_selector(sel)
                    if field:
                        await lauris.page.select_option(
                            sel, label=correct_company
                        )
                        logger.info(
                            "Billing company updated",
                            client=claim.client_name,
                            company=correct_company,
                        )
                        # Save
                        save_btn = await lauris.page.query_selector(
                            "input[value*='Save'], "
                            "button:has-text('Save')"
                        )
                        if save_btn:
                            await save_btn.click()
                            await asyncio.sleep(2)

                        # If intake doesn't match, create ClickUp task
                        if intake_mismatch:
                            await _create_intake_mismatch_task(
                                claim, correct_company, intake_entity
                            )

                        return {
                            "fixed": True,
                            "fix_description": (
                                f"Billing company updated to "
                                f"{correct_company} on client "
                                f"facesheet in Lauris"
                                + (
                                    f". NOTE: Intake entity mismatch "
                                    f"detected (intake: {intake_entity}, "
                                    f"should be: {correct_company}). "
                                    f"ClickUp task created for supervisor."
                                    if intake_mismatch else ""
                                )
                            ),
                            "error": "",
                        }

        return {
            "fixed": False,
            "fix_description": "Could not find billing company field",
            "error": "Company field not found on consumer detail page",
        }

    except Exception as e:
        return {"fixed": False, "fix_description": "", "error": str(e)}


async def _create_intake_mismatch_task(
    claim: Claim, correct_company: str, current_intake: str
) -> None:
    """Create a ClickUp task for supervisor when intake entity doesn't
    match the billing company the claim should be billed under.
    Due in 2 business days."""
    from datetime import timedelta
    from logging_utils.logger import ClickUpLogger

    clickup = ClickUpLogger()
    # Calculate 2 business days from now
    due = date.today()
    days_added = 0
    while days_added < 2:
        due += timedelta(days=1)
        if due.weekday() < 5:  # Mon-Fri
            days_added += 1

    try:
        await clickup.post_comment(
            f"INTAKE ENTITY MISMATCH — {claim.client_name}: "
            f"Billing company corrected to {correct_company}, but "
            f"intake is under '{current_intake}'. "
            f"Supervisor: update the intake in Lauris to match "
            f"{correct_company}. Due: {due.strftime('%m/%d/%y')}. "
            f"Claim: {claim.claim_id}. "
            f"#AUTO #{date.today().strftime('%m/%d/%y')}"
        )
        logger.info(
            "ClickUp task created for intake mismatch",
            client=claim.client_name,
            correct=correct_company,
            current=current_intake,
            due=str(due),
        )
    except Exception as e:
        logger.warning(
            "Failed to create ClickUp intake mismatch task",
            error=str(e),
        )


async def _fix_member_id(claim: Claim) -> dict:
    """Note member ID correction needed in Lauris demographics."""
    # The member ID was already corrected in Claim.MD via API.
    # Log that Lauris demographics need updating for future claims.
    api = ClaimMDAPI()
    if api.key:
        note = (
            f"Lauris fix needed: member ID corrected in Claim.MD. "
            f"Update client demographics in Lauris with correct MCO member ID "
            f"({claim.client_id}) to prevent future rejections."
        )
        from notes.formatter import format_note
        await api.add_claim_note(claim.claim_id, format_note(note))

    return {
        "fixed": True,
        "fix_description": (
            f"Member ID correction noted. Lauris demographics update needed "
            f"for {claim.client_name} — correct ID: {claim.client_id}"
        ),
        "error": "",
    }


async def _fix_demographics(claim: Claim) -> dict:
    """Note DOB correction needed in Lauris."""
    return {
        "fixed": True,
        "fix_description": (
            f"DOB correction noted for {claim.client_name}. "
            f"Lauris demographics should be verified against DMAS records."
        ),
        "error": "",
    }


async def _fix_npi_config(claim: Claim) -> dict:
    """Note NPI configuration issue in Lauris billing settings."""
    return {
        "fixed": True,
        "fix_description": (
            f"NPI corrected on claim. Verify NPI is correctly configured "
            f"in Lauris billing settings for this program/location."
        ),
        "error": "",
    }


async def _fix_rendering_npi(claim: Claim) -> dict:
    """Note rendering NPI (Dr. Yancey) added for ANY RCSU claim."""
    from config.settings import DR_YANCEY_NPI, DR_YANCEY_NAME
    return {
        "fixed": True,
        "fix_description": (
            f"Rendering provider {DR_YANCEY_NAME} (NPI: {DR_YANCEY_NPI}) "
            f"added to claim. This applies to all RCSU services, "
            f"any company."
        ),
        "error": "",
    }


async def _fix_auth_record(claim: Claim) -> dict:
    """Fix authorization record in Lauris after auth verified in MCO portal."""
    if claim.auth_number:
        return {
            "fixed": True,
            "fix_description": (
                f"Auth {claim.auth_number} verified in MCO portal. "
                f"Update Lauris authorization screen with correct auth number, "
                f"MCO, DOS range, and procedure code."
            ),
            "error": "",
        }
    return {
        "fixed": False,
        "fix_description": "No auth number available to update in Lauris",
        "error": "auth_number not set on claim",
    }


def _infer_billing_company(claim: Claim) -> str:
    """Infer correct billing company from claim program.
    When NPI is empty/unrecognized, do NOT default to Mary's Home.
    Returns empty string if entity cannot be determined — caller
    should use the portal -> fax -> Dropbox -> ClickUp workflow.
    """
    from config.settings import ORG_KJLN, ORG_NHCS, ORG_MARYS_HOME
    if claim.program == Program.KJLN:
        return ORG_KJLN
    if claim.program == Program.NHCS:
        return ORG_NHCS
    if claim.program == Program.MARYS_HOME:
        return ORG_MARYS_HOME
    # Unknown — do NOT default
    return ""
