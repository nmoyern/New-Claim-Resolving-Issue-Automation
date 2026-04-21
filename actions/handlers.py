"""
actions/handlers.py
--------------------
High-level action handlers — each maps to one ResolutionAction.
These orchestrate the lower-level portal sessions.
Called by the orchestrator with a Claim and the determined action.
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import date
from pathlib import Path
from typing import List, Optional

from config.models import (
    Claim,
    ClaimStatus,
    DenialCode,
    ERA,
    MCO,
    ResolutionAction,
    ResolutionResult,
)
from config.entities import get_entity_by_npi, get_entity_by_program
from config.settings import DRY_RUN, ORG_KJLN, ORG_NHCS, ORG_MARYS_HOME, DR_YANCEY_NPI


def _client_info_block(claim: "Claim") -> str:
    """Standard client info block for all ClickUp task descriptions.
    Always includes the Lauris Unique ID — staff need it to find the client."""
    lines = [f"Client: {claim.client_name}"]
    if claim.lauris_id:
        lines.append(f"Lauris Unique ID: {claim.lauris_id}")
    lines.append(f"Member ID / Medicaid #: {claim.client_id}")
    lines.append(f"Claim ID: {claim.claim_id}")
    lines.append(f"MCO: {claim.mco.value}")
    lines.append(f"DOS: {claim.dos}")
    lines.append(f"Amount: ${claim.billed_amount:.2f}")
    return "\n".join(lines)
from notes.formatter import (
    note_auth_not_found_fax_sent,
    note_auth_verified_in_portal,
    note_billing_company_fixed,
    note_correction,
    note_human_review_needed,
    note_reconsideration_submitted,
    note_write_off,
)
from sources.claimmd import ClaimMDSession
from sources.claimmd_api import ClaimMDAPI
from lauris.billing import LaurisSession
from lauris.authorization import check_lauris_authorization
from lauris.diagnosis import _lookup_uid_from_record_number
from mco_portals.auth_checker import get_auth_checker, AvailityPortal
from logging_utils.logger import get_logger
from reporting.autonomous_tracker import log_autonomous_correction

logger = get_logger("action_handlers")

# Cache: client_id -> {"icd_code": "F25.0", "description": "...", "uid": "ID003496"}
# Prevents re-opening Lauris for multiple claims from the same client
_diagnosis_cache: dict = {}


async def _is_claim_already_resolved(claim_id: str, claim: "Claim" = None) -> bool:
    """
    Pre-check: query Claim.MD for the latest response on this claim.
    Also checks Availity claim status for Aetna/Anthem/Humana/Molina.
    Returns True if the claim has already been accepted/paid.
    Prevents re-fixing claims that were corrected and paid since
    the rejection was first captured.
    """
    # Check 1: Claim.MD API
    try:
        api = ClaimMDAPI()
        if api.key:
            latest = await api.get_claim_responses(
                response_id="0", claim_id=claim_id
            )
            if latest:
                for resp in reversed(latest):
                    status = resp.get("status", "")
                    if status == "A":
                        logger.info(
                            "Claim already accepted/paid (Claim.MD)",
                            claim_id=claim_id,
                        )
                        return True
                    if status in ("R", "D"):
                        break  # Still rejected — check Availity too
    except Exception as e:
        logger.warning(
            "Claim.MD pre-check failed",
            claim_id=claim_id,
            error=str(e)[:60],
        )

    # Check 2: Availity claim status (for Aetna/Anthem/Humana/Molina)
    if claim and claim.mco in (MCO.AETNA, MCO.ANTHEM, MCO.HUMANA, MCO.MOLINA):
        try:
            from mco_portals.auth_checker import AvailityPortal
            async with AvailityPortal(headless=True) as portal:
                status_result = await portal.check_claim_status(claim)
                if status_result and status_result.get("status") == "paid":
                    logger.info(
                        "Claim already paid (Availity)",
                        claim_id=claim_id,
                        paid_amount=status_result.get("paid_amount"),
                        mco=claim.mco.value,
                    )
                    return True
        except Exception as e:
            logger.debug(
                "Availity status check unavailable",
                claim_id=claim_id,
                error=str(e)[:60],
            )

    return False


# Temp directory for downloaded docs
WORK_DIR = Path("/tmp/claims_work")

# MCO authorization fax numbers — where to send refax packages
# These should be updated from the Admin Logins Google Sheet
MCO_AUTH_FAX_NUMBERS = {
    MCO.SENTARA:  os.environ.get("FAX_SENTARA", ""),
    MCO.AETNA:    os.environ.get("FAX_AETNA", ""),
    MCO.ANTHEM:   os.environ.get("FAX_ANTHEM", ""),
    MCO.MOLINA:   os.environ.get("FAX_MOLINA", ""),
    MCO.HUMANA:   os.environ.get("FAX_HUMANA", ""),
    MCO.MAGELLAN: os.environ.get("FAX_MAGELLAN", ""),
    # United does NOT use fax — always portal submission
    # DMAS — separate process
}
WORK_DIR.mkdir(parents=True, exist_ok=True)

# Persistent log of ERA IDs successfully uploaded to Lauris.
# Prevents re-uploading if the script re-runs after a partial failure,
# since Claim.MD's new_only flag only tracks downloads, not Lauris uploads.
ERA_UPLOAD_LOG = Path("data/era_upload_log.json")


def _load_uploaded_era_ids() -> set:
    """Return the set of ERA IDs already uploaded to Lauris."""
    ERA_UPLOAD_LOG.parent.mkdir(parents=True, exist_ok=True)
    if ERA_UPLOAD_LOG.exists():
        try:
            return set(json.loads(ERA_UPLOAD_LOG.read_text()))
        except Exception:
            return set()
    return set()


def _record_uploaded_era_id(era_id: str) -> None:
    """Append an ERA ID to the persistent upload log."""
    ids = _load_uploaded_era_ids()
    ids.add(era_id)
    ERA_UPLOAD_LOG.write_text(json.dumps(sorted(ids), indent=2))


# ---------------------------------------------------------------------------
# ERA Upload Handler
# ---------------------------------------------------------------------------

async def handle_era_upload(eras: List[ERA]) -> ResolutionResult:
    """
    Download ERAs from Claim.MD (via API) and upload to Lauris.
    Skips irregular ERAs (Anthem Mary's, etc.) and flags them.
    Skips ERAs already recorded in data/era_upload_log.json so that
    re-runs never double-upload to Lauris.
    """
    from config.models import Program
    from lauris.billing import classify_era, LaurisSession

    logger.info("Starting ERA upload batch", count=len(eras))
    downloaded = 0
    uploaded = 0
    skipped_irregular = 0
    skipped_duplicate = 0
    era_dir = WORK_DIR / "eras"
    era_dir.mkdir(parents=True, exist_ok=True)

    already_uploaded = _load_uploaded_era_ids()

    # Step 1: Get ERA list from Claim.MD API
    api = ClaimMDAPI()
    era_objects = []

    if api.key:
        era_list = await api.get_era_list(new_only=True)
        logger.info(f"Found {len(era_list)} ERAs via API")

        for era_info in era_list:
            era_id = str(era_info.get("eraid", ""))
            if not era_id:
                continue

            # Skip if already uploaded to Lauris in a previous run
            if era_id in already_uploaded:
                skipped_duplicate += 1
                logger.info("Skipping already-uploaded ERA", era_id=era_id)
                continue

            payer = era_info.get("payer_name", "")
            npi = era_info.get("prov_npi", "")
            amount = float(era_info.get("paid_amount", "0") or "0")

            # Infer program from NPI
            entity = get_entity_by_npi(npi)
            program = entity.program if entity else Program.UNKNOWN

            # Infer MCO from payer
            from sources.claimmd_api import PAYER_MCO_MAP
            from sources.claimmd import _parse_mco
            payer_id = era_info.get("payerid", "")
            mco_str = PAYER_MCO_MAP.get(payer_id, payer)
            mco = _parse_mco(mco_str)

            era_obj = ERA(
                era_id=era_id,
                mco=mco,
                program=program,
                payment_date=date.today(),
                total_amount=amount,
                file_path="",
            )

            # Classify: skip irregular ERAs
            era_type = classify_era(era_obj)
            if era_type != "standard":
                skipped_irregular += 1
                logger.info("Skipping irregular ERA",
                            era_id=era_id, type=era_type)
                continue

            # Download 835 file via API
            save_path = str(era_dir / f"era_{era_id}.835")
            dl_content = await api.download_era_835(era_id, save_path)
            if dl_content:
                era_obj.file_path = save_path
                era_objects.append(era_obj)
                downloaded += 1
    else:
        # Fallback to browser
        async with ClaimMDSession() as claimmd:
            files = await claimmd.download_eras(str(era_dir))
            downloaded = len(files)

    # Step 2: Upload each downloaded ERA to Lauris.
    # On success, record the ERA ID so it is never re-uploaded.
    # On failure, leave it unrecorded so the next run retries.
    if era_objects:
        async with LaurisSession() as lauris:
            for era_obj in era_objects:
                ok = await lauris.upload_era(era_obj)
                if ok:
                    _record_uploaded_era_id(era_obj.era_id)
                    uploaded += 1
                else:
                    logger.warning(
                        "ERA upload to Lauris failed — will retry next run",
                        era_id=era_obj.era_id,
                    )

    result = ResolutionResult(
        claim=Claim(
            "era_batch", "N/A", "N/A", date.today(),
            MCO.UNKNOWN, Program.UNKNOWN, 0,
        ),
        action_taken=ResolutionAction.ERA_UPLOAD,
        success=uploaded > 0 or downloaded == 0,
        note_written=(
            f"Downloaded {downloaded} ERAs. "
            f"Uploaded {uploaded} to Lauris. "
            f"Skipped {skipped_irregular} irregular. "
            f"Skipped {skipped_duplicate} already uploaded."
        ),
    )
    logger.info("ERA upload complete",
                downloaded=downloaded, uploaded=uploaded,
                skipped_irregular=skipped_irregular,
                skipped_duplicate=skipped_duplicate)
    return result


# ---------------------------------------------------------------------------
# Claim Correction Handler (Step 1)
# ---------------------------------------------------------------------------

async def handle_correct_and_resubmit(claim: Claim) -> ResolutionResult:
    """
    Determine what fields need correction based on denial codes,
    then correct and retransmit in Claim.MD.
    Uses API when available, falls back to browser.

    PRE-CHECK: Verifies the claim is still rejected/denied before acting.
    If it's already been accepted/paid since the rejection, skip it.
    """
    # Pre-check: skip if already resolved
    if await _is_claim_already_resolved(claim.claim_id):
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.SKIP,
            success=True,
            note_written="Skipped — claim already accepted/paid",
        )

    corrections = _build_corrections(claim)

    if not corrections:
        logger.warning("No corrections determined for claim", claim_id=claim.claim_id)
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.CORRECT_AND_RESUBMIT,
            success=False,
            needs_human=True,
            human_reason="Could not determine what to correct — denial code unclear",
        )

    # If corrections require an eligibility lookup, do it now
    source_note = corrections.pop("_correction_source_note", "")
    needs_elig = corrections.pop("_needs_eligibility_lookup", "")
    needs_auth_check = corrections.pop("_needs_auth_portal_check", "")
    needs_diagnosis_fix = corrections.pop("_needs_diagnosis_fix", False)

    # DIAGNOSIS BLANK: Do NOT submit any claim without a diagnosis.
    # First, check the cache (another claim for this client may have already
    # been fixed in this run). If not cached, extract from the Mental Health
    # Assessment in Lauris. If that fails, fall back to ClickUp task.
    if needs_diagnosis_fix:
        # Check cache first — avoids re-opening Lauris for same client
        cached = _diagnosis_cache.get(claim.client_id)
        if cached:
            corrections["diagnosis_code"] = cached["icd_code"]
            needs_diagnosis_fix = False
            logger.info(
                "Diagnosis from cache (same client, already extracted)",
                claim_id=claim.claim_id,
                icd_code=cached["icd_code"],
            )

    if needs_diagnosis_fix:
        # Attempt auto-extraction from Lauris assessment
        lauris_diagnosis = None
        try:
            from lauris.diagnosis import get_diagnosis_for_claim, update_facesheet_diagnosis
            async with LaurisSession() as lauris:
                lauris_diagnosis = await get_diagnosis_for_claim(lauris.page, claim)

                if lauris_diagnosis:
                    icd_code = lauris_diagnosis["icd_code"]
                    description = lauris_diagnosis["description"]
                    logger.info(
                        "Auto-extracted diagnosis from Lauris assessment",
                        claim_id=claim.claim_id,
                        icd_code=icd_code,
                        description=description,
                    )

                    # Update the Client Face Sheet with the extracted diagnosis
                    from lauris.diagnosis import _lookup_uid_from_record_number
                    consumer_uid = await _lookup_uid_from_record_number(
                        lauris.page, claim.client_id
                    )
                    if consumer_uid:
                        await update_facesheet_diagnosis(
                            lauris.page, consumer_uid, icd_code, description
                        )

                    # Apply the diagnosis to the claim corrections
                    corrections["diagnosis_code"] = icd_code
                    needs_diagnosis_fix = False

                    # Cache for other claims from the same client
                    _diagnosis_cache[claim.client_id] = {
                        "icd_code": icd_code,
                        "description": description,
                        "uid": consumer_uid or "",
                    }

        except Exception as e:
            logger.warning(
                "Lauris diagnosis auto-extraction failed — falling back to ClickUp",
                claim_id=claim.claim_id,
                error=str(e),
            )

        # If auto-extraction succeeded, continue with normal correction flow
        # (needs_diagnosis_fix is now False, corrections has the diagnosis code)
        if needs_diagnosis_fix:
            # Auto-extraction failed — fall back to ClickUp task
            try:
                from actions.clickup_tasks import (
                    ClickUpTaskCreator, _next_business_day,
                    PRIORITY_HIGH, get_assignees,
                )
                from actions.clickup_poller import store_task_metadata
                tc = ClickUpTaskCreator()
                today_str = date.today().strftime("%m/%d/%y")
                diag_task_id = await tc.create_task(
                    list_id=tc.list_id,
                    name=(
                        f"Diagnosis Missing — {claim.client_name} "
                        f"[{today_str}]"
                    ),
                    description=(
                        f"Claim denied — diagnosis code is blank.\n\n"
                        f"{_client_info_block(claim)}\n\n"
                        f"Automation attempted to extract the diagnosis from "
                        f"the Mental Health Assessment 3.0 in Lauris but "
                        f"could not find it.\n\n"
                        f"This claim CANNOT be submitted without a "
                        f"diagnosis. Please comment on this task with the "
                        f"correct diagnosis code from the assessment.\n\n"
                        f"Generated by Claims Automation on {today_str}."
                    ),
                    assignees=get_assignees(),
                    due_date=_next_business_day(),
                    priority=PRIORITY_HIGH,
                )
                if diag_task_id:
                    store_task_metadata(
                        diag_task_id, "diagnosis_missing", claim.claim_id
                    )
            except Exception as e:
                logger.error("Failed to create diagnosis ClickUp task",
                             claim_id=claim.claim_id, error=str(e))

            return ResolutionResult(
                claim=claim,
                action_taken=ResolutionAction.CORRECT_AND_RESUBMIT,
                success=False,
                needs_human=True,
                human_reason=(
                    "Diagnosis code is blank. Lauris assessment extraction "
                    "failed. ClickUp task created. "
                    "Cannot submit claim until diagnosis is provided."
                ),
            )

    if needs_elig and claim.client_name:
        api_elig = ClaimMDAPI()
        if api_elig.key:
            try:
                name_parts = claim.client_name.split()
                entity = get_entity_by_npi(claim.npi) or get_entity_by_program(claim.program)
                provider_npi = entity.billing_npi if entity else claim.npi
                provider_taxid = entity.tax_id if entity else ""
                elig = await api_elig.check_eligibility(
                    member_last=name_parts[-1],
                    member_first=name_parts[0],
                    payer_id=claim.mco.value if claim.mco.value != "unknown" else "",
                    service_date=claim.dos.strftime("%Y%m%d"),
                    provider_npi=provider_npi,
                    provider_taxid=provider_taxid,
                    member_id=claim.client_id,
                )
                if elig and not elig.get("error"):
                    if needs_elig == "member_id" and elig.get("ins_number"):
                        corrections["member_id"] = elig["ins_number"]
                        logger.info("Eligibility API returned correct member ID",
                                    claim_id=claim.claim_id, new_id=elig["ins_number"])
                    elif needs_elig == "dob" and elig.get("ins_dob"):
                        corrections["dob"] = elig["ins_dob"]
                        logger.info("Eligibility API returned correct DOB",
                                    claim_id=claim.claim_id)
            except Exception as e:
                logger.warning("Eligibility lookup failed — using existing data",
                               claim_id=claim.claim_id, error=str(e))

    if needs_auth_check:
        # Check MCO portal auth to determine correct billing company/NPI
        try:
            checker = get_auth_checker(claim.mco)
            if checker:
                async with checker as portal:
                    auth_found, auth_record = await portal.check_auth(claim)
                    if auth_found and auth_record and auth_record.program:
                        from config.models import Program as Prog
                        region_map = {
                            Prog.KJLN: ORG_KJLN,
                            Prog.NHCS: ORG_NHCS,
                            Prog.MARYS_HOME: ORG_MARYS_HOME,
                        }
                        correct = region_map.get(auth_record.program)
                        if correct:
                            corrections["billing_region"] = correct
                            logger.info("Auth portal confirmed correct billing company",
                                        claim_id=claim.claim_id, company=correct)
        except Exception as e:
            logger.warning("Auth portal check for billing company failed",
                           claim_id=claim.claim_id, error=str(e))

    # NHCS MHSS rate adjustment: ensure $9.80/unit before resubmission
    from config.models import Program
    if (claim.program == Program.NHCS
            and (claim.service_code or "").upper() == "MHSS"
            and claim.units > 0):
        correct_amount = round(claim.units * 102.72, 2)
        if abs(claim.billed_amount - correct_amount) > 0.01:
            corrections["total_charge"] = str(correct_amount)
            claim.billed_amount = correct_amount
            claim.rate_per_unit = 9.80
            logger.info(
                "NHCS MHSS rate correction applied",
                claim_id=claim.claim_id,
                units=claim.units,
                corrected_charge=correct_amount,
            )

    # Use API if available (faster, more reliable, no browser needed)
    api = ClaimMDAPI()
    if api.key:
        success = await api.modify_claim(claim.claim_id, corrections)
        if success:
            correction_desc = ", ".join(f"{k}={v}" for k, v in corrections.items())
            if source_note:
                correction_desc += f". Source: {source_note}"
            note = note_correction(correction_desc)
            await api.add_claim_note(claim.claim_id, note)
    else:
        async with ClaimMDSession() as claimmd:
            success = await claimmd.correct_and_resubmit(claim, corrections)

    # Rule 2: Fix root cause in Lauris so same denial doesn't recur
    if success and claim.denial_codes:
        try:
            from actions.lauris_fixes import fix_root_cause
            fix_result = await fix_root_cause(claim, claim.denial_codes[0])
            if fix_result.get("fixed"):
                logger.info("Lauris root cause fixed",
                            claim_id=claim.claim_id,
                            fix=fix_result["fix_description"][:80])
        except Exception as e:
            logger.warning("Lauris root cause fix failed",
                           claim_id=claim.claim_id, error=str(e))

    # Log autonomous correction if successful
    if success:
        correction_desc = ", ".join(f"{k}={v}" for k, v in corrections.items())
        # Determine correction type from what was changed
        correction_type = "resubmitted"
        if "npi" in corrections:
            correction_type = "npi_fix"
        elif "billing_region" in corrections:
            correction_type = "entity_fix"
        elif "member_id" in corrections:
            correction_type = "member_id_fix"
        elif "rendering_npi" in corrections:
            correction_type = "rendering_npi_added"
        elif "diagnosis_code" in corrections:
            correction_type = "diagnosis_fix"
        elif "total_charge" in corrections:
            correction_type = "mhss_rate_fix"

        log_autonomous_correction(
            claim_id=claim.claim_id,
            client_name=claim.client_name,
            client_id=claim.client_id,
            correction_type=correction_type,
            correction_detail=correction_desc[:500],
            dollars_at_stake=claim.billed_amount,
        )

    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.CORRECT_AND_RESUBMIT,
        success=success,
        note_written=f"Corrected {list(corrections.keys())} and retransmitted.",
    )


def _build_corrections(claim: Claim) -> dict:
    """Map denial codes to the field corrections needed."""
    corrections = {}
    for code in claim.denial_codes:
        if code == DenialCode.INVALID_ID:
            # Use Claim.MD eligibility API to look up the correct member ID from
            # the MCO — claim.client_id may be wrong (that's why it was rejected).
            corrections["member_id"] = claim.client_id  # placeholder — overridden by async lookup
            corrections["_needs_eligibility_lookup"] = "member_id"
            corrections["_correction_source_note"] = (
                "Correct member ID obtained via Claim.MD eligibility API (270/271 lookup against MCO)."
            )
        elif code == DenialCode.INVALID_DOB:
            # Use Claim.MD eligibility API to verify DOB against MCO records.
            corrections["_needs_eligibility_lookup"] = "dob"
            corrections["_correction_source_note"] = (
                "Correct DOB verified via Claim.MD eligibility API (270/271 lookup against MCO)."
            )
        elif code == DenialCode.INVALID_NPI:
            from config.settings import MARYS_HOME_NPI
            corrections["npi"] = MARYS_HOME_NPI
        elif code == DenialCode.WRONG_BILLING_CO:
            # Check the authorization in the MCO portal to see which company/NPI
            # was on the approved auth, then match the claim to that entity.
            corrections["billing_region"] = _infer_correct_billing_region(claim)
            corrections["_needs_auth_portal_check"] = "billing_company"
            corrections["_correction_source_note"] = (
                "Correct billing company determined from MCO portal authorization "
                "(matched approved auth NPI/entity to LCI company)."
            )
        elif code == DenialCode.MISSING_NPI_RENDERING:
            # Dr. Yancey is rendering provider for ANY RCSU claim, any company
            corrections["rendering_npi"] = DR_YANCEY_NPI
        elif code == DenialCode.PROVIDER_NOT_CERTIFIED:
            # Retransmit as-is first — if processed once, usually resolves
            pass  # Correction handler will resubmit without changes
        elif code == DenialCode.COVERAGE_TERMINATED:
            # Check eligibility via API before deciding action
            pass  # Handled by dedicated coverage terminated handler
        elif code in (DenialCode.DIAGNOSIS_BLANK, DenialCode.INVALID_DIAG):
            # Do NOT submit without a valid diagnosis — try Lauris extraction first,
            # then fall back to ClickUp
            corrections["_needs_diagnosis_fix"] = True
    return corrections


async def handle_coverage_terminated(claim: Claim) -> ResolutionResult:
    """
    Handle 'Coverage Terminated' / 'Patient Not Enrolled' denials.
    Per Claims Troubleshooting Guide:
      1. Check eligibility in DMAS/MES portal
      2. If insurance changed to non-Anthem/Aetna: notify supervisor, request new SRA,
         tell them re-assessment must be completed and individual has 0 available units
      3. If changed to Anthem/Aetna: reduce units to 0 in Lauris, notify supervisor
         to help individual switch insurance, notify units are at 0
      4. If no change: verify name/DOB/ID and resubmit
      5. Always reduce available units to 0 when coverage is terminated
    """
    if await _is_claim_already_resolved(claim.claim_id):
        return ResolutionResult(
            claim=claim, action_taken=ResolutionAction.SKIP, success=True,
            note_written="Skipped — claim already accepted/paid",
        )
    logger.info("Handling coverage terminated", claim_id=claim.claim_id)

    # Always reduce available units to 0 when coverage is terminated
    try:
        async with LaurisSession() as lauris:
            await lauris.update_available_units(
                claim.client_name, claim.client_id, units=0
            )
            logger.info(
                "Reduced available units to 0 for terminated coverage",
                claim_id=claim.claim_id, client=claim.client_name,
            )
    except Exception as e:
        logger.warning("Failed to reduce units to 0 in Lauris",
                       claim_id=claim.claim_id, error=str(e))

    # Create ClickUp task to notify that person has 0 units
    try:
        from actions.clickup_tasks import (
            ClickUpTaskCreator, _next_business_day, PRIORITY_HIGH,
        )
        tc = ClickUpTaskCreator()
        today_str = date.today().strftime("%m/%d/%y")
        from actions.clickup_tasks import get_assignees
        await tc.create_task(
            list_id=tc.list_id,
            name=(
                f"Coverage Terminated — 0 Units — "
                f"{claim.client_name} [{today_str}]"
            ),
            description=(
                f"Coverage terminated — units zeroed out in Lauris.\n\n"
                f"{_client_info_block(claim)}\n\n"
                f"Individual has 0 available units.\n\n"
                f"Generated by Claims Automation on {today_str}."
            ),
            assignees=get_assignees("insurance_change"),
            due_date=_next_business_day(),
            priority=PRIORITY_HIGH,
        )
    except Exception as e:
        logger.warning("Failed to create 0-units ClickUp task",
                       claim_id=claim.claim_id, error=str(e))

    # Use Claim.MD eligibility API to check current coverage
    api = ClaimMDAPI()
    changed_to_anthem_aetna = False
    insurance_changed = False

    if api.key and claim.client_name:
        name_parts = claim.client_name.split()
        if len(name_parts) >= 2:
            entity = get_entity_by_npi(claim.npi) or get_entity_by_program(claim.program)
            provider_npi = entity.billing_npi if entity else claim.npi
            provider_taxid = entity.tax_id if entity else ""
            elig = await api.check_eligibility(
                member_last=name_parts[-1],
                member_first=name_parts[0],
                payer_id=claim.mco.value if claim.mco.value != "unknown" else "",
                service_date=claim.dos.strftime("%Y%m%d"),
                provider_npi=provider_npi,
                provider_taxid=provider_taxid,
                member_id=claim.client_id,
            )

            if elig and not elig.get("error"):
                # Check if insurance changed
                new_payer = str(elig.get("payer_name", "")).lower()
                if new_payer and new_payer != claim.mco.value.lower():
                    insurance_changed = True
                    changed_to_anthem_aetna = (
                        "anthem" in new_payer or "aetna" in new_payer
                    )

    from logging_utils.logger import ClickUpLogger
    clickup = ClickUpLogger()

    if changed_to_anthem_aetna:
        # Changed to Anthem/Aetna: reduce units to 0, notify supervisor
        note = (
            f"Coverage terminated — insurance changed to Anthem/Aetna. "
            f"Available units reduced to 0 in Lauris. "
            f"Supervisor notified to help individual switch insurance. "
            f"Units are at 0."
        )
        from notes.formatter import format_note
        await api.add_claim_note(claim.claim_id, format_note(note))
        await clickup.post_comment(
            f"COVERAGE TERMINATED — {claim.client_name}: Insurance changed to Anthem/Aetna. "
            f"Available units reduced to 0. Supervisor: help individual switch insurance. "
            f"Units are at 0. Claim: {claim.claim_id}. "
            f"#AUTO #{date.today().strftime('%m/%d/%y')}"
        )
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.HUMAN_REVIEW,
            success=True,
            needs_human=True,
            human_reason=(
                "Coverage terminated — changed to Anthem/Aetna. "
                "Units reduced to 0. Supervisor must help individual switch insurance."
            ),
        )

    if insurance_changed:
        # Changed to a different MCO (not Anthem/Aetna): notify supervisor, request new SRA
        note = (
            f"Coverage terminated — insurance changed to a new MCO. "
            f"Available units reduced to 0 in Lauris. "
            f"Supervisor notified: re-assessment must be completed, "
            f"individual has 0 available units, new SRA required."
        )
        from notes.formatter import format_note
        await api.add_claim_note(claim.claim_id, format_note(note))
        await clickup.post_comment(
            f"COVERAGE TERMINATED — {claim.client_name}: Insurance changed to new MCO. "
            f"Available units reduced to 0. Supervisor: re-assessment must be completed, "
            f"individual has 0 available units, new SRA must be submitted. "
            f"Claim: {claim.claim_id}. "
            f"#AUTO #{date.today().strftime('%m/%d/%y')}"
        )
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.HUMAN_REVIEW,
            success=True,
            needs_human=True,
            human_reason=(
                "Coverage terminated — insurance changed to new MCO. "
                "Units reduced to 0. Supervisor must complete re-assessment "
                "and submit new SRA. Individual has 0 available units."
            ),
        )

    # No insurance change detected — units still reduced to 0, flag for human
    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.HUMAN_REVIEW,
        success=False,
        needs_human=True,
        human_reason=(
            "Coverage terminated denial. Eligibility check performed. "
            "Available units reduced to 0. "
            "Human must verify: did insurance change? If yes, notify "
            "life coach/supervisor via ClickUp and request new SRA. "
            "Re-assessment must be completed. Individual has 0 available units."
        ),
    )


def _infer_correct_billing_region(claim: Claim) -> str:
    """
    Infer the correct billing region from claim program.
    When NPI is empty/unrecognized, do NOT default to Mary's Home.
    Instead return empty string so callers can invoke the
    portal -> fax -> Dropbox -> ClickUp entity determination workflow.
    """
    from config.models import Program
    if claim.program == Program.KJLN:
        return ORG_KJLN
    if claim.program == Program.NHCS:
        return ORG_NHCS
    if claim.program == Program.MARYS_HOME:
        return ORG_MARYS_HOME
    # Unknown program — do NOT default; caller must determine entity
    return ""


async def _determine_entity_or_clickup(claim: Claim) -> str:
    """
    Entity determination workflow (Comment 10/23):
    1. Check MCO portals for auth
    2. Check Lauris fax records
    3. Check Nextiva fax records
    4. Check Dropbox
    5. If still can't determine, create ClickUp task to Justin
    Returns the entity string, or empty string if ClickUp task was created.
    """
    # Step 1: Check MCO portal for auth
    try:
        checker = get_auth_checker(claim.mco)
        if checker:
            async with checker as portal:
                auth_found, auth_record = await portal.check_auth(claim)
                if auth_found and auth_record and auth_record.program:
                    from config.models import Program as Prog
                    region_map = {
                        Prog.KJLN: ORG_KJLN,
                        Prog.NHCS: ORG_NHCS,
                        Prog.MARYS_HOME: ORG_MARYS_HOME,
                    }
                    entity = region_map.get(auth_record.program, "")
                    if entity:
                        logger.info(
                            "Entity determined from MCO portal auth",
                            claim_id=claim.claim_id, entity=entity,
                        )
                        return entity
    except Exception as e:
        logger.warning("MCO portal entity check failed",
                       claim_id=claim.claim_id, error=str(e))

    # Step 2: Check ALL fax sources via fax_log DB
    # (Lauris sent, Nextiva nmoyern sent, Nextiva nmoyern2 sent, Nextiva received)
    try:
        from actions.fax_tracker import get_sent_fax_for_client

        fax_entries = get_sent_fax_for_client(
            client_name=claim.client_name,
            mco=claim.mco.value if claim.mco else None,
        )
        for entry in fax_entries:
            entity = entry.get("company", "")
            source = entry.get("source", "")
            if entity:
                # Normalize entity name
                entity_lower = entity.lower()
                if "kjln" in entity_lower:
                    logger.info(
                        "Entity determined from fax log",
                        claim_id=claim.claim_id, entity=ORG_KJLN,
                        source=source,
                    )
                    return ORG_KJLN
                elif "nhcs" in entity_lower or "new heights" in entity_lower:
                    logger.info(
                        "Entity determined from fax log",
                        claim_id=claim.claim_id, entity=ORG_NHCS,
                        source=source,
                    )
                    return ORG_NHCS
                elif "mary" in entity_lower:
                    logger.info(
                        "Entity determined from fax log",
                        claim_id=claim.claim_id, entity=ORG_MARYS_HOME,
                        source=source,
                    )
                    return ORG_MARYS_HOME
    except Exception as e:
        logger.warning("Fax log entity check failed",
                       claim_id=claim.claim_id, error=str(e))

    # Step 4: Check Dropbox
    try:
        from actions.dropbox_verify import verify_dropbox_auth
        dropbox_result = await verify_dropbox_auth(
            claim.client_name, claim.mco.value, claim.claim_id
        )
        if dropbox_result.get("found"):
            path = dropbox_result.get("path", "")
            # Infer entity from Dropbox path
            if "KJLN" in path.upper():
                return ORG_KJLN
            elif "NHCS" in path.upper():
                return ORG_NHCS
            elif "MARY" in path.upper():
                return ORG_MARYS_HOME
    except Exception as e:
        logger.warning("Dropbox entity check failed",
                       claim_id=claim.claim_id, error=str(e))

    # Step 5: Create ClickUp task to Justin
    try:
        from actions.clickup_tasks import (
            ClickUpTaskCreator, _next_business_day, PRIORITY_HIGH,
        )
        tc = ClickUpTaskCreator()
        today = date.today().strftime("%m/%d/%y")
        from actions.clickup_tasks import get_assignees
        from actions.clickup_poller import store_task_metadata
        entity_task_id = await tc.create_task(
            list_id=tc.list_id,
            name=(
                f"Entity Unknown — {claim.client_name} "
                f"[{today}]"
            ),
            description=(
                f"Could not determine the correct billing entity.\n\n"
                f"{_client_info_block(claim)}\n\n"
                f"Checked: MCO portal, Lauris fax, Nextiva fax, Dropbox "
                f"— none returned entity info.\n\n"
                f"Justin: please identify the correct entity (KJLN, NHCS, "
                f"or Mary's Home) and comment on this task.\n\n"
                f"Generated by Claims Automation on {today}."
            ),
            assignees=get_assignees("entity_fix"),
            due_date=_next_business_day(),
            priority=PRIORITY_HIGH,
        )
        if entity_task_id:
            store_task_metadata(
                entity_task_id, "entity_fix", claim.claim_id
            )
        logger.info("ClickUp task created for unknown entity",
                     claim_id=claim.claim_id)
    except Exception as e:
        logger.error("Failed to create entity ClickUp task",
                     claim_id=claim.claim_id, error=str(e))

    return ""


# ---------------------------------------------------------------------------
# MCO Portal Auth Check Handler
# ---------------------------------------------------------------------------

async def handle_mco_auth_check(claim: Claim) -> ResolutionResult:
    """
    Auth verification for the narrowed rejected/denied claim workflow.

    Steps:
      1. Try to obtain the auth via API (Availity 278I for Anthem/Aetna/
         Molina/Humana, Optum GraphQL for UHC).
      2. If auth found and matches claim service/entity/dates:
         a. If auth already on claim → flag for reconsideration.
         b. If auth NOT on claim → attach it and resubmit.
      3. If no auth found → create ClickUp task for human follow-up.
    """
    if await _is_claim_already_resolved(claim.claim_id):
        return ResolutionResult(
            claim=claim, action_taken=ResolutionAction.SKIP, success=True,
            note_written="Skipped — claim already accepted/paid",
        )
    logger.info("Handling MCO auth check", claim_id=claim.claim_id, mco=claim.mco.value)

    # ------------------------------------------------------------------
    # Step 1: Try to obtain auth via payer API (278I / Optum)
    # ------------------------------------------------------------------
    from sources.payer_auth_lookup import PayerAuthorizationLookup
    from sources.lauris_demographics import (
        fetch_lauris_demographics,
        enrich_claim_with_demographics,
    )

    # Enrich claim with DOB/gender from Lauris (needed for API calls)
    try:
        demos = fetch_lauris_demographics()
        enrich_claim_with_demographics(claim, demos)
    except Exception as exc:
        logger.warning("Demographics enrichment failed", error=str(exc)[:100])

    # Determine which entity billed this claim
    entity = get_entity_by_npi(claim.npi) or get_entity_by_program(claim.program)
    if not entity:
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.MCO_PORTAL_AUTH_CHECK,
            success=False,
            needs_human=True,
            human_reason="Cannot determine billing entity for auth lookup.",
        )

    lookup = PayerAuthorizationLookup()
    auth_result = None
    try:
        auth_result = await lookup.obtain_authorization(claim, entity)
    except Exception as exc:
        logger.warning(
            "Auth API lookup failed",
            claim_id=claim.claim_id,
            error=str(exc)[:200],
        )

    if auth_result and auth_result.found and auth_result.auth_number:
        auth_num = auth_result.auth_number
        logger.info(
            "Auth obtained via API",
            claim_id=claim.claim_id,
            auth=auth_num,
            reason=auth_result.reason,
        )

        # Check if auth is already on the claim
        if claim.auth_number and claim.auth_number == auth_num:
            # Auth already on claim but still denied → reconsideration
            note = (
                f"Auth #{auth_num} confirmed via payer API and already on claim. "
                f"{auth_result.reason}. Flagging for reconsideration."
            )
            api = ClaimMDAPI()
            if api.key:
                from notes.formatter import format_note
                await api.add_claim_note(claim.claim_id, format_note(note))
            return ResolutionResult(
                claim=claim,
                action_taken=ResolutionAction.MCO_PORTAL_AUTH_CHECK,
                success=False,
                needs_human=True,
                human_reason=(
                    f"Auth #{auth_num} is on claim and confirmed by payer, "
                    f"but claim was still denied. Needs reconsideration."
                ),
                note_written=note,
            )

        # Auth found but NOT on claim → attach and resubmit
        claim.auth_number = auth_num
        corrections = {"auth_number": auth_num}
        note = (
            f"Auth #{auth_num} obtained from payer API. "
            f"{auth_result.reason}. "
            f"Adding to claim and resubmitting."
        )

        api = ClaimMDAPI()
        success = False
        if api.key:
            success = await api.modify_claim(claim.claim_id, corrections)
            if success:
                from notes.formatter import format_note
                await api.add_claim_note(claim.claim_id, format_note(note))
                log_autonomous_correction(
                    claim.claim_id,
                    "api_auth_added",
                    note,
                )
                logger.info(
                    "Auth added to claim and resubmitted",
                    claim_id=claim.claim_id,
                    auth=auth_num,
                )

        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.MCO_PORTAL_AUTH_CHECK,
            success=success,
            note_written=note,
        )

    # ------------------------------------------------------------------
    # Auth not found via payer API — go to human review.
    # Do NOT fall back to Lauris for auth/company/MCO data — staff
    # entries are unreliable.  Payer API is the source of truth.
    # ------------------------------------------------------------------
    api_reason = auth_result.reason if auth_result else "API lookup not attempted"
    logger.info(
        "Auth not found via payer API — escalating to human review",
        claim_id=claim.claim_id,
        reason=api_reason,
    )

    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.MCO_PORTAL_AUTH_CHECK,
        success=False,
        needs_human=True,
        human_reason=(
            f"Payer API ({claim.mco.value}) did not return a matching auth. "
            f"Reason: {api_reason}"
        ),
    )


# ---------------------------------------------------------------------------
# Lauris Fax Verification Handler
# ---------------------------------------------------------------------------

async def handle_lauris_fax_verify(
    claim: Claim,
    auth_not_found: bool = False,
) -> ResolutionResult:
    """
    Verify that the SRA fax was actually sent via ANY fax system
    (Lauris fax, Nextiva nmoyern, Nextiva nmoyern2).
    If it was sent: create refax package + refax to MCO.
    If it was never sent: flag for human to resend.
    """
    logger.info("Verifying fax for auth (all sources)", claim_id=claim.claim_id)

    # Check fax_log DB first (covers ALL sources: Lauris + both Nextiva accounts)
    was_sent = False
    send_date = ""
    fax_id = ""
    fax_source = ""
    try:
        from actions.fax_tracker import get_sent_fax_for_client
        fax_entries = get_sent_fax_for_client(
            client_name=claim.client_name,
            mco=claim.mco.value if claim.mco else None,
            after_date=claim.dos,
        )
        if fax_entries:
            entry = fax_entries[0]
            was_sent = True
            send_date = entry.get("fax_date", "")
            fax_id = entry.get("fax_id", "")
            fax_source = entry.get("source", "")
            logger.info(
                "Fax found in fax_log",
                claim_id=claim.claim_id,
                source=fax_source,
                date=send_date,
            )
    except Exception as e:
        logger.warning("Fax log check failed", error=str(e))

    # Fallback: live Lauris check if fax_log had nothing
    if not was_sent:
        try:
            async with LaurisSession() as lauris:
                was_sent, send_date, fax_id = await lauris.check_fax_status(
                    claim.client_name, claim.dos
                )
                if was_sent:
                    fax_source = "lauris_live"
        except Exception as e:
            logger.warning("Live Lauris fax check failed", error=str(e))

    if was_sent and send_date:
        # Auth fax was sent — attempt refax via Nextiva
        mco_fax = MCO_AUTH_FAX_NUMBERS.get(claim.mco, "")
        if mco_fax:
            from actions.fax_refax import execute_refax_workflow
            success, confirm_id = await execute_refax_workflow(
                claim=claim,
                original_send_date=send_date,
                confirmation_path=None,
                sra_pdf=None,
                mco_fax_number=mco_fax,
            )
            if success:
                note = note_auth_not_found_fax_sent(
                    claim.mco.value, send_date, fax_id=fax_id
                )
                return ResolutionResult(
                    claim=claim,
                    action_taken=ResolutionAction.LAURIS_FAX_VERIFY,
                    success=True,
                    note_written=(
                        f"{note} Refax sent via Nextiva "
                        f"(confirm: {confirm_id}). "
                        f"Source: {fax_source}."
                    ),
                )

        # Fallback: no fax number or refax failed
        note = note_auth_not_found_fax_sent(claim.mco.value, send_date)
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.LAURIS_FAX_VERIFY,
            success=True,
            note_written=note,
            needs_human=True,
            human_reason=(
                f"Fax found (sent {send_date}, source: {fax_source}). "
                f"Auto-refax {'failed' if mco_fax else 'not available'}. "
                f"Manual refax needed."
            ),
        )
    else:
            # Fax was never sent — auth was never submitted
            # Create ClickUp task due in 1 business day to alert team
            try:
                from actions.clickup_tasks import (
                    ClickUpTaskCreator, _next_business_day,
                    PRIORITY_HIGH,
                )
                tc = ClickUpTaskCreator()
                today_str = date.today().strftime("%m/%d/%y")
                from actions.clickup_tasks import get_assignees
                await tc.create_task(
                    list_id=tc.list_id,
                    name=(
                        f"Auth Never Submitted — "
                        f"{claim.client_name} [{today_str}]"
                    ),
                    description=(
                        f"Authorization was never submitted.\n\n"
                        f"{_client_info_block(claim)}\n\n"
                        f"No portal submission, no Dropbox record, "
                        f"no fax record found.\n\n"
                        f"Action: Submit initial SRA to "
                        f"{claim.mco.value} immediately.\n\n"
                        f"Generated by Claims Automation on "
                        f"{today_str}."
                    ),
                    assignees=get_assignees(),
                    due_date=_next_business_day(),
                    priority=PRIORITY_HIGH,
                )
                logger.info(
                    "ClickUp task created for auth never submitted",
                    claim_id=claim.claim_id,
                )
            except Exception as e:
                logger.error(
                    "Failed to create auth-never-submitted ClickUp",
                    claim_id=claim.claim_id, error=str(e),
                )

            return ResolutionResult(
                claim=claim,
                action_taken=ResolutionAction.LAURIS_FAX_VERIFY,
                success=False,
                needs_human=True,
                human_reason=(
                    f"No fax record found for "
                    f"{claim.client_name} around DOS {claim.dos}. "
                    f"Auth never submitted. ClickUp task created "
                    f"(due 1 business day)."
                ),
            )


async def _fax_was_received(lauris: LaurisSession, fax_id: str) -> bool:
    """Check if a fax was successfully delivered (not just sent)."""
    try:
        status_col = await lauris.page.query_selector(f"tr:has-text('{fax_id}') td:last-child")
        if status_col:
            text = (await status_col.inner_text()).lower()
            return "delivered" in text or "success" in text
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Lauris Fix — Billing Company (KJLN/NHCS mismatch)
# ---------------------------------------------------------------------------

async def handle_lauris_fix_company(claim: Claim) -> ResolutionResult:
    """
    Fix the billing company in Lauris facesheet to match MCO approval letter.
    Then reprocess the claim.
    """
    if await _is_claim_already_resolved(claim.claim_id):
        return ResolutionResult(
            claim=claim, action_taken=ResolutionAction.SKIP, success=True,
            note_written="Skipped — claim already accepted/paid",
        )
    correct_company = _infer_correct_billing_region(claim)
    logger.info(
        "Fixing billing company",
        claim_id=claim.claim_id,
        client=claim.client_name,
        company=correct_company,
    )

    async with LaurisSession() as lauris:
        success = await lauris.fix_billing_company(
            claim.client_name, claim.client_id, correct_company
        )

    note = note_billing_company_fixed(claim.billing_region or "unknown", correct_company)

    if success:
        # After fixing, correct and resubmit
        claim.billing_region = correct_company
        resubmit_result = await handle_correct_and_resubmit(claim)
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.LAURIS_FIX_COMPANY,
            success=resubmit_result.success,
            note_written=note,
        )

    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.LAURIS_FIX_COMPANY,
        success=False,
        needs_human=True,
        human_reason=f"Could not auto-fix billing company for {claim.client_name}",
    )


# ---------------------------------------------------------------------------
# Write-off Handler
# ---------------------------------------------------------------------------

# Module-level accumulator for non-RRR write-offs.
# Flushed weekly via flush_writeoff_approval_queue().
_writeoff_approval_queue: list = []


async def handle_write_off(claim: Claim, reason: str = "") -> ResolutionResult:
    """
    Write off a claim in both Lauris and add note to Claim.MD.
    For non-RRR write-offs, queue for weekly Desiree approval.
    """
    if await _is_claim_already_resolved(claim.claim_id):
        return ResolutionResult(
            claim=claim, action_taken=ResolutionAction.SKIP, success=True,
            note_written="Skipped — claim already accepted/paid",
        )
    if not reason:
        if DenialCode.RURAL_RATE_REDUCTION in claim.denial_codes:
            # March 2026: RRR write-off only for NHCS <= $19.80
            reason = (
                f"Rural Rate Reduction — NHCS provider, amount ${claim.billed_amount:.2f} "
                f"(within $19.80 threshold). Write off per standard process."
            )
        elif DenialCode.TIMELY_FILING in claim.denial_codes:
            reason = "Timely filing limit exceeded — unable to recover after resubmission and reconsideration"
        else:
            reason = "Reimbursement confirmed unrecoverable"

    logger.info("Writing off claim", claim_id=claim.claim_id, reason=reason)

    lauris_success = claimmd_success = False

    async with LaurisSession() as lauris:
        lauris_success = await lauris.write_off_claim(claim, reason)

    # Write note via API if available
    api = ClaimMDAPI()
    note = note_write_off(reason, amount=claim.billed_amount)
    if api.key:
        claimmd_success = await api.add_claim_note(claim.claim_id, note)
    else:
        async with ClaimMDSession() as claimmd:
            claimmd_success = await claimmd.write_claimmd_writeoff_note(claim, reason)

    # For non-RRR write-offs, queue for weekly Desiree approval
    is_rrr = DenialCode.RURAL_RATE_REDUCTION in claim.denial_codes
    if not is_rrr:
        _writeoff_approval_queue.append({
            "claim_id": claim.claim_id,
            "client_name": claim.client_name,
            "client_id": claim.client_id,
            "mco": claim.mco.value,
            "amount": claim.billed_amount,
            "reason": reason[:120],
            "dos": str(claim.dos),
        })

    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.WRITE_OFF,
        success=lauris_success and claimmd_success,
        note_written=note,
    )


async def flush_writeoff_approval_queue():
    """
    Create ONE weekly ClickUp task to Desiree for non-RRR write-offs.
    Includes spreadsheet-style data of what was attempted. Due in 3
    business days.
    """
    if not _writeoff_approval_queue:
        return

    from actions.clickup_tasks import (
        ClickUpTaskCreator, _next_business_day, PRIORITY_HIGH,
    )
    from datetime import timedelta

    today = date.today()
    today_str = today.strftime("%m/%d/%y")

    # Calculate 3 business days
    due = today
    days_added = 0
    while days_added < 3:
        due += timedelta(days=1)
        if due.weekday() < 5:
            days_added += 1
    from datetime import datetime as dt
    due_dt = dt(due.year, due.month, due.day, 17, 0, 0)

    total_amount = sum(item["amount"] for item in _writeoff_approval_queue)

    # Build spreadsheet-style data
    header = (
        f"{'Claim ID':<15} {'Client':<20} {'Lauris ID':<12} {'Member ID':<15} {'MCO':<12} "
        f"{'Amount':>10} {'DOS':<12} Reason"
    )
    rows = []
    for item in _writeoff_approval_queue:
        rows.append(
            f"{item['claim_id']:<15} "
            f"{item['client_name'][:18]:<20} "
            f"{item.get('lauris_id', ''):<12} "
            f"{item.get('client_id', ''):<15} "
            f"{item['mco']:<12} "
            f"${item['amount']:>9,.2f} "
            f"{item['dos']:<12} "
            f"{item['reason'][:40]}"
        )

    from actions.clickup_tasks import get_assignees
    from actions.clickup_poller import store_task_metadata
    tc = ClickUpTaskCreator()
    wo_task_id = await tc.create_task(
        list_id=tc.list_id,
        name=(
            f"Write-Off Approval — "
            f"{len(_writeoff_approval_queue)} claims, "
            f"${total_amount:,.2f} [{today_str}]"
        ),
        description=(
            f"Weekly write-off approval request for Desiree.\n\n"
            f"{len(_writeoff_approval_queue)} non-RRR claims "
            f"totaling ${total_amount:,.2f}.\n\n"
            f"{header}\n"
            f"{'-' * 80}\n"
            + "\n".join(rows) + "\n\n"
            f"All automated resolution steps were exhausted for "
            f"these claims before write-off.\n\n"
            f"Please approve or deny by commenting on this task.\n\n"
            f"Generated by Claims Automation on {today_str}."
        ),
        assignees=get_assignees("write_off_approval"),
        due_date=due_dt,
        priority=PRIORITY_HIGH,
    )
    if wo_task_id:
        store_task_metadata(wo_task_id, "write_off_approval")
    logger.info(
        "Weekly write-off approval ClickUp created for Desiree",
        claims=len(_writeoff_approval_queue),
        total=total_amount,
    )
    _writeoff_approval_queue.clear()


# ---------------------------------------------------------------------------
# Reconsideration Handler (Step 2)
# ---------------------------------------------------------------------------

async def handle_duplicate_check(claim: Claim) -> bool:
    """
    For duplicate claim denials, check if the DOS was actually paid
    before submitting reconsideration. Returns True if it's a real duplicate.
    """
    api = ClaimMDAPI()
    if not api.key:
        return False

    try:
        # Get all responses for this claim to check payment history
        responses = await api.get_claim_responses(response_id="0")
        for resp in responses:
            if (resp.get("ins_number") == claim.client_id
                    and resp.get("fdos") == claim.dos.strftime("%Y-%m-%d")
                    and resp.get("status") == "A"):
                # Found an accepted claim for same member + DOS
                logger.info("Duplicate confirmed — DOS was paid",
                            claim_id=claim.claim_id,
                            paid_claim=resp.get("claimmd_id"))
                return True
    except Exception as e:
        logger.warning("Duplicate check failed", error=str(e))

    return False


async def handle_reconsideration(claim: Claim) -> ResolutionResult:
    """Submit reconsideration. United uses TrackIt; all others use Claim.MD.
    Includes DMAS language letter explaining why they should pay.
    Verifies authorization PDF exists before submitting.
    """
    if await _is_claim_already_resolved(claim.claim_id):
        return ResolutionResult(
            claim=claim, action_taken=ResolutionAction.SKIP, success=True,
            note_written="Skipped — claim already accepted/paid",
        )
    logger.info("Submitting reconsideration",
                claim_id=claim.claim_id, mco=claim.mco.value)

    # For duplicate denials, verify it's not actually a duplicate before recon
    if DenialCode.DUPLICATE in claim.denial_codes:
        is_real_dup = await handle_duplicate_check(claim)
        if is_real_dup:
            return await handle_write_off(
                claim,
                reason=(
                    "Confirmed duplicate — DOS was paid on a "
                    "prior claim"
                ),
            )

    # Verify authorization PDF exists before submitting recon
    auth_pdf_path = str(
        WORK_DIR / f"auth_{claim.claim_id}.pdf"
    )
    auth_pdf_exists = Path(auth_pdf_path).exists()
    if not auth_pdf_exists:
        # Check Dropbox for the auth PDF
        try:
            from actions.dropbox_verify import verify_dropbox_auth
            dropbox_result = await verify_dropbox_auth(
                claim.client_name, claim.mco.value, claim.claim_id
            )
            if dropbox_result.get("found"):
                auth_pdf_exists = True
                auth_pdf_path = dropbox_result.get("path", "")
        except Exception:
            pass

    if not auth_pdf_exists:
        logger.warning(
            "Authorization PDF not found — flagging for human review "
            "instead of submitting recon without docs",
            claim_id=claim.claim_id,
        )
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.RECONSIDERATION,
            success=False,
            needs_human=True,
            human_reason=(
                "Reconsideration not submitted — authorization PDF "
                "not found. Locate the auth document before "
                "submitting reconsideration."
            ),
        )

    # Build DMAS language reconsideration reason
    from notes.formatter import get_recon_reason
    denial_key = "unknown"
    if claim.denial_codes:
        denial_key = claim.denial_codes[0].value
    recon_letter_text = get_recon_reason(
        denial_key, claim.mco.value
    )

    if claim.mco == MCO.UNITED:
        checker = get_auth_checker(MCO.UNITED)
        async with checker as portal:
            auth_pdf = str(WORK_DIR / f"auth_{claim.claim_id}.pdf")
            # Auth PDF would be downloaded from portal — placeholder path
            success = await portal.submit_reconsideration_trackit(claim, auth_pdf)
        if success:
            log_autonomous_correction(
                claim_id=claim.claim_id,
                client_name=claim.client_name,
                client_id=claim.client_id,
                correction_type="reconsideration_submitted",
                correction_detail="United reconsideration submitted via TrackIt",
                dollars_at_stake=claim.billed_amount,
            )
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.RECONSIDERATION,
            success=success,
            note_written=f"United reconsideration submitted via TrackIt.",
        )

    # Use API for appeal submission if available
    api = ClaimMDAPI()
    if api.key:
        success = await api.submit_appeal(claim.claim_id, {
            "AppealType": "reconsideration",
            "ReconReason": recon_letter_text,
        })
        if success:
            note = note_reconsideration_submitted(claim.mco.value)
            await api.add_claim_note(claim.claim_id, note)
    else:
        async with ClaimMDSession() as claimmd:
            success = await claimmd.submit_reconsideration(claim)

    # Log autonomous correction if reconsideration submitted successfully
    if success:
        log_autonomous_correction(
            claim_id=claim.claim_id,
            client_name=claim.client_name,
            client_id=claim.client_id,
            correction_type="reconsideration_submitted",
            correction_detail=(
                f"Reconsideration submitted to {claim.mco.value}. "
                f"DMAS language: {recon_letter_text[:200]}"
            ),
            dollars_at_stake=claim.billed_amount,
        )

    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.RECONSIDERATION,
        success=success,
        note_written=(
            f"Reconsideration submitted to {claim.mco.value}. "
            f"DMAS language included: {recon_letter_text[:80]}"
        ),
    )


# ---------------------------------------------------------------------------
# Appeal Handler (Step 3)
# ---------------------------------------------------------------------------

async def handle_appeal(claim: Claim) -> ResolutionResult:
    """
    Submit formal appeal. Magellan and DMAS escalations are flagged for human.
    """
    if await _is_claim_already_resolved(claim.claim_id):
        return ResolutionResult(
            claim=claim, action_taken=ResolutionAction.SKIP, success=True,
            note_written="Skipped — claim already accepted/paid",
        )
    logger.info("Submitting appeal (Step 3)", claim_id=claim.claim_id)

    if claim.mco in {MCO.MAGELLAN, MCO.DMAS}:
        return ResolutionResult(
            claim=claim,
            action_taken=ResolutionAction.APPEAL_STEP3,
            success=False,
            needs_human=True,
            human_reason=(
                f"{claim.mco.value} appeal requires manual DMAS/Magellan process. "
                "See Admin Manual: Claim Appeals Step 3."
            ),
        )

    async with ClaimMDSession() as claimmd:
        success = await claimmd.submit_appeal(claim)

    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.APPEAL_STEP3,
        success=success,
        note_written=f"Appeal submitted to {claim.mco.value}.",
    )


# ---------------------------------------------------------------------------
# Phone call flag (Thursday task)
# ---------------------------------------------------------------------------

# Module-level accumulator for phone call claims, to create ONE
# consolidated ClickUp task (not one per claim).  Flushed via
# flush_phone_call_queue() at end of daily run.
_phone_call_queue: list = []
_PHONE_CALL_COOLDOWN_DAYS = 14


async def handle_phone_call_flag(claim: Claim) -> ResolutionResult:
    """
    Accumulate claim for consolidated MCO call ClickUp task.
    Claims are grouped by patient/client. Includes MCO denial reason
    and resolution history for each. Waits 14 days before re-adding.
    """
    from datetime import timedelta

    # Check 14-day cooldown — skip if recently queued
    if claim.last_followup:
        days_since = (date.today() - claim.last_followup).days
        if days_since < _PHONE_CALL_COOLDOWN_DAYS:
            return ResolutionResult(
                claim=claim,
                action_taken=ResolutionAction.PHONE_CALL_THURSDAY,
                success=True,
                note_written=(
                    f"Phone call cooldown — last queued "
                    f"{days_since} days ago (14-day wait)."
                ),
            )

    # Find next Thursday
    today = date.today()
    days_until_thursday = (3 - today.weekday()) % 7
    next_thursday = today + timedelta(days=days_until_thursday or 7)

    note = (
        f"Queued for MCO call follow-up. "
        f"Call scheduled for {next_thursday.strftime('%m/%d/%y')}. "
        f"Reason: {claim.denial_reason_raw[:80]}"
    )
    from notes.formatter import format_note
    formatted_note = format_note(note)

    api = ClaimMDAPI()
    if api.key:
        await api.add_claim_note(claim.claim_id, formatted_note)

    _phone_call_queue.append({
        "claim_id": claim.claim_id,
        "client_name": claim.client_name,
        "client_id": claim.client_id,
        "mco": claim.mco.value,
        "denial_reason": claim.denial_reason_raw[:120],
        "amount": claim.billed_amount,
        "dos": str(claim.dos),
    })

    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.PHONE_CALL_THURSDAY,
        success=True,
        note_written=formatted_note,
    )


async def flush_phone_call_queue():
    """
    Create ONE consolidated ClickUp task for all queued phone calls,
    grouped by patient/client.
    """
    if not _phone_call_queue:
        return

    from actions.clickup_tasks import (
        ClickUpTaskCreator, _next_business_day, PRIORITY_NORMAL,
    )
    from datetime import timedelta

    today = date.today()
    days_until_thursday = (3 - today.weekday()) % 7
    next_thursday = today + timedelta(days=days_until_thursday or 7)
    today_str = today.strftime("%m/%d/%y")

    # Group by client
    by_client: dict = {}
    for item in _phone_call_queue:
        key = item["client_name"]
        by_client.setdefault(key, []).append(item)

    lines = []
    for client, claims in by_client.items():
        lauris_id = claims[0].get('lauris_id', '') if claims else ''
        id_str = f" ({lauris_id})" if lauris_id else ""
        lines.append(f"\n{client}{id_str}:")
        for c in claims:
            lines.append(
                f"  - Claim {c['claim_id']} | "
                f"Lauris ID: {c.get('lauris_id', '')} | "
                f"Member ID: {c.get('client_id', '')} | "
                f"MCO: {c['mco']} | "
                f"DOS: {c['dos']} | "
                f"${c['amount']:.2f} | "
                f"Denial: {c['denial_reason']}"
            )

    body = "\n".join(lines)
    tc = ClickUpTaskCreator()
    from actions.clickup_tasks import get_assignees
    await tc.create_task(
        list_id=tc.list_id,
        name=(
            f"MCO Call List — {len(_phone_call_queue)} claims "
            f"[{today_str}]"
        ),
        description=(
            f"Consolidated phone call list for "
            f"{next_thursday.strftime('%m/%d/%y')}.\n\n"
            f"{len(_phone_call_queue)} claims across "
            f"{len(by_client)} clients:\n"
            f"{body}\n\n"
            f"Generated by Claims Automation on {today_str}."
        ),
        assignees=get_assignees(),
        due_date=_next_business_day(),
        priority=PRIORITY_NORMAL,
    )
    logger.info(
        "Consolidated phone call ClickUp created",
        claims=len(_phone_call_queue),
        clients=len(by_client),
    )
    _phone_call_queue.clear()
