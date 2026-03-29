"""
decision_tree/router.py
-----------------------
Maps every claim denial code + context to the correct ResolutionAction.
Mirrors the Master Decision Tree from the Complete Framework document.

KEY RULE CHANGES (March 2026 team review):
  - RRR: Only write off if provider=NHCS AND amount <= $19.80.
         Otherwise submit reconsideration (urban area providers).
  - Timely filing: Resubmit first, then recon after 30 days. NOT auto-human-review.
  - Magellan: Does NOT always require Thursday phone call first.
  - Billing day: Wednesday (not Tuesday).
  - Every denial triggers TWO actions: fix claim AND fix root cause in Lauris.
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import List, Tuple

from config.models import (
    Claim,
    ClaimStatus,
    DenialCode,
    MCO,
    Program,
    ResolutionAction,
)
from config.settings import SKIP_NEWER_DAYS
from logging_utils.logger import get_logger

logger = get_logger("router")


# ---------------------------------------------------------------------------
# Denial code → primary action map
# Order matters: more specific rules first
# ---------------------------------------------------------------------------

_ROUTING_TABLE: dict[DenialCode, Tuple[ResolutionAction, str]] = {
    # Format / data errors → Step 1 correction
    DenialCode.INVALID_ID:        (ResolutionAction.CORRECT_AND_RESUBMIT, "incorrect_member_id"),
    DenialCode.INVALID_DOB:       (ResolutionAction.CORRECT_AND_RESUBMIT, "incorrect_dob"),
    DenialCode.INVALID_NPI:       (ResolutionAction.CORRECT_AND_RESUBMIT, "incorrect_npi"),
    DenialCode.INVALID_DIAG:      (ResolutionAction.CORRECT_AND_RESUBMIT, "invalid_diagnostic_code"),
    DenialCode.DUPLICATE:         (ResolutionAction.RECONSIDERATION, "duplicate"),
    DenialCode.WRONG_BILLING_CO:  (ResolutionAction.LAURIS_FIX_COMPANY, "wrong_billing_company"),

    # Payer decisions
    DenialCode.NO_AUTH:           (ResolutionAction.MCO_PORTAL_AUTH_CHECK, "no_auth_on_file"),
    DenialCode.AUTH_EXPIRED:      (ResolutionAction.MCO_PORTAL_AUTH_CHECK, "auth_expired"),
    DenialCode.NOT_ENROLLED:      (ResolutionAction.RECONSIDERATION, "not_enrolled_assessment"),
    # UNDERPAID: handled specially in route() — NHCS <= $19.80 auto-reprocesses, others recon
    DenialCode.UNDERPAID:         (ResolutionAction.RECONSIDERATION, "underpaid_auto_recon"),
    # RRR: handled specially in route() — NOT a blanket write-off
    DenialCode.RURAL_RATE_REDUCTION: (ResolutionAction.WRITE_OFF, "rural_rate_reduction"),
    DenialCode.RECOUPMENT:        (ResolutionAction.HUMAN_REVIEW, "recoupment_detected"),
    # Timely filing: resubmit first, not auto human review
    DenialCode.TIMELY_FILING:     (ResolutionAction.CORRECT_AND_RESUBMIT, "timely_filing_resubmit"),
    DenialCode.NEEDS_CALL:        (ResolutionAction.PHONE_CALL_THURSDAY, "unclear_denial"),
    DenialCode.COVERAGE_TERMINATED: (ResolutionAction.HUMAN_REVIEW, "coverage_terminated_check_eligibility"),
    # PROVIDER_NOT_CERTIFIED: handled specially in route() — retransmit once, then recon with DMAS letter
    DenialCode.PROVIDER_NOT_CERTIFIED: (ResolutionAction.CORRECT_AND_RESUBMIT, "provider_not_certified_retransmit"),
    DenialCode.UNLISTED_PROCEDURE: (ResolutionAction.HUMAN_REVIEW, "unlisted_procedure_check_dual_plan"),
    # Rendering NPI: Dr. Yancey for ANY RCSU claim, any company
    DenialCode.MISSING_NPI_RENDERING: (
        ResolutionAction.CORRECT_AND_RESUBMIT,
        "missing_rendering_npi_add_yancey_any_rcsu",
    ),
    DenialCode.DIAGNOSIS_BLANK:   (
        ResolutionAction.CORRECT_AND_RESUBMIT,
        "diagnosis_blank_clickup_then_fix",
    ),
    # EXCEEDED_UNITS: handled specially in route() — check portal, recon if not actually exceeded
    DenialCode.EXCEEDED_UNITS: (
        ResolutionAction.MCO_PORTAL_AUTH_CHECK,
        "exceeded_units_verify",
    ),

    # Escalated
    DenialCode.RECON_DENIED:      (ResolutionAction.APPEAL_STEP3, "reconsideration_denied"),
    DenialCode.NO_RESPONSE_45D:   (ResolutionAction.APPEAL_STEP3, "no_response_45_days"),
    DenialCode.MCO_APPEAL_DENIED: (ResolutionAction.HUMAN_REVIEW, "mco_appeal_denied_escalate_dmas"),

    DenialCode.UNKNOWN:           (ResolutionAction.HUMAN_REVIEW, "unknown_denial_code"),
}

# Irregular ERAs that must never be auto-processed
_IRREGULAR_ERA_TYPES = {
    "anthem_marys", "united_marys", "recoupment", "straight_medicaid_marys"
}

# RRR write-off threshold: only NHCS and <= $19.80
RRR_WRITEOFF_MAX_AMOUNT = 19.80
RRR_WRITEOFF_PROGRAM = Program.NHCS

# NHCS MHSS correct rate: $102.72 per unit
# If billed above this rate and underpaid, write off the difference
NHCS_MHSS_RATE_PER_UNIT = 102.72


class ClaimRouter:
    """Determines the correct ResolutionAction for a given claim."""

    def route(self, claim: Claim) -> Tuple[ResolutionAction, str]:
        """
        Returns (action, reason_code).
        reason_code is a short slug used for logging and note generation.
        """
        # 1. Rural Rate Reduction — conditional write-off (March 2026 rule change)
        #    Only write off if provider=NHCS AND amount <= $19.80
        #    Otherwise submit reconsideration (urban area providers should be paid as billed)
        if claim.denial_codes and DenialCode.RURAL_RATE_REDUCTION in claim.denial_codes:
            if (claim.program == RRR_WRITEOFF_PROGRAM
                    and claim.billed_amount <= RRR_WRITEOFF_MAX_AMOUNT):
                return ResolutionAction.WRITE_OFF, "rural_rate_reduction_nhcs_under_threshold"
            else:
                logger.info(
                    "RRR claim NOT auto-written-off — urban provider or amount exceeds threshold",
                    claim_id=claim.claim_id,
                    program=claim.program.value,
                    amount=claim.billed_amount,
                )
                return ResolutionAction.RECONSIDERATION, "rural_rate_reduction_recon_urban_or_over_threshold"

        # 1b. Underpayment handling
        #     NHCS MHSS overbilled: if billed above $102.72/unit, write off the
        #     difference (the correct rate IS $102.72, so the overage is ours).
        #     NHCS <= $19.80: auto-reprocess.
        #     All others: auto-submit reconsideration first.
        if claim.denial_codes and DenialCode.UNDERPAID in claim.denial_codes:
            # Check if NHCS MHSS was billed above the correct rate
            service = (claim.service_code or "").upper()
            if (claim.program == Program.NHCS
                    and service == "MHSS"
                    and claim.units > 0):
                correct_total = round(claim.units * NHCS_MHSS_RATE_PER_UNIT, 2)
                if claim.billed_amount > correct_total + 0.01:
                    # Billed above $102.72/unit — write off the overage
                    overage = round(claim.billed_amount - correct_total, 2)
                    logger.info(
                        "NHCS MHSS overbilled — writing off difference",
                        claim_id=claim.claim_id,
                        billed=claim.billed_amount,
                        correct=correct_total,
                        overage=overage,
                    )
                    return (
                        ResolutionAction.WRITE_OFF,
                        f"nhcs_mhss_overbilled_writeoff_{overage}",
                    )
                # Billed at or below correct rate — paid correctly
                if (claim.paid_amount > 0
                        and abs(claim.paid_amount - correct_total) < 0.01):
                    logger.info(
                        "NHCS MHSS paid at correct rate — no action",
                        claim_id=claim.claim_id,
                    )
                    return ResolutionAction.SKIP, "nhcs_mhss_paid_correctly"

            if (claim.program == Program.NHCS
                    and claim.billed_amount <= RRR_WRITEOFF_MAX_AMOUNT):
                return ResolutionAction.REPROCESS_LAURIS, "underpaid_nhcs_reprocess"

            # All others: auto-submit reconsideration
            logger.info(
                "Underpaid claim — auto-submitting reconsideration",
                claim_id=claim.claim_id,
                program=claim.program.value,
                amount=claim.billed_amount,
            )
            return (
                ResolutionAction.RECONSIDERATION,
                "underpaid_auto_recon",
            )

        # 2. Rejections (from Claim.MD/clearinghouse) → fix immediately.
        #    Rejections mean the claim never reached the MCO.
        #    No waiting — fix the data issue and resubmit right away.
        if claim.status == ClaimStatus.REJECTED:
            logger.info(
                "Claim REJECTED by clearinghouse — processing immediately",
                claim_id=claim.claim_id,
            )
            # Fall through to denial code routing below (Node 8)

        # 3. Skip DENIED claims resubmitted within 14 days UNLESS denied again.
        #    Denials are from the MCO — they received and processed the claim.
        #    - If denied again within 14 days → work on it (new MCO denial)
        #    - If just sitting (no new denial) → wait 14 days for MCO to process
        elif claim.last_followup:
            days_since = (date.today() - claim.last_followup).days
            if days_since < 14:
                # Check if there's a new denial AFTER the resubmission
                has_new_denial = (
                    claim.date_denied
                    and claim.last_followup
                    and claim.date_denied > claim.last_followup
                )
                if has_new_denial:
                    logger.info(
                        "Claim denied again within 14 days — processing",
                        claim_id=claim.claim_id,
                        days_since=days_since,
                        denied_date=str(claim.date_denied),
                        resubmitted_date=str(claim.last_followup),
                    )
                    # Fall through to normal routing
                else:
                    logger.info(
                        "Skipping claim — resubmitted within 14 days, no new denial",
                        claim_id=claim.claim_id,
                        days_since=days_since,
                    )
                    return ResolutionAction.SKIP, "resubmitted_wait_14_days"

        # 3. Already in reconsideration — check if 45-day timeout
        if claim.status == ClaimStatus.IN_RECON:
            if claim.recon_submitted:
                days_since = (date.today() - claim.recon_submitted).days
                if days_since >= 45:
                    return ResolutionAction.APPEAL_STEP3, "recon_no_response_45d"
            return ResolutionAction.SKIP, "recon_in_progress_not_due"

        # 4. Already in appeal — check if 45-day timeout
        if claim.status == ClaimStatus.IN_APPEAL:
            if claim.appeal_submitted:
                days_since = (date.today() - claim.appeal_submitted).days
                if days_since >= 45:
                    return ResolutionAction.HUMAN_REVIEW, "appeal_no_response_escalate_dmas"
            return ResolutionAction.SKIP, "appeal_in_progress_not_due"

        # 4b. Provider Not Certified — retransmit once, then recon with DMAS letter
        if claim.denial_codes and DenialCode.PROVIDER_NOT_CERTIFIED in claim.denial_codes:
            if claim.last_followup:
                # Already retransmitted at least once — submit recon with DMAS cert letter
                return (
                    ResolutionAction.RECONSIDERATION,
                    "provider_not_certified_recon_dmas_letter",
                )
            # First time — retransmit
            return (
                ResolutionAction.CORRECT_AND_RESUBMIT,
                "provider_not_certified_retransmit",
            )

        # 5. Timely filing special handling:
        #    First attempt: resubmit. If claim was already resubmitted and >30 days,
        #    escalate to reconsideration.
        if claim.denial_codes and DenialCode.TIMELY_FILING in claim.denial_codes:
            if claim.last_followup and (date.today() - claim.last_followup).days >= 30:
                return ResolutionAction.RECONSIDERATION, "timely_filing_recon_after_30d"
            return ResolutionAction.CORRECT_AND_RESUBMIT, "timely_filing_resubmit"

        # 6. Route by primary denial code (first code wins)
        if claim.denial_codes:
            primary = claim.denial_codes[0]
            if primary in _ROUTING_TABLE:
                action, reason = _ROUTING_TABLE[primary]
                logger.info(
                    "Claim routed",
                    claim_id=claim.claim_id,
                    denial_code=primary.value,
                    action=action.value,
                )
                return action, reason

        # 7. Fallback: no codes but claim is overdue
        if claim.age_days > 14:
            return ResolutionAction.PHONE_CALL_THURSDAY, "overdue_no_denial_code"

        return ResolutionAction.HUMAN_REVIEW, "no_routing_match"

    def route_batch(self, claims: List[Claim]) -> List[Tuple[Claim, ResolutionAction, str]]:
        """Route a list of claims, returning (claim, action, reason) tuples."""
        results = []
        for claim in claims:
            action, reason = self.route(claim)
            results.append((claim, action, reason))
        return results


# ---------------------------------------------------------------------------
# Daily schedule helper
# ---------------------------------------------------------------------------

def get_todays_primary_actions() -> List[ResolutionAction]:
    """
    Returns ALL actions — the system runs on demand (one command, one SMS code).
    No day-of-week scheduling; the user triggers a full run manually.

    Previously had a per-day schedule; removed March 2026 so every run
    processes everything outstanding.
    """
    return [
        ResolutionAction.ERA_UPLOAD,
        ResolutionAction.WRITE_OFF,
        ResolutionAction.CORRECT_AND_RESUBMIT,
        ResolutionAction.MCO_PORTAL_AUTH_CHECK,
        ResolutionAction.RECONSIDERATION,
        ResolutionAction.APPEAL_STEP3,
        ResolutionAction.PHONE_CALL_THURSDAY,
        ResolutionAction.LAURIS_FIX_COMPANY,
        ResolutionAction.REPROCESS_LAURIS,
        ResolutionAction.HUMAN_REVIEW,
    ]
