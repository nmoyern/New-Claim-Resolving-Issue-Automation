"""
notes/formatter.py
------------------
Enforces LCI's mandatory claim note format:
  [Action taken. What was found. What was done. Lauris fix. Gap logged. Next step.] #INITIALS #MM/DD/YY

Rules from Complete Framework (March 2026):
  - # used ONLY for initials and date suffix — never in the body
  - Date: 2-digit month / 2-digit day / 2-digit year
  - "Follow-up" means: contacted payer, documented rep name + ref# + resolution date
  - Simply noting a status/date is NOT acceptable follow-up
  - Every note must describe: what action was taken, what was found, and what happens next
  - Lauris fix and gap category should be included when applicable

IMPORTANT: initials always resolved lazily via os.getenv so that
           AUTOMATION_INITIALS set in the environment is honoured at call time,
           not captured once at module-import time.
"""
from __future__ import annotations

import os
from datetime import date, timedelta
from typing import Optional


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_initials() -> str:
    return os.getenv("AUTOMATION_INITIALS", "AUTO")


def _validate_body(text: str) -> None:
    if "#" in text:
        raise ValueError(
            f"Note body must NOT contain '#' — it is reserved for the suffix only.\n"
            f"Offending text: {text!r}"
        )


def _followup_date_str(days: int) -> str:
    return (date.today() + timedelta(days=days)).strftime("%m/%d/%y")


def _today_str() -> str:
    return date.today().strftime("%m/%d/%y")


# ---------------------------------------------------------------------------
# Core formatter
# ---------------------------------------------------------------------------

def format_note(
    body: str,
    initials: Optional[str] = None,
    as_of: Optional[date] = None,
) -> str:
    """
    Returns a properly formatted claim note.
    Raises ValueError if body contains '#'.
    """
    _validate_body(body)
    ini = initials if initials is not None else _default_initials()
    d   = (as_of or date.today()).strftime("%m/%d/%y")
    return f"{body.rstrip()} #{ini} #{d}"


# ---------------------------------------------------------------------------
# Pre-built note templates — Updated March 2026
# All new params have defaults for backwards compatibility
# ---------------------------------------------------------------------------

def note_correction(
    corrections_made: str,
    initials: Optional[str] = None,
    *,
    field: str = "",
    old_value: str = "",
    new_value: str = "",
    source: str = "",
    lauris_fix: str = "",
    gap_category: str = "",
) -> str:
    """Step 1 correction note."""
    ini = initials if initials is not None else _default_initials()
    if field and old_value and new_value:
        body = (
            f"Step 1 correction: {field} was {old_value} — corrected to {new_value}"
            f"{' per ' + source if source else ''}. "
            f"Resubmitted {_today_str()}."
        )
    else:
        body = f"Corrections made: {corrections_made}. Retransmitted {_today_str()}."
    if lauris_fix:
        body += f" Lauris fixed: {lauris_fix}."
    if gap_category:
        body += f" Gap logged: {gap_category}."
    return format_note(body, ini)


def note_reconsideration_submitted(
    mco: str,
    initials: Optional[str] = None,
    *,
    reason_text: str = "",
    docs_attached: str = "",
    lauris_fix: str = "",
    gap_category: str = "",
) -> str:
    """Step 2 reconsideration note."""
    ini = initials if initials is not None else _default_initials()
    body = f"Step 2 reconsideration submitted to {mco} on {_today_str()}."
    if reason_text:
        body += f" Reason: {reason_text}."
    if docs_attached:
        body += f" Documents attached: {docs_attached}."
    body += (
        f" Response expected within 30-45 days."
        f" Will follow up by {_followup_date_str(45)}."
    )
    if lauris_fix:
        body += f" Lauris: {lauris_fix}."
    if gap_category:
        body += f" Gap logged: {gap_category}."
    return format_note(body, ini)


def note_appeal_submitted(
    mco: str,
    initials: Optional[str] = None,
    *,
    recon_date: str = "",
    recon_outcome: str = "",
    docs_attached: str = "",
    lauris_fix: str = "",
    gap_category: str = "",
) -> str:
    """Step 3 formal appeal note."""
    ini = initials if initials is not None else _default_initials()
    body = f"Step 3 appeal submitted to {mco} on {_today_str()}."
    if recon_date:
        body += f" Prior recon submitted {recon_date}"
        if recon_outcome:
            body += f" — {recon_outcome}"
        body += "."
    if docs_attached:
        body += f" Documents attached: {docs_attached}."
    body += f" Will follow up in 45 days."
    if lauris_fix:
        body += f" Lauris: {lauris_fix}."
    if gap_category:
        body += f" Gap logged: {gap_category}."
    return format_note(body, ini)


def note_write_off(
    reason: str,
    extra: str = "",
    initials: Optional[str] = None,
    *,
    amount: float = 0.0,
    gap_category: str = "",
) -> str:
    """
    Write-off note. Per Complete Framework:
    "Write off — [reason]. [context]. Total amount: $X. Gap logged: [category]."
    """
    ini = initials if initials is not None else _default_initials()
    body = f"Write off - {reason}"
    if extra:
        body += f" {extra}"
    if amount > 0:
        body += f" Total amount written off: ${amount:.2f}."
    if gap_category:
        body += f" Gap logged: {gap_category}."
    body += " Lauris write-off completed."
    return format_note(body, ini)


def note_auth_verified_in_portal(
    mco: str,
    auth_number: str,
    initials: Optional[str] = None,
    *,
    dos_range: str = "",
    billing_region: str = "",
    lauris_fix: str = "",
) -> str:
    """Auth found in MCO portal — reconsideration triggered."""
    ini = initials if initials is not None else _default_initials()
    body = f"Auth verified in {mco} portal. Auth {auth_number}"
    if dos_range:
        body += f" approved for {dos_range}"
    body += "."
    if billing_region:
        body += f" Billing region {billing_region} matched."
    body += " Step 2 reconsideration submitted."
    if lauris_fix:
        body += f" Lauris: {lauris_fix}."
    return format_note(body, ini)


def note_auth_not_found_fax_sent(
    mco: str,
    original_fax_date: date,
    initials: Optional[str] = None,
    *,
    fax_id: str = "",
    lauris_fix: str = "",
    gap_category: str = "",
) -> str:
    """Auth not in portal, but fax proof found — refax sent."""
    ini = initials if initials is not None else _default_initials()
    body = f"Auth not found in {mco} portal."
    fax_date_str = original_fax_date.strftime("%m/%d/%y")
    if fax_id:
        body += f" Verified original fax sent {fax_date_str} via Lauris Fax Proxy (fax ID {fax_id})."
    else:
        body += f" Verified original fax sent {fax_date_str}."
    body += (
        f" Refax package sent {_today_str()} to {mco} fax — "
        f"cover letter, SRA copy, fax confirmation."
        f" Requested honor of original submission date."
    )
    if lauris_fix:
        body += f" Lauris: {lauris_fix}."
    if gap_category:
        body += f" Gap logged: {gap_category}."
    return format_note(body, ini)


def note_auth_not_found_dropbox_found(
    mco: str,
    dropbox_path: str,
    file_date: str,
    initials: Optional[str] = None,
    *,
    lauris_fix: str = "",
) -> str:
    """Auth not in portal, but Dropbox confirmation found."""
    ini = initials if initials is not None else _default_initials()
    body = (
        f"Auth not found in {mco} portal."
        f" Submission verified via Dropbox — confirmation file located at {dropbox_path}."
        f" File dated {file_date}."
        f" Reconsideration submitted with Dropbox confirmation attached."
    )
    if lauris_fix:
        body += f" Lauris: {lauris_fix}."
    return format_note(body, ini)


def note_auth_not_found_dropbox_missing(
    mco: str,
    initials: Optional[str] = None,
    *,
    gap_category: str = "AUTH — Submitted but Not Saved to Dropbox",
) -> str:
    """Auth not in portal and NOT in Dropbox — human review required."""
    ini = initials if initials is not None else _default_initials()
    body = (
        f"HUMAN REVIEW REQUIRED — Auth not found in {mco} portal."
        f" Portal submission method used but NO confirmation file found in Dropbox."
        f" Cannot verify submission without saved confirmation."
        f" Flagged for admin follow-up."
        f" Gap logged: {gap_category}."
    )
    return format_note(body, ini)


def note_auth_never_submitted(
    initials: Optional[str] = None,
    *,
    gap_category: str = "AUTH — Never Submitted",
) -> str:
    """Authorization was never submitted — urgent human review."""
    ini = initials if initials is not None else _default_initials()
    body = (
        f"HUMAN REVIEW REQUIRED — Authorization was never submitted."
        f" New auth request must be submitted immediately."
        f" Gap logged: {gap_category}."
    )
    return format_note(body, ini)


def note_mco_call(
    rep_name: str,
    ref_number: str,
    outcome: str,
    resolution_date: Optional[date] = None,
    initials: Optional[str] = None,
    *,
    lauris_update: str = "",
    gap_category: str = "",
) -> str:
    """MCO phone call outcome note."""
    ini = initials if initials is not None else _default_initials()
    # NOTE: Do NOT put '#' before ref_number — '#' is reserved for suffix only
    body = f"Called MCO. Spoke with {rep_name} (ref {ref_number}). {outcome}."
    if resolution_date:
        body += f" Committed resolution by {resolution_date.strftime('%m/%d/%y')}."
        body += " Will follow up if not resolved."
    if lauris_update:
        body += f" Lauris: {lauris_update}."
    if gap_category:
        body += f" Gap logged: {gap_category}."
    return format_note(body, ini)


def note_follow_up_pending(
    last_action: str,
    next_followup: date,
    initials: Optional[str] = None,
) -> str:
    ini = initials if initials is not None else _default_initials()
    body = (f"{last_action}. "
            f"Follow-up scheduled: {next_followup.strftime('%m/%d/%y')}.")
    return format_note(body, ini)


def note_billing_company_fixed(
    old_company: str,
    new_company: str,
    initials: Optional[str] = None,
    *,
    lauris_fix: str = "Company field updated on client facesheet",
    gap_category: str = "BILLING — Wrong Program / Billing Company",
) -> str:
    ini = initials if initials is not None else _default_initials()
    body = (
        f"Step 1 correction: billing company was {old_company} — "
        f"corrected to {new_company} to match MCO approval letter."
        f" Resubmitted {_today_str()}."
        f" Lauris fixed: {lauris_fix}."
        f" Gap logged: {gap_category}."
    )
    return format_note(body, ini)


def note_era_uploaded(
    mco: str,
    era_id: str,
    initials: Optional[str] = None,
) -> str:
    ini = initials if initials is not None else _default_initials()
    body = (f"ERA {era_id} from {mco} uploaded to Lauris. "
            f"Claim cleared from outstanding report.")
    return format_note(body, ini)


def note_timely_filing_flag(
    dos: str,
    amount: float,
    initials: Optional[str] = None,
    *,
    billed_date: str = "unknown",
    days_beyond: str = "unknown",
    gap_category: str = "BILLING — Not Billed Within Timely Filing Window",
) -> str:
    """Timely filing human review flag."""
    ini = initials if initials is not None else _default_initials()
    body = (
        f"HUMAN REVIEW REQUIRED — Claim denied for timely filing."
        f" DOS {dos}. Originally billed {billed_date}."
        f" Days beyond filing limit: {days_beyond}."
        f" Potential revenue loss: ${amount:.2f}."
        f" Gap logged: {gap_category}."
        f" Operations manager review required before any write-off decision."
    )
    return format_note(body, ini)


def note_human_review_needed(
    reason: str,
    initials: Optional[str] = None,
) -> str:
    ini = initials if initials is not None else _default_initials()
    body = f"HUMAN REVIEW REQUIRED — {reason}. Automation flagged — do not auto-process."
    return format_note(body, ini)


# ---------------------------------------------------------------------------
# Reconsideration reason text (verbatim from Admin Manual — confirmed correct)
# ---------------------------------------------------------------------------

RECON_REASON_TEMPLATES = {
    "no_auth": (
        "Open, approved authorization on file for DOS. Requesting full reimbursement."
    ),
    "duplicate": (
        "Claim submitted for DOS is complete, accurate, and not a duplicate DOS. "
        "Requesting full reimbursement."
    ),
    "not_enrolled_assessment": (
        "Claim is for an eligibility assessment for H0046 services. "
        "Requesting full reimbursement."
    ),
    "aetna_standard": (
        "The submission of record of the attached claim meets all DMAS standards for "
        "payment to be received as services were delivered and documented in accordance "
        "with guidelines & regulations. Open, approved authorization on file for DOS."
    ),
    "sentara_standard": (
        "Open, approved authorization on file for DOS. Requesting full reimbursement."
    ),
    "united_standard": (
        "Open, approved authorization on file for DOS. Requesting full reimbursement."
    ),
}


def get_recon_reason(denial_code: str, mco: str) -> str:
    """Return the correct reconsideration reason text for a given denial + MCO."""
    if "aetna" in mco.lower():
        return RECON_REASON_TEMPLATES["aetna_standard"]
    if denial_code == "no_auth":
        return RECON_REASON_TEMPLATES["no_auth"]
    if denial_code == "duplicate":
        return RECON_REASON_TEMPLATES["duplicate"]
    if denial_code in ("not_enrolled", "not_enrolled_assessment"):
        return RECON_REASON_TEMPLATES["not_enrolled_assessment"]
    mco_key = f"{mco.lower()}_standard"
    return RECON_REASON_TEMPLATES.get(mco_key, RECON_REASON_TEMPLATES["no_auth"])


# ---------------------------------------------------------------------------
# Module-level constant (resolved at access time via __getattr__)
# ---------------------------------------------------------------------------

def __getattr__(name: str):
    """Lazy module attribute: AUTOMATION_INITIALS reads env at access time."""
    if name == "AUTOMATION_INITIALS":
        return os.getenv("AUTOMATION_INITIALS", "AUTO")
    raise AttributeError(f"module 'notes.formatter' has no attribute {name!r}")
