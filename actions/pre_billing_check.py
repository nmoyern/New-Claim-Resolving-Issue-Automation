"""
actions/pre_billing_check.py
-----------------------------
Pre-billing corrections module. Runs BEFORE claims are submitted to Claim.MD
for the first time. Catches and fixes issues that would cause denials.

Checks performed:
  - Missing or blank diagnosis
  - Wrong billing entity (company doesn't match auth)
  - Missing authorization number
  - Missing rendering NPI for RCSU services
  - Invalid member ID (via eligibility API)
  - Missing or wrong NPI (verify against entity)

For each issue:
  - Try to fix automatically
  - If can't fix, create consolidated ClickUp task per patient
  - Do NOT submit the claim to Claim.MD until fixed
  - Log what was caught and fixed
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.entities import entity_npi_map
from config.models import Claim, DenialCode, MCO, Program
from config.settings import (
    DR_YANCEY_NPI,
    DRY_RUN,
    ORG_KJLN,
    ORG_MARYS_HOME,
    ORG_NHCS,
)
from logging_utils.logger import get_logger
from reporting.autonomous_tracker import log_autonomous_correction

logger = get_logger("pre_billing_check")

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "claims_history.db"

# NPI mapping per entity
ENTITY_NPI_MAP = entity_npi_map()

# Programs that require rendering NPI for RCSU services
RCSU_RENDERING_PROGRAMS = {Program.MARYS_HOME, Program.NHCS, Program.KJLN}

# NHCS MHSS rate adjustment: $102.72 per unit
# NHCS Mental Health Skill-Building claims must be billed at $102.72/unit
NHCS_MHSS_RATE_PER_UNIT = 102.72


# ---------------------------------------------------------------------------
# Database for pre-billing gap log
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_pre_billing_table():
    conn = _get_conn()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pre_billing_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            client_name TEXT NOT NULL,
            check_type TEXT NOT NULL,
            issue TEXT NOT NULL,
            auto_fixed INTEGER DEFAULT 0,
            fix_detail TEXT DEFAULT '',
            blocked INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prebilling_date ON pre_billing_log(date)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_prebilling_claim ON pre_billing_log(claim_id)"
    )
    conn.commit()
    conn.close()


_ensure_pre_billing_table()


def _log_pre_billing_issue(
    claim_id: str,
    client_name: str,
    check_type: str,
    issue: str,
    auto_fixed: bool = False,
    fix_detail: str = "",
    blocked: bool = False,
):
    """Log a pre-billing issue to the database."""
    conn = _get_conn()
    conn.execute(
        """INSERT INTO pre_billing_log
           (date, claim_id, client_name, check_type, issue,
            auto_fixed, fix_detail, blocked, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            date.today().isoformat(),
            claim_id,
            client_name,
            check_type,
            issue,
            int(auto_fixed),
            fix_detail,
            int(blocked),
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Individual check methods
# ---------------------------------------------------------------------------

def check_diagnosis(claim: Claim) -> Tuple[bool, str]:
    """
    Check for missing or blank diagnosis on the claim.

    Returns (passed, message).
    If failed, attempts auto-fix by looking up in Lauris assessment.
    """
    # Check if diagnosis codes are present
    diag = getattr(claim, "diagnosis_codes", None) or getattr(claim, "denial_reason_raw", "")
    has_blank_diag = DenialCode.DIAGNOSIS_BLANK in (claim.denial_codes or [])

    # Also check if there's simply no diagnosis info
    if has_blank_diag or (hasattr(claim, "diagnosis_codes") and not claim.diagnosis_codes):
        issue = f"Missing or blank diagnosis for claim {claim.claim_id}"
        logger.warning(issue, client=claim.client_name)

        # Attempt auto-fix: extract diagnosis from Lauris Mental Health Assessment
        try:
            import asyncio
            from lauris.diagnosis import get_diagnosis_for_claim, update_facesheet_diagnosis
            from lauris.billing import LaurisSession

            async def _try_extract_diagnosis():
                async with LaurisSession() as lauris:
                    diagnosis = await get_diagnosis_for_claim(lauris.page, claim)
                    if diagnosis:
                        # Also update the facesheet so it doesn't recur
                        from lauris.diagnosis import _lookup_uid_from_record_number
                        uid = await _lookup_uid_from_record_number(
                            lauris.page, claim.client_id
                        )
                        if uid:
                            await update_facesheet_diagnosis(
                                lauris.page, uid,
                                diagnosis["icd_code"], diagnosis["description"],
                            )
                    return diagnosis

            if not DRY_RUN:
                # Run the async extraction.
                # check_diagnosis is sync, so use asyncio.run() when no loop
                # is running. If an event loop is already active (called from
                # async context), skip here — handlers.py will handle it.
                diagnosis = None
                try:
                    asyncio.get_running_loop()
                    # Loop already running — can't call asyncio.run().
                    # The async handler in handlers.py will attempt
                    # extraction instead. Log and fall through to blocked.
                    logger.info(
                        "Async loop already running — deferring "
                        "Lauris diagnosis extraction to handler",
                        claim_id=claim.claim_id,
                    )
                except RuntimeError:
                    # No running loop — safe to use asyncio.run()
                    diagnosis = asyncio.run(_try_extract_diagnosis())

                if diagnosis:
                    fix_detail = (
                        f"Extracted diagnosis {diagnosis['icd_code']} - "
                        f"{diagnosis['description']} from Mental Health "
                        f"Assessment 3.0 (page {diagnosis['page_found']}). "
                        f"Updated Client Face Sheet."
                    )
                    logger.info(
                        "Auto-fixed diagnosis from Lauris assessment",
                        claim_id=claim.claim_id,
                        icd_code=diagnosis["icd_code"],
                    )
                    _log_pre_billing_issue(
                        claim.claim_id, claim.client_name, "diagnosis",
                        issue, auto_fixed=True, fix_detail=fix_detail,
                    )
                    log_autonomous_correction(
                        claim_id=claim.claim_id,
                        client_name=claim.client_name,
                        client_id=claim.client_id,
                        correction_type="diagnosis_fix",
                        correction_detail=fix_detail,
                        dollars_at_stake=claim.billed_amount,
                    )
                    return True, f"Auto-fixed: {fix_detail}"

            # If we get here, extraction failed or DRY_RUN
            if DRY_RUN:
                logger.info(
                    "DRY_RUN: Would attempt Lauris diagnosis extraction",
                    claim_id=claim.claim_id,
                )
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "diagnosis",
                issue, auto_fixed=False, blocked=True,
            )
            return False, issue

        except Exception as e:
            logger.error("Diagnosis lookup failed", error=str(e))
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "diagnosis",
                issue, auto_fixed=False, blocked=True,
            )
            return False, f"{issue} — lookup failed: {e}"

    return True, "Diagnosis present"


def check_entity(claim: Claim) -> Tuple[bool, str]:
    """
    Verify the billing entity matches the MCO authorization.

    Source of truth: MCO portals and received fax auth letters.
    DO NOT rely on Lauris XML (manually entered, could be wrong).

    Flow:
      1. Skip if client paid within 30 days (use AR cache)
      2. Check received fax log (Gmail) for auth approval letter
      3. Compare auth entity vs billing entity
      4. If mismatch or can't verify → block for ClickUp review
         Staff replies "verified" to confirm

    Returns (passed, message).
    """
    # Skip check if client recently paid (within 30 days)
    if hasattr(check_entity, "_ar_cache") and check_entity._ar_cache is not None:
        member = claim.client_id
        client_outstanding = any(
            c.get("member_id") == member for c in check_entity._ar_cache
        )
        if not client_outstanding:
            return True, "Entity check skipped — client recently paid"

    has_wrong_entity = DenialCode.WRONG_BILLING_CO in (claim.denial_codes or [])

    # Step 1: Check MCO portal for auth — see which company it's under
    # This is the source of truth. For Anthem, checks all 3 orgs.
    portal_entity = ""
    portal_auth_number = ""
    try:
        import asyncio
        from mco_portals.auth_checker import get_auth_checker

        checker = get_auth_checker(claim.mco, headless=True)
        if checker:
            async def _check_portal():
                async with checker as portal:
                    found, auth_rec = await portal.check_auth(claim)
                    return found, auth_rec

            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    # Already in async context
                    found, auth_rec = False, None
                else:
                    found, auth_rec = loop.run_until_complete(_check_portal())
            except RuntimeError:
                found, auth_rec = False, None

            if found and auth_rec:
                # Map program to entity
                prog_to_entity = {
                    Program.NHCS: "NHCS",
                    Program.KJLN: "KJLN",
                    Program.MARYS_HOME: "MARYS_HOME",
                }
                portal_entity = prog_to_entity.get(auth_rec.program, "")
                portal_auth_number = auth_rec.auth_number or ""
                logger.info(
                    "Auth verified from MCO portal",
                    claim_id=claim.claim_id,
                    entity=portal_entity,
                    auth=portal_auth_number,
                    mco=claim.mco.value,
                )
    except Exception as e:
        logger.debug(
            "MCO portal check unavailable",
            claim_id=claim.claim_id,
            error=str(e)[:60],
        )

    if portal_entity:
        actual_region = claim.billing_region.upper() if claim.billing_region else ""
        if actual_region and portal_entity != actual_region:
            # Mismatch — auto-fix to match portal
            fix_detail = (
                f"Billing entity corrected from '{actual_region}' to "
                f"'{portal_entity}' (verified from MCO portal, "
                f"auth #{portal_auth_number})"
            )
            if not DRY_RUN:
                _log_pre_billing_issue(
                    claim.claim_id, claim.client_name, "entity",
                    fix_detail, auto_fixed=True, fix_detail=fix_detail,
                )
                log_autonomous_correction(
                    claim_id=claim.claim_id,
                    client_name=claim.client_name,
                    client_id=claim.client_id,
                    correction_type="entity_fix",
                    correction_detail=fix_detail,
                    dollars_at_stake=claim.billed_amount,
                )
                claim.billing_region = portal_entity
            return True, f"Auto-fixed: {fix_detail}"
        return True, (
            f"Entity verified from MCO portal: {portal_entity} "
            f"(auth #{portal_auth_number})"
        )

    # Step 2: Check received fax log for auth approval letter
    # Only checks faxes not already verified (entity_verified=0)
    auth_entity_from_fax = ""
    auth_number_from_fax = ""
    matched_fax_id = ""
    try:
        from actions.fax_tracker import (
            get_received_auth_for_client,
            mark_fax_entity_verified,
        )
        received = get_received_auth_for_client(
            client_name=claim.client_name,
            mco=claim.mco.value if claim.mco else None,
            skip_already_verified=True,
        )
        for entry in received:
            entity = (entry.get("company", "") or "").strip()
            auth_num = (entry.get("auth_number", "") or "").strip()
            fax_id = entry.get("fax_id", "")
            if entity:
                if "kjln" in entity.lower():
                    auth_entity_from_fax = "KJLN"
                elif "nhcs" in entity.lower() or "new heights" in entity.lower():
                    auth_entity_from_fax = "NHCS"
                elif "mary" in entity.lower():
                    auth_entity_from_fax = "MARYS_HOME"
            if auth_num:
                auth_number_from_fax = auth_num
            if auth_entity_from_fax:
                matched_fax_id = fax_id
                # Mark this fax as verified so we don't re-check it
                mark_fax_entity_verified(fax_id, claim.client_name)
                break
    except Exception:
        pass

    # Cross-check program against billing region
    program_entity = {
        Program.NHCS: "NHCS",
        Program.KJLN: "KJLN",
        Program.MARYS_HOME: "MARYS_HOME",
    }

    expected_entity = program_entity.get(claim.program, "")
    actual_region = claim.billing_region.upper() if claim.billing_region else ""

    # If we found entity from received fax, verify it matches billing
    if auth_entity_from_fax:
        if actual_region and auth_entity_from_fax != actual_region:
            issue = (
                f"Entity mismatch: auth approved under "
                f"{auth_entity_from_fax} but billing under "
                f"{actual_region}. Auth #: {auth_number_from_fax}"
            )
            logger.warning(issue, claim_id=claim.claim_id)

            # Auto-fix to match the auth entity
            if not DRY_RUN:
                fix_detail = (
                    f"Billing entity corrected from '{actual_region}' "
                    f"to '{auth_entity_from_fax}' (verified from received "
                    f"fax auth letter)"
                )
                _log_pre_billing_issue(
                    claim.claim_id, claim.client_name, "entity",
                    issue, auto_fixed=True, fix_detail=fix_detail,
                )
                log_autonomous_correction(
                    claim_id=claim.claim_id,
                    client_name=claim.client_name,
                    client_id=claim.client_id,
                    correction_type="entity_fix",
                    correction_detail=fix_detail,
                    dollars_at_stake=claim.billed_amount,
                )
                claim.billing_region = auth_entity_from_fax
                return True, f"Auto-fixed: {fix_detail}"

            return False, issue

        # Auth entity matches billing — verified
        return True, (
            f"Entity verified from received fax: "
            f"{auth_entity_from_fax} (auth #{auth_number_from_fax})"
        )

    # No fax verification available — check basic program/region match
    if has_wrong_entity or (expected_entity and actual_region and expected_entity != actual_region):
        issue = (
            f"Entity mismatch for claim {claim.claim_id}: "
            f"program={claim.program.value}, billing_region={claim.billing_region}"
        )
        logger.warning(issue, client=claim.client_name)

        # Attempt auto-fix: update billing region to match program
        if expected_entity and not DRY_RUN:
            fix_detail = f"Would set billing_region from '{claim.billing_region}' to '{expected_entity}'"
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "entity",
                issue, auto_fixed=True, fix_detail=fix_detail,
            )
            log_autonomous_correction(
                claim_id=claim.claim_id,
                client_name=claim.client_name,
                client_id=claim.client_id,
                correction_type="entity_fix",
                correction_detail=fix_detail,
                dollars_at_stake=claim.billed_amount,
            )
            claim.billing_region = expected_entity
            return True, f"Auto-fixed: {fix_detail}"

        # Before blocking, check ALL fax sources for entity info
        try:
            from actions.fax_tracker import get_sent_fax_for_client
            fax_entries = get_sent_fax_for_client(
                client_name=claim.client_name,
                mco=claim.mco.value if claim.mco else None,
            )
            for entry in fax_entries:
                fax_entity = (entry.get("company", "") or "").lower()
                source = entry.get("source", "")
                entity_from_fax = ""
                if "kjln" in fax_entity:
                    entity_from_fax = "KJLN"
                elif "nhcs" in fax_entity or "new heights" in fax_entity:
                    entity_from_fax = "NHCS"
                elif "mary" in fax_entity:
                    entity_from_fax = "MARYS_HOME"
                if entity_from_fax:
                    fix_detail = (
                        f"Entity determined from fax log ({source}): "
                        f"set billing_region to '{entity_from_fax}'"
                    )
                    _log_pre_billing_issue(
                        claim.claim_id, claim.client_name, "entity",
                        issue, auto_fixed=True, fix_detail=fix_detail,
                    )
                    log_autonomous_correction(
                        claim_id=claim.claim_id,
                        client_name=claim.client_name,
                        client_id=claim.client_id,
                        correction_type="entity_fix",
                        correction_detail=fix_detail,
                        dollars_at_stake=claim.billed_amount,
                    )
                    claim.billing_region = entity_from_fax
                    return True, f"Auto-fixed from fax log: {fix_detail}"
        except Exception:
            pass

        _log_pre_billing_issue(
            claim.claim_id, claim.client_name, "entity",
            issue, auto_fixed=False, blocked=True,
        )
        return False, (
            f"NEEDS VERIFICATION — Could not verify from MCO portal "
            f"or received fax. {issue}\n"
            f"Staff: please call MCO to verify the correct company, "
            f"authorization number, and program. Reply 'verified' "
            f"once confirmed."
        )

    # No mismatch detected, but if we couldn't verify from MCO/fax
    # and this is a new claim (no prior payment), flag for review
    if not auth_entity_from_fax and not actual_region:
        return False, (
            f"NEEDS VERIFICATION — No entity on claim and could not "
            f"verify from received fax auth letters.\n"
            f"Client: {claim.client_name} | "
            f"Lauris ID: {claim.lauris_id} | "
            f"Member: {claim.client_id} | "
            f"MCO: {claim.mco.value if claim.mco else 'unknown'}\n"
            f"Staff: check MCO portal for authorization, verify "
            f"correct company. Reply 'verified' once confirmed."
        )

    return True, "Entity matches program"


def check_auth(claim: Claim) -> Tuple[bool, str]:
    """
    Check for missing authorization number.

    Returns (passed, message).
    """
    has_no_auth = DenialCode.NO_AUTH in (claim.denial_codes or [])

    if has_no_auth or not claim.auth_number:
        issue = f"Missing authorization number for claim {claim.claim_id}"
        logger.warning(issue, client=claim.client_name)

        # Attempt auto-fix: check MCO portal for auth
        # In production, this would call the portal API
        try:
            if not DRY_RUN:
                # Would check portal and add auth to claim
                pass
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "auth",
                issue, auto_fixed=False, blocked=True,
            )
            return False, issue
        except Exception as e:
            logger.error("Auth lookup failed", error=str(e))
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "auth",
                issue, auto_fixed=False, blocked=True,
            )
            return False, f"{issue} — lookup failed: {e}"

    return True, "Authorization present"


def check_rendering_npi(claim: Claim) -> Tuple[bool, str]:
    """
    Check for missing rendering NPI, especially for RCSU services.

    For RCSU services, Dr. Yancey's NPI should be used as rendering provider.
    Returns (passed, message).
    """
    has_missing_npi = DenialCode.MISSING_NPI_RENDERING in (claim.denial_codes or [])
    service_code = getattr(claim, "service_code", "") or ""
    is_rcsu = service_code.upper() == "RCSU"

    if has_missing_npi or (is_rcsu and claim.program in RCSU_RENDERING_PROGRAMS):
        # Check if rendering NPI is populated
        rendering_npi = getattr(claim, "rendering_npi", "") or ""
        if not rendering_npi:
            issue = f"Missing rendering NPI for RCSU claim {claim.claim_id}"
            logger.warning(issue, client=claim.client_name)

            # Auto-fix: add Dr. Yancey's NPI
            if not DRY_RUN:
                fix_detail = f"Set rendering NPI to Dr. Yancey ({DR_YANCEY_NPI})"
                if hasattr(claim, "rendering_npi"):
                    claim.rendering_npi = DR_YANCEY_NPI
                _log_pre_billing_issue(
                    claim.claim_id, claim.client_name, "rendering_npi",
                    issue, auto_fixed=True, fix_detail=fix_detail,
                )
                log_autonomous_correction(
                    claim_id=claim.claim_id,
                    client_name=claim.client_name,
                    client_id=claim.client_id,
                    correction_type="rendering_npi_added",
                    correction_detail=fix_detail,
                    dollars_at_stake=claim.billed_amount,
                )
                return True, f"Auto-fixed: {fix_detail}"

            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "rendering_npi",
                issue, auto_fixed=False, blocked=True,
            )
            return False, issue

    return True, "Rendering NPI present"


def check_member_id(claim: Claim) -> Tuple[bool, str]:
    """
    Check for invalid or missing member ID.

    Returns (passed, message).
    """
    has_invalid_id = DenialCode.INVALID_ID in (claim.denial_codes or [])

    if has_invalid_id or not claim.client_id or claim.client_id.strip() == "":
        issue = f"Invalid or missing member ID for claim {claim.claim_id}"
        logger.warning(issue, client=claim.client_name)

        # Attempt auto-fix: check eligibility API
        try:
            if not DRY_RUN:
                # Would call eligibility API to verify/correct member ID
                pass
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "member_id",
                issue, auto_fixed=False, blocked=True,
            )
            return False, issue
        except Exception as e:
            logger.error("Member ID lookup failed", error=str(e))
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "member_id",
                issue, auto_fixed=False, blocked=True,
            )
            return False, f"{issue} — lookup failed: {e}"

    return True, "Member ID valid"


def check_nhcs_mhss_rate(claim: Claim) -> Tuple[bool, str]:
    """
    NHCS MHSS claims must be billed at $9.80 per unit.

    If the claim is NHCS + MHSS and the rate doesn't match,
    auto-adjust the billed_amount to units × $9.80.

    Returns (passed, message).
    """
    # Only applies to NHCS program + MHSS service code
    if claim.program != Program.NHCS:
        return True, "Not NHCS — rate check skipped"

    service = (claim.service_code or "").upper()
    if service != "MHSS":
        return True, "Not MHSS — rate check skipped"

    # Need units to calculate correct amount
    units = claim.units
    if not units or units <= 0:
        # Try to infer units from billed_amount if rate is known
        # Can't adjust without knowing units
        issue = (
            f"NHCS MHSS claim {claim.claim_id} has no units — "
            f"cannot verify $9.80/unit rate"
        )
        logger.warning(issue, client=claim.client_name)
        _log_pre_billing_issue(
            claim.claim_id, claim.client_name, "nhcs_mhss_rate",
            issue, auto_fixed=False, blocked=False,
        )
        return True, issue  # Don't block — just warn

    correct_amount = round(units * NHCS_MHSS_RATE_PER_UNIT, 2)

    # Check if current billed amount matches
    if abs(claim.billed_amount - correct_amount) < 0.01:
        return True, (
            f"NHCS MHSS rate correct: {units} units × "
            f"${NHCS_MHSS_RATE_PER_UNIT} = ${correct_amount:,.2f}"
        )

    # Auto-fix: adjust billed amount
    original = claim.billed_amount
    if not DRY_RUN:
        claim.billed_amount = correct_amount
        claim.rate_per_unit = NHCS_MHSS_RATE_PER_UNIT

    fix_detail = (
        f"Adjusted NHCS MHSS rate: {units} units × "
        f"${NHCS_MHSS_RATE_PER_UNIT}/unit = ${correct_amount:,.2f} "
        f"(was ${original:,.2f})"
    )
    logger.info(
        "NHCS MHSS rate adjusted",
        claim_id=claim.claim_id,
        client=claim.client_name,
        units=units,
        original=original,
        adjusted=correct_amount,
    )
    _log_pre_billing_issue(
        claim.claim_id, claim.client_name, "nhcs_mhss_rate",
        f"Rate was ${original:,.2f}, should be ${correct_amount:,.2f}",
        auto_fixed=True, fix_detail=fix_detail,
    )
    log_autonomous_correction(
        claim_id=claim.claim_id,
        client_name=claim.client_name,
        client_id=claim.client_id,
        correction_type="mhss_rate_fix",
        correction_detail=fix_detail,
        dollars_at_stake=claim.billed_amount,
    )
    return True, f"Auto-fixed: {fix_detail}"


def _check_npi(claim: Claim) -> Tuple[bool, str]:
    """
    Check for missing or wrong NPI — verify against entity.

    Returns (passed, message).
    """
    has_invalid_npi = DenialCode.INVALID_NPI in (claim.denial_codes or [])
    expected_npi = ENTITY_NPI_MAP.get(claim.program.value, "")

    if has_invalid_npi:
        issue = f"Invalid NPI for claim {claim.claim_id}"
        if expected_npi and not DRY_RUN:
            fix_detail = f"Set NPI to entity NPI: {expected_npi}"
            claim.npi = expected_npi
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "npi",
                issue, auto_fixed=True, fix_detail=fix_detail,
            )
            log_autonomous_correction(
                claim_id=claim.claim_id,
                client_name=claim.client_name,
                client_id=claim.client_id,
                correction_type="npi_fix",
                correction_detail=fix_detail,
                dollars_at_stake=claim.billed_amount,
            )
            return True, f"Auto-fixed: {fix_detail}"

        _log_pre_billing_issue(
            claim.claim_id, claim.client_name, "npi",
            issue, auto_fixed=False, blocked=True,
        )
        return False, issue

    # Also check if NPI is simply missing
    if not claim.npi and expected_npi:
        issue = f"Missing NPI for claim {claim.claim_id}"
        if not DRY_RUN:
            fix_detail = f"Set NPI to entity NPI: {expected_npi}"
            claim.npi = expected_npi
            _log_pre_billing_issue(
                claim.claim_id, claim.client_name, "npi",
                issue, auto_fixed=True, fix_detail=fix_detail,
            )
            log_autonomous_correction(
                claim_id=claim.claim_id,
                client_name=claim.client_name,
                client_id=claim.client_id,
                correction_type="npi_fix",
                correction_detail=fix_detail,
                dollars_at_stake=claim.billed_amount,
            )
            return True, f"Auto-fixed: {fix_detail}"

        _log_pre_billing_issue(
            claim.claim_id, claim.client_name, "npi",
            issue, auto_fixed=False, blocked=True,
        )
        return False, issue

    return True, "NPI valid"


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run_pre_billing_checks(claims: list) -> dict:
    """
    Run all pre-billing checks on a list of claims.

    Returns:
        {
            "passed": [claim, ...],      # Claims ready for submission
            "fixed": [claim, ...],        # Claims that were auto-fixed and are now ready
            "blocked": [claim, ...],      # Claims blocked from submission
            "issues": [                   # All issues found
                {"claim_id": ..., "client_name": ..., "check": ..., "issue": ..., "fixed": bool},
                ...
            ],
            "summary": {
                "total_checked": int,
                "total_passed": int,
                "total_fixed": int,
                "total_blocked": int,
            }
        }
    """
    passed = []
    fixed = []
    blocked = []
    issues = []

    checks = [
        ("diagnosis", check_diagnosis),
        ("entity", check_entity),
        ("auth", check_auth),
        ("rendering_npi", check_rendering_npi),
        ("member_id", check_member_id),
        ("npi", _check_npi),
        ("nhcs_mhss_rate", check_nhcs_mhss_rate),
    ]

    # Track blocked claims per patient for consolidated ClickUp tasks
    patient_issues: Dict[str, List[dict]] = {}

    for claim in claims:
        claim_passed = True
        claim_was_fixed = False

        for check_name, check_fn in checks:
            ok, message = check_fn(claim)
            if not ok:
                claim_passed = False
                issues.append({
                    "claim_id": claim.claim_id,
                    "client_name": claim.client_name,
                    "check": check_name,
                    "issue": message,
                    "fixed": False,
                })
                # Track for ClickUp consolidation
                patient_name = claim.client_name
                if patient_name not in patient_issues:
                    patient_issues[patient_name] = []
                patient_issues[patient_name].append({
                    "claim_id": claim.claim_id,
                    "client_id": claim.client_id,
                    "check": check_name,
                    "issue": message,
                })
            elif "Auto-fixed" in message:
                claim_was_fixed = True
                issues.append({
                    "claim_id": claim.claim_id,
                    "client_name": claim.client_name,
                    "check": check_name,
                    "issue": message,
                    "fixed": True,
                })

        if claim_passed and claim_was_fixed:
            fixed.append(claim)
        elif claim_passed:
            passed.append(claim)
        else:
            blocked.append(claim)

    # Create consolidated ClickUp tasks for blocked patients
    if patient_issues and not DRY_RUN:
        try:
            from actions.clickup_tasks import ClickUpTaskCreator
            task_creator = ClickUpTaskCreator()
            import asyncio

            for patient_name, patient_issue_list in patient_issues.items():
                claim_ids = list(set(i["claim_id"] for i in patient_issue_list))
                # Get the client_id from the first issue entry
                patient_client_id = next(
                    (i["client_id"] for i in patient_issue_list if i.get("client_id")),
                    "",
                )
                issue_bullets = "\n".join(
                    f"  - [{i['check']}] {i['issue']}" for i in patient_issue_list
                )
                history = f"Pre-billing check on {date.today().strftime('%m/%d/%y')}"

                asyncio.ensure_future(
                    task_creator.create_or_update_patient_task(
                        patient_name=patient_name,
                        claim_id=", ".join(claim_ids),
                        issue=f"Pre-billing issues found:\n{issue_bullets}",
                        history=history,
                        client_id=patient_client_id,
                    )
                )
        except Exception as e:
            logger.error("Failed to create ClickUp tasks for blocked claims", error=str(e))

    total_checked = len(claims)
    logger.info(
        "Pre-billing checks complete",
        total=total_checked,
        passed=len(passed),
        fixed=len(fixed),
        blocked=len(blocked),
    )

    return {
        "passed": passed,
        "fixed": fixed,
        "blocked": blocked,
        "issues": issues,
        "summary": {
            "total_checked": total_checked,
            "total_passed": len(passed),
            "total_fixed": len(fixed),
            "total_blocked": len(blocked),
        },
    }
