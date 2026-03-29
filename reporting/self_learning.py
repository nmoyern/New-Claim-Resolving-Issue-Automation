"""
reporting/self_learning.py
--------------------------
Self-learning and efficiency module for LCI claims automation.

Every 10th run, generates a Self-Learning Report that:
  - Maps decisions over time (which actions resolved claims vs. led to more denials)
  - Identifies what's working (high resolution rates) and what's not (recurring denials)
  - Proposes changes with estimated financial impact
  - Identifies efficiency improvements

Emails the report to ss@lifeconsultantsinc.org and nm@lifeconsultantsinc.org
BEFORE the system takes action on proposed changes.

Run counter is stored in data/run_counter.txt.
"""
from __future__ import annotations

import imaplib
import email as email_lib
import os
import re
import smtplib
import sqlite3
from collections import defaultdict
from datetime import date, datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
from typing import Dict, List, Optional

from dotenv import load_dotenv

from logging_utils.logger import get_logger

load_dotenv()

logger = get_logger("self_learning")

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
RUN_COUNTER_PATH = DATA_DIR / "run_counter.txt"
DB_PATH = DATA_DIR / "claims_history.db"

REPORT_RECIPIENTS = [
    "ss@lifeconsultantsinc.org",
    "nm@lifeconsultantsinc.org",
]

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 587

# ONLY nm@lifeconsultantsinc.org can approve self-learning changes
APPROVAL_AUTHORIZED_EMAIL = "nm@lifeconsultantsinc.org"

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993

# Self-learning report subject prefix (used to find replies)
SL_SUBJECT_PREFIX = "LCI Claims Automation — Self-Learning Report"


# ---------------------------------------------------------------------------
# Run counter
# ---------------------------------------------------------------------------

def increment_run_count() -> int:
    """Increment the persistent run counter and return the new value."""
    current = 0
    if RUN_COUNTER_PATH.exists():
        try:
            current = int(RUN_COUNTER_PATH.read_text().strip())
        except (ValueError, OSError):
            current = 0
    current += 1
    RUN_COUNTER_PATH.write_text(str(current))
    logger.info("Run counter incremented", run_count=current)
    return current


def should_generate_report() -> bool:
    """Return True every 10th run."""
    if not RUN_COUNTER_PATH.exists():
        return False
    try:
        count = int(RUN_COUNTER_PATH.read_text().strip())
    except (ValueError, OSError):
        return False
    return count > 0 and count % 10 == 0


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _one_year_ago() -> str:
    return (date.today() - timedelta(days=365)).isoformat()


# ---------------------------------------------------------------------------
# Analysis methods
# ---------------------------------------------------------------------------

def analyze_decision_outcomes() -> dict:
    """
    Analyze at least 1 year of data from claim_history and gap_report.

    Returns dict with:
      - action_outcomes: {action: {total, success, failed, resolution_rate}}
      - top_resolving_actions: list of (action, rate) sorted desc
      - top_failing_actions: list of (action, rate) sorted desc
      - denial_to_action_map: {denial_type: {action: count}}
    """
    conn = _get_conn()
    cutoff = _one_year_ago()

    # --- Per-action success/fail breakdown ---
    rows = conn.execute(
        """SELECT action_taken,
                  COUNT(*) as total,
                  SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) as successes,
                  SUM(CASE WHEN result != 'success' THEN 1 ELSE 0 END) as failures,
                  COALESCE(SUM(dollar_amount), 0) as total_dollars
           FROM claim_history
           WHERE date >= ?
           GROUP BY action_taken
           ORDER BY total DESC""",
        (cutoff,),
    ).fetchall()

    action_outcomes = {}
    for r in rows:
        total = r["total"]
        successes = r["successes"]
        rate = round((successes / total * 100), 1) if total > 0 else 0.0
        action_outcomes[r["action_taken"]] = {
            "total": total,
            "success": successes,
            "failed": r["failures"],
            "resolution_rate": rate,
            "total_dollars": round(r["total_dollars"], 2),
        }

    sorted_by_rate = sorted(
        action_outcomes.items(),
        key=lambda x: x[1]["resolution_rate"],
        reverse=True,
    )
    top_resolving = [(a, d["resolution_rate"]) for a, d in sorted_by_rate if d["total"] >= 5]
    top_failing = [(a, d["resolution_rate"]) for a, d in reversed(sorted_by_rate) if d["total"] >= 5]

    # --- Denial type -> action map ---
    gap_rows = conn.execute(
        """SELECT gap_category, resolution, COUNT(*) as cnt
           FROM gap_report
           WHERE date >= ?
           GROUP BY gap_category, resolution
           ORDER BY gap_category, cnt DESC""",
        (cutoff,),
    ).fetchall()

    denial_to_action: Dict[str, Dict[str, int]] = defaultdict(dict)
    for r in gap_rows:
        denial_to_action[r["gap_category"]][r["resolution"]] = r["cnt"]

    conn.close()
    return {
        "action_outcomes": action_outcomes,
        "top_resolving_actions": top_resolving[:10],
        "top_failing_actions": top_failing[:10],
        "denial_to_action_map": dict(denial_to_action),
    }


def identify_patterns() -> list:
    """
    Identify recurring issues and preventable denials over the last year.

    Returns list of dicts:
      {pattern_type, description, count, estimated_dollars, recommendation}
    """
    conn = _get_conn()
    cutoff = _one_year_ago()
    patterns = []

    # 1. Recurring client denials (same client, same gap, 3+ times)
    recurring = conn.execute(
        """SELECT client_name, gap_category, COUNT(*) as cnt,
                  COALESCE(SUM(dollar_amount), 0) as total_dollars
           FROM gap_report
           WHERE date >= ?
           GROUP BY client_name, gap_category
           HAVING cnt >= 3
           ORDER BY cnt DESC""",
        (cutoff,),
    ).fetchall()

    for r in recurring:
        patterns.append({
            "pattern_type": "recurring_client_denial",
            "description": (
                f"{r['client_name']} has {r['cnt']}x '{r['gap_category']}' "
                f"denials in the past year"
            ),
            "count": r["cnt"],
            "estimated_dollars": round(r["total_dollars"], 2),
            "recommendation": (
                f"Investigate root cause for {r['client_name']}. "
                f"This pattern is preventable with upstream correction."
            ),
        })

    # 2. High-volume gap categories (systematic issues)
    high_volume = conn.execute(
        """SELECT gap_category, COUNT(*) as cnt,
                  COALESCE(SUM(dollar_amount), 0) as total_dollars,
                  SUM(CASE WHEN status = 'write_off' THEN 1 ELSE 0 END) as writeoffs,
                  COALESCE(SUM(CASE WHEN status = 'write_off' THEN dollar_amount ELSE 0 END), 0) as wo_dollars
           FROM gap_report
           WHERE date >= ?
           GROUP BY gap_category
           HAVING cnt >= 10
           ORDER BY total_dollars DESC""",
        (cutoff,),
    ).fetchall()

    for r in high_volume:
        patterns.append({
            "pattern_type": "high_volume_gap",
            "description": (
                f"'{r['gap_category']}' — {r['cnt']} denials totaling "
                f"${r['total_dollars']:,.2f} ({r['writeoffs']} written off "
                f"for ${r['wo_dollars']:,.2f})"
            ),
            "count": r["cnt"],
            "estimated_dollars": round(r["total_dollars"], 2),
            "recommendation": (
                f"Systemic fix needed for '{r['gap_category']}'. "
                f"Consider process change or pre-billing validation."
            ),
        })

    # 3. Actions that consistently fail (< 30% success rate with 10+ attempts)
    action_rows = conn.execute(
        """SELECT action_taken, COUNT(*) as total,
                  SUM(CASE WHEN result = 'success' THEN 1 ELSE 0 END) as successes,
                  COALESCE(SUM(dollar_amount), 0) as total_dollars
           FROM claim_history
           WHERE date >= ?
           GROUP BY action_taken
           HAVING total >= 10
           ORDER BY (CAST(successes AS FLOAT) / total) ASC""",
        (cutoff,),
    ).fetchall()

    for r in action_rows:
        rate = round((r["successes"] / r["total"] * 100), 1) if r["total"] > 0 else 0.0
        if rate < 30.0:
            patterns.append({
                "pattern_type": "low_success_action",
                "description": (
                    f"Action '{r['action_taken']}' has only {rate}% success rate "
                    f"across {r['total']} attempts (${r['total_dollars']:,.2f})"
                ),
                "count": r["total"],
                "estimated_dollars": round(r["total_dollars"], 2),
                "recommendation": (
                    f"Re-evaluate when '{r['action_taken']}' is used. "
                    f"Consider alternative resolution paths."
                ),
            })

    # 4. Monthly trend — are denials increasing or decreasing?
    monthly = conn.execute(
        """SELECT strftime('%Y-%m', date) as month,
                  COUNT(*) as cnt,
                  COALESCE(SUM(dollar_amount), 0) as dollars
           FROM gap_report
           WHERE date >= ?
           GROUP BY month
           ORDER BY month""",
        (cutoff,),
    ).fetchall()

    if len(monthly) >= 3:
        first_3 = sum(r["cnt"] for r in monthly[:3])
        last_3 = sum(r["cnt"] for r in monthly[-3:])
        if last_3 > first_3 * 1.2:
            patterns.append({
                "pattern_type": "worsening_trend",
                "description": (
                    f"Denial volume is increasing: {first_3} (earliest 3 months) "
                    f"vs {last_3} (latest 3 months)"
                ),
                "count": last_3,
                "estimated_dollars": sum(r["dollars"] for r in monthly[-3:]),
                "recommendation": "Investigate what changed. Check for new MCO policies or staffing issues.",
            })
        elif last_3 < first_3 * 0.8:
            patterns.append({
                "pattern_type": "improving_trend",
                "description": (
                    f"Denial volume is decreasing: {first_3} (earliest 3 months) "
                    f"vs {last_3} (latest 3 months)"
                ),
                "count": last_3,
                "estimated_dollars": sum(r["dollars"] for r in monthly[-3:]),
                "recommendation": "Current approach is working. Continue monitoring.",
            })

    conn.close()
    return patterns


def estimate_financial_impact(proposed_changes: list) -> dict:
    """
    Given a list of proposed changes (from identify_patterns), estimate
    financial impact if those patterns were prevented.

    Args:
        proposed_changes: list of pattern dicts from identify_patterns()

    Returns dict:
      - total_preventable_dollars: float
      - total_preventable_claims: int
      - by_pattern_type: {type: {claims, dollars}}
      - top_opportunities: list of (description, dollars) sorted desc
    """
    total_dollars = 0.0
    total_claims = 0
    by_type: Dict[str, dict] = defaultdict(lambda: {"claims": 0, "dollars": 0.0})

    for change in proposed_changes:
        dollars = change.get("estimated_dollars", 0.0)
        claims = change.get("count", 0)
        ptype = change.get("pattern_type", "unknown")
        total_dollars += dollars
        total_claims += claims
        by_type[ptype]["claims"] += claims
        by_type[ptype]["dollars"] += dollars

    top_opps = sorted(
        [(c["description"], c["estimated_dollars"]) for c in proposed_changes],
        key=lambda x: x[1],
        reverse=True,
    )

    return {
        "total_preventable_dollars": round(total_dollars, 2),
        "total_preventable_claims": total_claims,
        "by_pattern_type": dict(by_type),
        "top_opportunities": top_opps[:10],
    }


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------

def _categorize_pattern(pattern_type: str) -> str:
    """Map pattern types from identify_patterns() to proposal categories."""
    mapping = {
        "recurring_client_denial": "pattern_update",
        "high_volume_gap": "rule_addition",
        "low_success_action": "workflow_change",
        "worsening_trend": "threshold_change",
        "improving_trend": "threshold_change",
    }
    return mapping.get(pattern_type, "pattern_update")


def generate_self_learning_report() -> str:
    """
    Generate a comprehensive self-learning report combining outcomes analysis,
    pattern detection, and financial impact estimates.

    Each improvement proposal is assigned a unique SLP-XXX ID, stored in the
    self_learning_proposals table, and formatted with approval instructions
    so nm@ can reply with APPROVE/REJECT commands.
    """
    _ensure_proposals_table()

    outcomes = analyze_decision_outcomes()
    patterns = identify_patterns()
    impact = estimate_financial_impact(patterns)

    # Build proposals from patterns and efficiency findings
    proposals = []

    for p in patterns:
        proposals.append({
            "proposal_text": f"{p['description']} — {p['recommendation']}",
            "financial_impact": p.get("estimated_dollars", 0.0),
            "category": _categorize_pattern(p["pattern_type"]),
        })

    # Efficiency proposals from action outcomes
    for action, data in outcomes["action_outcomes"].items():
        if data["total"] >= 20 and data["resolution_rate"] < 40:
            proposals.append({
                "proposal_text": (
                    f"'{action}' is attempted {data['total']}x/year with only "
                    f"{data['resolution_rate']:.1f}% success. Consider pre-screening "
                    f"or routing differently."
                ),
                "financial_impact": data.get("total_dollars", 0.0),
                "category": "workflow_change",
            })
    for action, data in outcomes["action_outcomes"].items():
        if data["total"] >= 10 and data["resolution_rate"] > 85:
            proposals.append({
                "proposal_text": (
                    f"'{action}' resolves {data['resolution_rate']:.1f}% of claims. "
                    f"Consider expanding its use."
                ),
                "financial_impact": data.get("total_dollars", 0.0),
                "category": "workflow_change",
            })

    # Assign SLP IDs and store in database
    stored_proposals = []
    for prop in proposals:
        slp_id = _next_proposal_id()
        _store_proposal(
            proposal_id=slp_id,
            proposal_text=prop["proposal_text"],
            financial_impact=prop["financial_impact"],
            category=prop["category"],
        )
        stored_proposals.append({
            "proposal_id": slp_id,
            "proposal_text": prop["proposal_text"],
            "financial_impact": prop["financial_impact"],
            "category": prop["category"],
        })

    # Build the report text
    lines = [
        "=" * 70,
        f"  LCI CLAIMS AUTOMATION — SELF-LEARNING REPORT",
        f"  Generated: {datetime.now().strftime('%m/%d/%Y %I:%M %p')}",
        f"  Analysis period: {_one_year_ago()} to {date.today().isoformat()}",
        "=" * 70,
        "",
    ]

    # --- Section 1: Decision Outcomes ---
    lines.append("SECTION 1: DECISION OUTCOMES")
    lines.append("-" * 50)

    if outcomes["action_outcomes"]:
        lines.append(f"{'Action':<35} {'Total':>8} {'Success':>8} {'Rate':>8} {'Dollars':>12}")
        lines.append("-" * 75)
        for action, data in sorted(
            outcomes["action_outcomes"].items(),
            key=lambda x: x[1]["total"],
            reverse=True,
        ):
            lines.append(
                f"{action:<35} {data['total']:>8} {data['success']:>8} "
                f"{data['resolution_rate']:>7.1f}% ${data['total_dollars']:>10,.2f}"
            )
    else:
        lines.append("  No historical action data available yet.")

    lines.append("")

    # --- Section 2: What's Working ---
    lines.append("SECTION 2: WHAT'S WORKING (Top Resolving Actions)")
    lines.append("-" * 50)
    if outcomes["top_resolving_actions"]:
        for action, rate in outcomes["top_resolving_actions"][:5]:
            status = "EXCELLENT" if rate >= 80 else ("GOOD" if rate >= 60 else "NEEDS IMPROVEMENT")
            lines.append(f"  {action}: {rate:.1f}% success — {status}")
    else:
        lines.append("  Not enough data yet.")
    lines.append("")

    # --- Section 3: What's NOT Working ---
    lines.append("SECTION 3: WHAT'S NOT WORKING (Lowest Resolution Rates)")
    lines.append("-" * 50)
    if outcomes["top_failing_actions"]:
        for action, rate in outcomes["top_failing_actions"][:5]:
            if rate < 50:
                lines.append(f"  WARNING: {action}: {rate:.1f}% success — needs attention")
    else:
        lines.append("  No low-performing actions detected.")
    lines.append("")

    # --- Section 4: Recurring Patterns ---
    lines.append("SECTION 4: RECURRING PATTERNS & PREVENTABLE DENIALS")
    lines.append("-" * 50)
    if patterns:
        for p in patterns:
            lines.append(f"  [{p['pattern_type'].upper()}]")
            lines.append(f"    {p['description']}")
            lines.append(f"    Recommendation: {p['recommendation']}")
            lines.append("")
    else:
        lines.append("  No significant recurring patterns detected.")
    lines.append("")

    # --- Section 5: Financial Impact ---
    lines.append("SECTION 5: ESTIMATED FINANCIAL IMPACT OF PROPOSED CHANGES")
    lines.append("-" * 50)
    lines.append(f"  Total preventable claims: {impact['total_preventable_claims']}")
    lines.append(f"  Total preventable dollars: ${impact['total_preventable_dollars']:,.2f}")
    lines.append("")

    if impact["top_opportunities"]:
        lines.append("  Top opportunities:")
        for desc, dollars in impact["top_opportunities"][:5]:
            lines.append(f"    ${dollars:>10,.2f} — {desc[:80]}")
    lines.append("")

    # --- Section 6: Proposals Requiring Approval ---
    lines.append("=" * 70)
    lines.append("SECTION 6: PROPOSALS REQUIRING YOUR APPROVAL")
    lines.append("=" * 70)
    lines.append("")

    if stored_proposals:
        for sp in stored_proposals:
            dollar_str = f"${sp['financial_impact']:,.2f}" if sp['financial_impact'] else "N/A"
            lines.append(f"PROPOSAL {sp['proposal_id']}: {sp['proposal_text']}")
            lines.append(f"  Category: {sp['category']}")
            lines.append(f"  Estimated Impact: {dollar_str}")
            lines.append(f'  To approve: Reply with "APPROVE {sp["proposal_id"]}"')
            lines.append(f'  To reject:  Reply with "REJECT {sp["proposal_id"]}"')
            lines.append("")
        lines.append('To approve all proposals: Reply with "APPROVE ALL"')
        lines.append("")
    else:
        lines.append("  No new proposals this cycle.")
        lines.append("")

    lines.append("=" * 70)
    lines.append("This report is generated automatically. No changes have been made.")
    lines.append("Approved proposals are logged for developer implementation.")
    lines.append("ONLY nm@lifeconsultantsinc.org can approve or reject proposals.")
    lines.append("=" * 70)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def email_report(report_text: str) -> bool:
    """
    Email the self-learning report to ss@ and nm@ via Gmail SMTP.

    Uses AUTOMATION_EMAIL and AUTOMATION_EMAIL_PASSWORD from .env.
    Returns True on success, False on failure.
    """
    sender = os.getenv("AUTOMATION_EMAIL", "")
    password = os.getenv("AUTOMATION_EMAIL_PASSWORD", "")

    if not sender or not password:
        logger.error(
            "Cannot send self-learning report: "
            "AUTOMATION_EMAIL or AUTOMATION_EMAIL_PASSWORD not set in .env"
        )
        return False

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = ", ".join(REPORT_RECIPIENTS)
    msg["Subject"] = (
        f"LCI Claims Automation — Self-Learning Report "
        f"({date.today().strftime('%m/%d/%Y')})"
    )

    body = (
        "This is an automated self-learning report from the LCI Claims "
        "Automation system. Please review the analysis below.\n\n"
        "No changes have been made. All proposed changes require your "
        "approval before implementation.\n\n"
        f"{report_text}"
    )
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            server.sendmail(sender, REPORT_RECIPIENTS, msg.as_string())
        logger.info(
            "Self-learning report emailed successfully",
            recipients=REPORT_RECIPIENTS,
        )
        return True
    except Exception as e:
        logger.error("Failed to email self-learning report", error=str(e))
        return False


# ---------------------------------------------------------------------------
# Proposal tracking & email-based approval (Comment 8)
# ---------------------------------------------------------------------------
# Table: self_learning_proposals in data/claims_history.db
# Security: ONLY nm@lifeconsultantsinc.org can approve/reject proposals.
#           Any other sender is logged as REJECTED with "unauthorized sender".
#           No autonomous rule changes — approved proposals logged for developer.
# ---------------------------------------------------------------------------

def _ensure_proposals_table():
    """Create the self_learning_proposals table in claims_history.db."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS self_learning_proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            proposal_id TEXT UNIQUE NOT NULL,
            proposal_text TEXT NOT NULL,
            financial_impact REAL DEFAULT 0.0,
            category TEXT DEFAULT 'pattern_update',
            status TEXT DEFAULT 'pending',
            created_at TEXT NOT NULL,
            responded_at TEXT DEFAULT NULL,
            responded_by TEXT DEFAULT NULL,
            response_text TEXT DEFAULT NULL
        )
    """)
    conn.commit()
    conn.close()


def _next_proposal_id() -> str:
    """Generate the next sequential SLP-XXX proposal ID."""
    conn = sqlite3.connect(str(DB_PATH))
    row = conn.execute(
        "SELECT proposal_id FROM self_learning_proposals "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    conn.close()

    if row and row[0]:
        # Extract number from SLP-001 format
        match = re.search(r"SLP-(\d+)", row[0])
        if match:
            next_num = int(match.group(1)) + 1
            return f"SLP-{next_num:03d}"
    return "SLP-001"


def _store_proposal(
    proposal_id: str,
    proposal_text: str,
    financial_impact: float = 0.0,
    category: str = "pattern_update",
):
    """Insert a new proposal into self_learning_proposals."""
    conn = sqlite3.connect(str(DB_PATH))
    try:
        conn.execute(
            """INSERT OR IGNORE INTO self_learning_proposals
               (proposal_id, proposal_text, financial_impact, category,
                status, created_at)
               VALUES (?, ?, ?, ?, 'pending', ?)""",
            (proposal_id, proposal_text, financial_impact, category,
             datetime.now().isoformat()),
        )
        conn.commit()
        logger.info("Proposal stored", proposal_id=proposal_id, category=category)
    except Exception as e:
        logger.warning("Failed to store proposal", proposal_id=proposal_id, error=str(e))
    finally:
        conn.close()


def _extract_email_address(header_value: str) -> str:
    """Extract a clean email address from an email header like 'Name <email>'."""
    if "<" in header_value and ">" in header_value:
        return header_value.split("<")[1].split(">")[0].strip().lower()
    return header_value.strip().lower()


def _get_email_body(msg) -> str:
    """Extract plain text body from an email message."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    return payload.decode("utf-8", errors="replace")
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            return payload.decode("utf-8", errors="replace")
    return ""


def _send_clarification_email(
    original_subject: str,
    original_sender: str,
    body_received: str,
):
    """
    Send a clarification reply when nm@'s response is not a clear
    approve or reject. Asks nm@ to reply with APPROVED to confirm.
    """
    sender = os.getenv("AUTOMATION_EMAIL", "")
    password = os.getenv("AUTOMATION_EMAIL_PASSWORD", "")
    if not sender or not password:
        logger.warning("Cannot send clarification: no email credentials")
        return

    msg = MIMEMultipart()
    msg["From"] = sender
    msg["To"] = APPROVAL_AUTHORIZED_EMAIL
    msg["Cc"] = ", ".join(REPORT_RECIPIENTS)
    msg["Subject"] = f"Re: {original_subject}"

    clarification_body = (
        f"Hello,\n\n"
        f"The LCI Claims Automation system received your reply to the "
        f"Self-Learning Report, but could not determine a clear "
        f"APPROVE or REJECT instruction.\n\n"
        f"Your reply contained:\n"
        f"---\n"
        f"{body_received[:500]}\n"
        f"---\n\n"
        f"If you intended to approve, please reply with the word "
        f'"APPROVED" to confirm.\n\n'
        f"Valid commands:\n"
        f'  - "APPROVE SLP-XXX" — approve a specific proposal\n'
        f'  - "REJECT SLP-XXX" — reject a specific proposal\n'
        f'  - "APPROVE ALL" — approve all pending proposals\n\n'
        f"No changes have been made. This is an automated message.\n"
    )
    msg.attach(MIMEText(clarification_body, "plain"))

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
            server.ehlo()
            server.starttls()
            server.ehlo()
            server.login(sender, password)
            recipients = list(set([APPROVAL_AUTHORIZED_EMAIL] + REPORT_RECIPIENTS))
            server.sendmail(sender, recipients, msg.as_string())
        logger.info(
            "Clarification email sent to nm@",
            subject=msg["Subject"],
        )
    except Exception as e:
        logger.error("Failed to send clarification email", error=str(e))


def check_self_learning_approvals() -> List[dict]:
    """
    Check email for replies to self-learning report emails.

    Security rules:
      - ONLY nm@lifeconsultantsinc.org can approve/reject proposals.
      - Any other sender: logged as REJECTED with "unauthorized sender".
      - Parses for "APPROVE SLP-XXX", "REJECT SLP-XXX", or "APPROVE ALL".
      - If reply is unclear (no clear approve/reject), sends a clarification
        email and does NOT take action.
      - All approvals/rejections logged with who, when, what.

    Returns list of dicts for proposals that were approved or rejected.
    """
    _ensure_proposals_table()

    imap_user = os.getenv("AUTOMATION_EMAIL", "")
    imap_pass = os.getenv("AUTOMATION_EMAIL_PASSWORD", "")

    if not imap_user or not imap_pass:
        logger.warning("No email credentials for self-learning approval checking")
        return []

    results = []

    try:
        mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
        mail.login(imap_user, imap_pass)
        mail.select("INBOX")

        # Search for unread replies to self-learning reports
        _, msg_ids = mail.search(
            None,
            '(SUBJECT "Self-Learning Report" UNSEEN)'
        )

        if not msg_ids[0]:
            mail.logout()
            return []

        for msg_id in msg_ids[0].split():
            if not msg_id:
                continue

            _, msg_data = mail.fetch(msg_id, "(RFC822)")
            raw = msg_data[0][1]
            msg = email_lib.message_from_bytes(raw)

            sender_raw = msg.get("From", "")
            sender_email = _extract_email_address(sender_raw)
            subject = msg.get("Subject", "")
            body = _get_email_body(msg)
            body_upper = body.upper().strip()
            now_iso = datetime.now().isoformat()

            # -------------------------------------------------------
            # SECURITY CHECK: Must be from nm@lifeconsultantsinc.org
            # -------------------------------------------------------
            if sender_email != APPROVAL_AUTHORIZED_EMAIL:
                logger.warning(
                    "SECURITY: Unauthorized sender attempted self-learning approval",
                    sender=sender_email,
                    authorized=APPROVAL_AUTHORIZED_EMAIL,
                    subject=subject,
                )
                # Log rejection in database for every pending proposal mentioned
                conn = sqlite3.connect(str(DB_PATH))
                # Find any SLP-XXX references in the body
                slp_refs = re.findall(r"SLP-\d{3,}", body_upper)
                if slp_refs:
                    for slp_id in slp_refs:
                        conn.execute(
                            """INSERT INTO self_learning_proposals
                               (proposal_id, proposal_text, status, created_at,
                                responded_at, responded_by, response_text)
                               VALUES (?, ?, 'rejected', ?, ?, ?, ?)
                               ON CONFLICT(proposal_id) DO UPDATE SET
                                responded_at = excluded.responded_at,
                                responded_by = excluded.responded_by,
                                response_text = excluded.response_text""",
                            (slp_id,
                             f"Unauthorized approval attempt by {sender_email}",
                             now_iso, now_iso, sender_email,
                             f"REJECTED — unauthorized sender: {sender_email}"),
                        )
                else:
                    # General unauthorized attempt — just log it
                    logger.warning(
                        "Unauthorized sender reply with no SLP references",
                        sender=sender_email,
                        body_preview=body[:200],
                    )
                conn.commit()
                conn.close()
                # Mark as read so we don't reprocess
                mail.store(msg_id, "+FLAGS", "\\Seen")
                continue

            # -------------------------------------------------------
            # Parse the reply body for commands
            # -------------------------------------------------------

            # Check for "APPROVE ALL"
            approve_all = bool(re.search(r"\bAPPROVE\s+ALL\b", body_upper))

            # Find individual APPROVE SLP-XXX commands
            approve_matches = re.findall(r"\bAPPROVE\s+(SLP-\d{3,})\b", body_upper)

            # Find individual REJECT SLP-XXX commands
            reject_matches = re.findall(r"\bREJECT\s+(SLP-\d{3,})\b", body_upper)

            # Also accept bare "APPROVED" as confirmation (from clarification flow)
            bare_approved = bool(re.search(r"\bAPPROVED\b", body_upper))

            has_clear_command = (
                approve_all
                or approve_matches
                or reject_matches
                or bare_approved
            )

            if not has_clear_command:
                # Unclear reply — send clarification, do NOT take action
                logger.info(
                    "Self-learning reply from nm@ is unclear — sending clarification",
                    sender=sender_email,
                    body_preview=body[:300],
                )
                _send_clarification_email(
                    original_subject=subject,
                    original_sender=sender_email,
                    body_received=body,
                )
                # Mark as read
                mail.store(msg_id, "+FLAGS", "\\Seen")
                continue

            # -------------------------------------------------------
            # Process approvals and rejections
            # -------------------------------------------------------
            conn = sqlite3.connect(str(DB_PATH))
            conn.row_factory = sqlite3.Row

            if approve_all or bare_approved:
                # Approve ALL pending proposals
                pending = conn.execute(
                    "SELECT proposal_id, proposal_text FROM self_learning_proposals "
                    "WHERE status = 'pending'"
                ).fetchall()

                for row in pending:
                    pid = row["proposal_id"]
                    conn.execute(
                        """UPDATE self_learning_proposals SET
                            status = 'approved',
                            responded_at = ?,
                            responded_by = ?,
                            response_text = ?
                           WHERE proposal_id = ?""",
                        (now_iso, sender_email,
                         f"APPROVE ALL — full reply: {body[:500]}", pid),
                    )
                    results.append({
                        "proposal_id": pid,
                        "proposal_text": row["proposal_text"],
                        "action": "approved",
                        "approved_by": sender_email,
                    })
                    logger.info(
                        "Proposal APPROVED (APPROVE ALL)",
                        proposal_id=pid,
                        by=sender_email,
                    )

            # Process individual approvals (these may overlap with APPROVE ALL,
            # but the UPDATE is idempotent)
            for slp_id in approve_matches:
                row = conn.execute(
                    "SELECT proposal_id, proposal_text, status "
                    "FROM self_learning_proposals WHERE proposal_id = ?",
                    (slp_id,),
                ).fetchone()
                if row:
                    conn.execute(
                        """UPDATE self_learning_proposals SET
                            status = 'approved',
                            responded_at = ?,
                            responded_by = ?,
                            response_text = ?
                           WHERE proposal_id = ?""",
                        (now_iso, sender_email,
                         f"APPROVE {slp_id} — reply: {body[:500]}", slp_id),
                    )
                    results.append({
                        "proposal_id": slp_id,
                        "proposal_text": row["proposal_text"],
                        "action": "approved",
                        "approved_by": sender_email,
                    })
                    logger.info(
                        "Proposal APPROVED",
                        proposal_id=slp_id,
                        by=sender_email,
                    )
                else:
                    logger.warning(
                        "APPROVE command for unknown proposal",
                        proposal_id=slp_id,
                    )

            # Process individual rejections
            for slp_id in reject_matches:
                row = conn.execute(
                    "SELECT proposal_id, proposal_text, status "
                    "FROM self_learning_proposals WHERE proposal_id = ?",
                    (slp_id,),
                ).fetchone()
                if row:
                    conn.execute(
                        """UPDATE self_learning_proposals SET
                            status = 'rejected',
                            responded_at = ?,
                            responded_by = ?,
                            response_text = ?
                           WHERE proposal_id = ?""",
                        (now_iso, sender_email,
                         f"REJECT {slp_id} — reply: {body[:500]}", slp_id),
                    )
                    results.append({
                        "proposal_id": slp_id,
                        "proposal_text": row["proposal_text"],
                        "action": "rejected",
                        "rejected_by": sender_email,
                    })
                    logger.info(
                        "Proposal REJECTED",
                        proposal_id=slp_id,
                        by=sender_email,
                    )
                else:
                    logger.warning(
                        "REJECT command for unknown proposal",
                        proposal_id=slp_id,
                    )

            conn.commit()
            conn.close()

            # Mark email as read
            mail.store(msg_id, "+FLAGS", "\\Seen")

        mail.logout()

    except Exception as e:
        logger.warning("Self-learning approval email check failed", error=str(e))

    if results:
        logger.info(
            "Self-learning approval check complete",
            total_processed=len(results),
            approved=[r["proposal_id"] for r in results if r.get("action") == "approved"],
            rejected=[r["proposal_id"] for r in results if r.get("action") == "rejected"],
        )

    return results


def get_pending_proposals() -> List[dict]:
    """Get all pending (unapproved) proposals from self_learning_proposals."""
    _ensure_proposals_table()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM self_learning_proposals WHERE status = 'pending' "
        "ORDER BY created_at DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_all_proposals() -> List[dict]:
    """Get all proposals regardless of status."""
    _ensure_proposals_table()
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM self_learning_proposals ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# Legacy aliases for backward compatibility
def store_proposal(proposal_id: str, description: str,
                   category: str = "", financial_impact: str = ""):
    """Legacy wrapper — use _store_proposal instead."""
    try:
        impact_float = float(financial_impact) if financial_impact else 0.0
    except (ValueError, TypeError):
        impact_float = 0.0
    _store_proposal(proposal_id, description, impact_float, category)


def check_approval_replies() -> List[dict]:
    """Legacy alias for check_self_learning_approvals."""
    return check_self_learning_approvals()
