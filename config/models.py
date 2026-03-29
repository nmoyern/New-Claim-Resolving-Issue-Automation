"""
config/models.py
----------------
All dataclasses / enums used across the automation.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional, List


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class MCO(str, enum.Enum):
    UNITED    = "united"
    SENTARA   = "sentara"
    AETNA     = "aetna"
    ANTHEM    = "anthem"
    MOLINA    = "molina"
    HUMANA    = "humana"
    MAGELLAN  = "magellan"
    DMAS      = "dmas"        # straight Medicaid
    UNKNOWN   = "unknown"


class Program(str, enum.Enum):
    KJLN        = "KJLN"        # Kempsville Junction Life Network
    NHCS        = "NHCS"        # New Heights Community Support
    MARYS_HOME  = "MARYS_HOME"
    UNKNOWN     = "UNKNOWN"


class ClaimStatus(str, enum.Enum):
    PENDING         = "pending"
    REJECTED        = "rejected"       # Format/transmission error — fixable
    DENIED          = "denied"         # Payer decision
    UNDERPAID       = "underpaid"
    PAID            = "paid"
    WRITTEN_OFF     = "written_off"
    IN_RECON        = "in_reconsideration"
    IN_APPEAL       = "in_appeal"
    NEEDS_HUMAN     = "needs_human_review"


class DenialCode(str, enum.Enum):
    # Clearinghouse / format errors
    INVALID_ID          = "invalid_id"
    INVALID_DOB         = "invalid_dob"
    INVALID_NPI         = "invalid_npi"
    INVALID_DIAG        = "invalid_diag"
    WRONG_BILLING_CO    = "wrong_billing_company"
    DUPLICATE           = "duplicate"

    # Payer decisions
    NO_AUTH             = "no_auth_on_file"
    AUTH_EXPIRED        = "auth_expired"
    NOT_ENROLLED        = "not_enrolled"    # Assessment / eligibility
    COVERAGE_TERMINATED = "coverage_terminated"  # Patient not enrolled on DOS
    TIMELY_FILING       = "timely_filing"
    RURAL_RATE_REDUCTION = "rural_rate_reduction"
    RECOUPMENT          = "recoupment"
    RECON_DENIED        = "reconsideration_denied"
    UNDERPAID           = "underpaid"
    NEEDS_CALL          = "needs_mco_call"
    PROVIDER_NOT_CERTIFIED = "provider_not_certified"
    UNLISTED_PROCEDURE  = "unlisted_procedure_code"
    MISSING_NPI_RENDERING = "missing_rendering_npi"
    DIAGNOSIS_BLANK     = "diagnosis_pointer_blank"
    EXCEEDED_UNITS      = "exceeded_units"

    # Escalated
    MCO_APPEAL_DENIED   = "mco_appeal_denied"
    NO_RESPONSE_45D     = "no_response_45_days"

    UNKNOWN             = "unknown"


class ResolutionAction(str, enum.Enum):
    SKIP                    = "skip_too_new"
    CORRECT_AND_RESUBMIT    = "correct_and_resubmit"
    RECONSIDERATION         = "reconsideration"
    MCO_PORTAL_AUTH_CHECK   = "mco_portal_auth_check"
    LAURIS_FAX_VERIFY       = "lauris_fax_verify"
    LAURIS_FIX_COMPANY      = "lauris_fix_company"
    REPROCESS_LAURIS        = "reprocess_lauris"
    WRITE_OFF               = "write_off"
    APPEAL_STEP3            = "appeal_step3"
    PHONE_CALL_THURSDAY     = "phone_call_thursday"
    HUMAN_REVIEW            = "human_review"
    ERA_UPLOAD              = "era_upload"


# ---------------------------------------------------------------------------
# Core claim model
# ---------------------------------------------------------------------------

@dataclass
class Claim:
    claim_id:       str
    client_name:    str
    client_id:      str             # MCO member ID
    dos:            date            # Date of service
    mco:            MCO
    program:        Program
    billed_amount:  float
    paid_amount:    float = 0.0
    lauris_id:      str = ""        # Lauris Unique ID (e.g. ID004665)
    status:         ClaimStatus = ClaimStatus.PENDING
    denial_codes:   List[DenialCode] = field(default_factory=list)
    denial_reason_raw: str = ""     # Raw text from Claim.MD
    auth_number:    str = ""
    npi:            str = ""
    service_code:   str = ""        # e.g. "RCSU", "MHSS", etc.
    proc_code:      str = ""        # CPT/HCPCS code (e.g. "H2015", "H0031")
    units:          float = 0.0     # Number of billed units
    rate_per_unit:  float = 0.0     # Rate per unit (for adjustments)
    billing_region: str = ""
    date_billed:    Optional[date] = None
    date_denied:    Optional[date] = None
    last_note:      str = ""
    last_followup:  Optional[date] = None
    next_followup:  Optional[date] = None
    recon_submitted: Optional[date] = None
    appeal_submitted: Optional[date] = None
    claimmd_url:    str = ""
    age_days:       int = 0         # Days since DOS

    def __post_init__(self):
        if self.date_billed and self.age_days == 0:
            self.age_days = (date.today() - self.date_billed).days


@dataclass
class ERA:
    """Electronic Remittance Advice — a payment batch from an MCO."""
    era_id:       str
    mco:          MCO
    program:      Program
    payment_date: date
    total_amount: float
    file_path:    str               # Local path to downloaded 835 file
    is_irregular: bool = False      # Anthem Mary's, recoupments, etc.
    irregular_type: str = ""        # "anthem_marys", "united_marys", "recoupment", "straight_medicaid_marys"
    claims:       List[Claim] = field(default_factory=list)
    uploaded:     bool = False


@dataclass
class AuthRecord:
    """Service authorization record from MCO portal or Lauris."""
    client_id:    str
    client_name:  str
    mco:          MCO
    program:      Program
    auth_number:  str
    proc_code:    str
    start_date:   date
    end_date:     date
    approved_units: int = 0
    status:       str = "approved"  # approved | denied | pending | not_found
    source:       str = "portal"    # portal | lauris | fax


@dataclass
class ResolutionResult:
    """What the automation did with a single claim."""
    claim:          Claim
    action_taken:   ResolutionAction
    success:        bool
    note_written:   str = ""
    needs_human:    bool = False
    human_reason:   str = ""
    timestamp:      datetime = field(default_factory=datetime.now)
    error:          Optional[str] = None


@dataclass
class DailyRunSummary:
    run_date:           date = field(default_factory=date.today)
    eras_uploaded:      int = 0
    claims_at_start:    int = 0
    claims_completed:   int = 0
    write_offs:         int = 0
    recons_submitted:   int = 0
    corrections_made:   int = 0
    appeals_submitted:  int = 0
    human_review_flags: int = 0
    errors:             List[str] = field(default_factory=list)
    results:            List[ResolutionResult] = field(default_factory=list)

    @property
    def claims_remaining(self) -> int:
        return max(0, self.claims_at_start - self.claims_completed)

    def to_clickup_comment(self) -> str:
        lines = [
            f"Automated run {self.run_date.strftime('%m/%d/%y')}.",
            f"ERAs uploaded: {self.eras_uploaded}.",
            f"Started with {self.claims_at_start} outstanding claims.",
            f"Completed: {self.claims_completed} "
            f"({self.corrections_made} corrections, "
            f"{self.recons_submitted} reconsiderations, "
            f"{self.write_offs} write-offs, "
            f"{self.appeals_submitted} appeals).",
            f"Remaining: {self.claims_remaining} claims.",
        ]
        # Autonomous corrections summary
        try:
            from reporting.autonomous_tracker import get_daily_correction_summary
            ac = get_daily_correction_summary()
            if ac["total"] > 0:
                type_parts = ", ".join(
                    f"{t}: {c}" for t, c in ac["by_type"].items()
                )
                lines.append(
                    f"Autonomous corrections: {ac['total']} "
                    f"({type_parts})."
                )
        except Exception:
            pass
        if self.human_review_flags:
            lines.append(
                f"⚠️ {self.human_review_flags} claim(s) flagged "
                f"for human review."
            )
        if self.errors:
            lines.append(f"❌ {len(self.errors)} error(s) — check logs.")
        lines.append(f"#AUTO #{date.today().strftime('%m/%d/%y')}")
        return " ".join(lines)
