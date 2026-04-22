"""
reporting/outcome_tracker.py
------------------------------
Tracks every claim the automation touches and what happened — action taken,
outcome, resolution time, human intervention needed. Enables learning from
results and measuring autonomous resolution rate.

Key metrics:
  - Autonomous resolution rate (no human intervention)
  - Success rate by action type, MCO, denial code
  - Time to resolution
  - Dollar recovery rate
  - Pattern detection for systemic issues
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

from logging_utils.logger import get_logger

logger = get_logger("outcome_tracker")

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "claims_history.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)


def _ensure_tables():
    conn = sqlite3.connect(str(_DB_PATH))

    # Core outcome tracking table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS claim_outcomes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,
            pcn TEXT DEFAULT '',
            patient_name TEXT DEFAULT '',
            mco TEXT DEFAULT '',
            dos TEXT DEFAULT '',
            billed_amount REAL DEFAULT 0,
            denial_code TEXT DEFAULT '',
            denial_reason TEXT DEFAULT '',
            entity_key TEXT DEFAULT '',

            -- What the automation did
            action_taken TEXT DEFAULT '',
            action_detail TEXT DEFAULT '',
            human_intervention INTEGER DEFAULT 0,
            clickup_task_id TEXT DEFAULT '',

            -- Outcome (updated when we learn the result)
            outcome TEXT DEFAULT 'pending',
            paid_amount REAL DEFAULT 0,
            check_number TEXT DEFAULT '',
            resolution_days INTEGER DEFAULT 0,

            -- Timestamps
            action_date TEXT NOT NULL,
            outcome_date TEXT DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)

    # Daily summary snapshots for trend analysis
    conn.execute("""
        CREATE TABLE IF NOT EXISTS daily_resolution_summary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT NOT NULL,
            total_claims_processed INTEGER DEFAULT 0,
            autonomous_resolved INTEGER DEFAULT 0,
            human_review_needed INTEGER DEFAULT 0,
            already_paid INTEGER DEFAULT 0,
            already_written_off INTEGER DEFAULT 0,
            pending_still INTEGER DEFAULT 0,
            recon_submitted INTEGER DEFAULT 0,
            appeal_submitted INTEGER DEFAULT 0,
            corrections_made INTEGER DEFAULT 0,
            era_posting_needed INTEGER DEFAULT 0,
            insurance_mismatch INTEGER DEFAULT 0,
            total_dollars_recovered REAL DEFAULT 0,
            total_dollars_written_off REAL DEFAULT 0,
            total_dollars_outstanding REAL DEFAULT 0,
            autonomous_rate REAL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)

    # Pattern tracking — recurring issues by entity/MCO/denial
    conn.execute("""
        CREATE TABLE IF NOT EXISTS denial_patterns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            pattern_key TEXT NOT NULL,
            entity_key TEXT DEFAULT '',
            mco TEXT DEFAULT '',
            denial_code TEXT DEFAULT '',
            occurrence_count INTEGER DEFAULT 1,
            auto_resolved_count INTEGER DEFAULT 0,
            human_resolved_count INTEGER DEFAULT 0,
            write_off_count INTEGER DEFAULT 0,
            avg_resolution_days REAL DEFAULT 0,
            total_billed REAL DEFAULT 0,
            total_recovered REAL DEFAULT 0,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            recommendation TEXT DEFAULT ''
        )
    """)

    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_co_claim "
        "ON claim_outcomes(claim_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_co_pcn "
        "ON claim_outcomes(pcn)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_co_outcome "
        "ON claim_outcomes(outcome)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_co_action "
        "ON claim_outcomes(action_taken)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dp_key "
        "ON denial_patterns(pattern_key)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_drs_date "
        "ON daily_resolution_summary(run_date)"
    )
    conn.commit()
    conn.close()


_ensure_tables()


# ======================================================================
# Record an action taken on a claim
# ======================================================================

def record_action(
    claim_id: str,
    pcn: str = "",
    patient_name: str = "",
    mco: str = "",
    dos: str = "",
    billed_amount: float = 0.0,
    denial_code: str = "",
    denial_reason: str = "",
    entity_key: str = "",
    action_taken: str = "",
    action_detail: str = "",
    human_intervention: bool = False,
    clickup_task_id: str = "",
) -> int:
    """Record an action the automation took on a claim. Returns row ID."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(_DB_PATH))
    cursor = conn.execute(
        """INSERT INTO claim_outcomes
           (claim_id, pcn, patient_name, mco, dos, billed_amount,
            denial_code, denial_reason, entity_key,
            action_taken, action_detail, human_intervention,
            clickup_task_id, outcome, action_date, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)""",
        (
            claim_id, pcn, patient_name, mco, dos, billed_amount,
            denial_code, denial_reason, entity_key,
            action_taken, action_detail[:500],
            1 if human_intervention else 0,
            clickup_task_id, now, now, now,
        ),
    )
    row_id = cursor.lastrowid
    conn.commit()
    conn.close()

    logger.info(
        "Outcome recorded",
        claim_id=claim_id,
        action=action_taken,
        human=human_intervention,
    )
    return row_id


def record_outcome(
    claim_id: str,
    outcome: str,
    paid_amount: float = 0.0,
    check_number: str = "",
) -> None:
    """Update the outcome for a previously recorded action.

    outcome: 'paid', 'denied_again', 'written_off', 'pending'
    """
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(_DB_PATH))

    # Get action_date to calculate resolution_days
    row = conn.execute(
        "SELECT action_date FROM claim_outcomes WHERE claim_id = ? ORDER BY created_at DESC LIMIT 1",
        (claim_id,),
    ).fetchone()

    resolution_days = 0
    if row and row[0]:
        try:
            action_dt = datetime.fromisoformat(row[0])
            resolution_days = (datetime.now() - action_dt).days
        except Exception:
            pass

    conn.execute(
        """UPDATE claim_outcomes
           SET outcome = ?, paid_amount = ?, check_number = ?,
               resolution_days = ?, outcome_date = ?, updated_at = ?
           WHERE claim_id = ?
             AND outcome = 'pending'""",
        (outcome, paid_amount, check_number, resolution_days, now, now, claim_id),
    )
    conn.commit()
    conn.close()


# ======================================================================
# Update outcomes by checking Lauris AR for payments
# ======================================================================

def update_pending_outcomes() -> dict:
    """Check Lauris AR for payments on claims with pending outcomes.

    Returns summary of updates.
    """
    from sources.payer_claim_status import _check_lauris_ar

    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    pending = conn.execute(
        "SELECT DISTINCT claim_id, pcn FROM claim_outcomes WHERE outcome = 'pending'"
    ).fetchall()
    conn.close()

    result = {"checked": 0, "paid": 0, "still_pending": 0}

    for row in pending:
        pcn = row["pcn"]
        if not pcn:
            continue
        parts = pcn.upper().replace("CW", "").split("-")
        if len(parts) != 2:
            continue
        bs_id = parts[1]
        result["checked"] += 1

        ar = _check_lauris_ar(bs_id)
        if ar and ar.status == "paid":
            record_outcome(
                row["claim_id"],
                outcome="paid",
                paid_amount=ar.paid_amount,
                check_number=ar.check_number,
            )
            result["paid"] += 1
        else:
            result["still_pending"] += 1

    logger.info("Pending outcomes updated", **result)
    return result


# ======================================================================
# Update denial patterns
# ======================================================================

def update_denial_patterns() -> None:
    """Aggregate claim_outcomes into denial_patterns for trend analysis."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    # Get all unique patterns
    rows = conn.execute("""
        SELECT entity_key, mco, denial_code,
               COUNT(*) as total,
               SUM(CASE WHEN human_intervention = 0 AND outcome = 'paid' THEN 1 ELSE 0 END) as auto_resolved,
               SUM(CASE WHEN human_intervention = 1 AND outcome = 'paid' THEN 1 ELSE 0 END) as human_resolved,
               SUM(CASE WHEN outcome = 'written_off' THEN 1 ELSE 0 END) as written_off,
               AVG(CASE WHEN resolution_days > 0 THEN resolution_days END) as avg_days,
               SUM(billed_amount) as total_billed,
               SUM(paid_amount) as total_recovered,
               MIN(action_date) as first_seen,
               MAX(action_date) as last_seen
        FROM claim_outcomes
        WHERE denial_code != ''
        GROUP BY entity_key, mco, denial_code
    """).fetchall()

    now = datetime.now().isoformat()
    for row in rows:
        pattern_key = f"{row['entity_key']}_{row['mco']}_{row['denial_code']}"
        total = row["total"]
        auto_resolved = row["auto_resolved"] or 0
        human_resolved = row["human_resolved"] or 0
        written_off = row["written_off"] or 0

        # Generate recommendation
        recommendation = ""
        if total >= 5:
            auto_rate = auto_resolved / total if total > 0 else 0
            wo_rate = written_off / total if total > 0 else 0
            if auto_rate > 0.8:
                recommendation = "HIGH auto-resolve rate — automation handling well"
            elif wo_rate > 0.5:
                recommendation = "FREQUENT write-offs — investigate root cause"
            elif human_resolved > auto_resolved:
                recommendation = "Mostly human-resolved — review for automation opportunity"

        conn.execute(
            """INSERT OR REPLACE INTO denial_patterns
               (pattern_key, entity_key, mco, denial_code,
                occurrence_count, auto_resolved_count, human_resolved_count,
                write_off_count, avg_resolution_days, total_billed,
                total_recovered, first_seen, last_seen, recommendation)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pattern_key, row["entity_key"], row["mco"], row["denial_code"],
                total, auto_resolved, human_resolved,
                written_off, row["avg_days"] or 0,
                row["total_billed"] or 0, row["total_recovered"] or 0,
                row["first_seen"], row["last_seen"], recommendation,
            ),
        )

    conn.commit()
    conn.close()
    logger.info("Denial patterns updated", patterns=len(rows))


# ======================================================================
# Save daily resolution summary
# ======================================================================

def save_daily_summary(
    total_claims: int = 0,
    autonomous_resolved: int = 0,
    human_review: int = 0,
    already_paid: int = 0,
    already_written_off: int = 0,
    pending_still: int = 0,
    recon_submitted: int = 0,
    appeal_submitted: int = 0,
    corrections_made: int = 0,
    era_posting: int = 0,
    insurance_mismatch: int = 0,
    dollars_recovered: float = 0.0,
    dollars_written_off: float = 0.0,
    dollars_outstanding: float = 0.0,
) -> None:
    """Save a daily summary snapshot."""
    now = datetime.now().isoformat()
    today = date.today().isoformat()
    auto_rate = (
        autonomous_resolved / total_claims * 100
        if total_claims > 0 else 0.0
    )

    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """INSERT INTO daily_resolution_summary
           (run_date, total_claims_processed, autonomous_resolved,
            human_review_needed, already_paid, already_written_off,
            pending_still, recon_submitted, appeal_submitted,
            corrections_made, era_posting_needed, insurance_mismatch,
            total_dollars_recovered, total_dollars_written_off,
            total_dollars_outstanding, autonomous_rate, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            today, total_claims, autonomous_resolved,
            human_review, already_paid, already_written_off,
            pending_still, recon_submitted, appeal_submitted,
            corrections_made, era_posting, insurance_mismatch,
            dollars_recovered, dollars_written_off,
            dollars_outstanding, auto_rate, now,
        ),
    )
    conn.commit()
    conn.close()

    logger.info(
        "Daily summary saved",
        date=today,
        total=total_claims,
        autonomous=autonomous_resolved,
        auto_rate=f"{auto_rate:.1f}%",
    )


# ======================================================================
# Reports / Analytics
# ======================================================================

def get_autonomous_rate(days: int = 30) -> dict:
    """Get autonomous resolution rate for the last N days."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    row = conn.execute(
        """SELECT
               COUNT(*) as total,
               SUM(CASE WHEN human_intervention = 0 THEN 1 ELSE 0 END) as autonomous,
               SUM(CASE WHEN human_intervention = 1 THEN 1 ELSE 0 END) as human,
               SUM(CASE WHEN outcome = 'paid' AND human_intervention = 0 THEN 1 ELSE 0 END) as auto_paid,
               SUM(CASE WHEN outcome = 'paid' THEN paid_amount ELSE 0 END) as recovered,
               SUM(CASE WHEN outcome = 'written_off' THEN billed_amount ELSE 0 END) as written_off,
               SUM(CASE WHEN outcome = 'pending' THEN billed_amount ELSE 0 END) as outstanding
           FROM claim_outcomes
           WHERE action_date >= ?""",
        (cutoff,),
    ).fetchone()
    conn.close()

    total = row["total"] or 0
    autonomous = row["autonomous"] or 0
    return {
        "period_days": days,
        "total_claims": total,
        "autonomous_actions": autonomous,
        "human_actions": row["human"] or 0,
        "autonomous_rate": (
            f"{autonomous / total * 100:.1f}%" if total > 0 else "N/A"
        ),
        "auto_paid": row["auto_paid"] or 0,
        "dollars_recovered": row["recovered"] or 0,
        "dollars_written_off": row["written_off"] or 0,
        "dollars_outstanding": row["outstanding"] or 0,
    }


def get_success_by_action(days: int = 30) -> list[dict]:
    """Success rate broken down by action type."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT action_taken,
               COUNT(*) as total,
               SUM(CASE WHEN outcome = 'paid' THEN 1 ELSE 0 END) as paid,
               SUM(CASE WHEN outcome = 'written_off' THEN 1 ELSE 0 END) as wo,
               SUM(CASE WHEN outcome = 'pending' THEN 1 ELSE 0 END) as pending,
               AVG(CASE WHEN resolution_days > 0 THEN resolution_days END) as avg_days
           FROM claim_outcomes
           WHERE action_date >= ?
           GROUP BY action_taken
           ORDER BY total DESC""",
        (cutoff,),
    ).fetchall()
    conn.close()

    return [
        {
            "action": r["action_taken"],
            "total": r["total"],
            "paid": r["paid"] or 0,
            "written_off": r["wo"] or 0,
            "pending": r["pending"] or 0,
            "success_rate": (
                f"{(r['paid'] or 0) / r['total'] * 100:.0f}%"
                if r["total"] > 0 else "N/A"
            ),
            "avg_resolution_days": round(r["avg_days"] or 0, 1),
        }
        for r in rows
    ]


def get_success_by_mco(days: int = 30) -> list[dict]:
    """Success rate broken down by MCO."""
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT mco,
               COUNT(*) as total,
               SUM(CASE WHEN outcome = 'paid' THEN 1 ELSE 0 END) as paid,
               SUM(CASE WHEN outcome = 'paid' THEN paid_amount ELSE 0 END) as recovered,
               SUM(CASE WHEN human_intervention = 0 THEN 1 ELSE 0 END) as autonomous
           FROM claim_outcomes
           WHERE action_date >= ?
           GROUP BY mco
           ORDER BY total DESC""",
        (cutoff,),
    ).fetchall()
    conn.close()

    return [
        {
            "mco": r["mco"],
            "total": r["total"],
            "paid": r["paid"] or 0,
            "recovered": r["recovered"] or 0,
            "autonomous": r["autonomous"] or 0,
            "auto_rate": (
                f"{(r['autonomous'] or 0) / r['total'] * 100:.0f}%"
                if r["total"] > 0 else "N/A"
            ),
        }
        for r in rows
    ]


def get_trending_denials(min_occurrences: int = 3) -> list[dict]:
    """Get denial patterns that are trending (recurring issues)."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    rows = conn.execute(
        """SELECT * FROM denial_patterns
           WHERE occurrence_count >= ?
           ORDER BY occurrence_count DESC""",
        (min_occurrences,),
    ).fetchall()
    conn.close()

    return [dict(r) for r in rows]


def generate_report(days: int = 30) -> str:
    """Generate a text report of automation performance."""
    rate = get_autonomous_rate(days)
    by_action = get_success_by_action(days)
    by_mco = get_success_by_mco(days)
    patterns = get_trending_denials()

    lines = [
        f"=== Claims Automation Performance Report ({days} days) ===",
        "",
        f"Total claims processed: {rate['total_claims']}",
        f"Autonomous (no human): {rate['autonomous_actions']} ({rate['autonomous_rate']})",
        f"Human intervention: {rate['human_actions']}",
        f"Auto-resolved & paid: {rate['auto_paid']}",
        f"Dollars recovered: ${rate['dollars_recovered']:,.2f}",
        f"Dollars written off: ${rate['dollars_written_off']:,.2f}",
        f"Dollars outstanding: ${rate['dollars_outstanding']:,.2f}",
        "",
        "--- By Action Type ---",
    ]
    for a in by_action:
        lines.append(
            f"  {a['action']}: {a['total']} total, "
            f"{a['success_rate']} paid, "
            f"avg {a['avg_resolution_days']} days"
        )

    lines.append("")
    lines.append("--- By MCO ---")
    for m in by_mco:
        lines.append(
            f"  {m['mco']}: {m['total']} total, "
            f"{m['paid']} paid (${m['recovered']:,.2f}), "
            f"{m['auto_rate']} autonomous"
        )

    if patterns:
        lines.append("")
        lines.append("--- Trending Denial Patterns ---")
        for p in patterns[:10]:
            lines.append(
                f"  {p['entity_key']}/{p['mco']}/{p['denial_code']}: "
                f"{p['occurrence_count']}x, "
                f"auto={p['auto_resolved_count']}, "
                f"human={p['human_resolved_count']}, "
                f"wo={p['write_off_count']}"
            )
            if p.get("recommendation"):
                lines.append(f"    → {p['recommendation']}")

    return "\n".join(lines)
