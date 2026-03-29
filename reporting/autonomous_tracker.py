"""
reporting/autonomous_tracker.py
-------------------------------
Tracks autonomous corrections that resolved claims without human intervention.

Logs every auto-correction (entity fix, NPI fix, diagnosis fix, resubmission, etc.)
and later checks whether those corrections resulted in the claim being paid.

Uses the shared claims_history.db SQLite database.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional

from logging_utils.logger import get_logger

logger = get_logger("autonomous_tracker")

DB_DIR = Path(__file__).resolve().parent.parent / "data"
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "claims_history.db"

# Valid correction types
CORRECTION_TYPES = {
    "entity_fix",
    "npi_fix",
    "member_id_fix",
    "mhss_rate_fix",
    "diagnosis_fix",
    "auth_added",
    "rendering_npi_added",
    "resubmitted",
    "reconsideration_submitted",
}


def _get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def _ensure_table():
    """Create the autonomous_corrections table if it doesn't exist."""
    try:
        conn = _get_conn()
        conn.execute("""
            CREATE TABLE IF NOT EXISTS autonomous_corrections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id TEXT NOT NULL,
                client_name TEXT NOT NULL DEFAULT '',
                client_id TEXT NOT NULL DEFAULT '',
                correction_type TEXT NOT NULL,
                correction_detail TEXT NOT NULL DEFAULT '',
                dollars_at_stake REAL DEFAULT 0.0,
                resolved INTEGER DEFAULT 0,
                resolved_date TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ac_claim ON autonomous_corrections(claim_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ac_type ON autonomous_corrections(correction_type)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ac_resolved ON autonomous_corrections(resolved)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_ac_created ON autonomous_corrections(created_at)")
        conn.commit()
        conn.close()
    except sqlite3.OperationalError:
        pass  # DB locked by another process — table will be created on next call


# Initialize table on import
_ensure_table()


# ---------------------------------------------------------------------------
# Logging corrections
# ---------------------------------------------------------------------------

def log_autonomous_correction(
    claim_id: str,
    client_name: str,
    client_id: str,
    correction_type: str,
    correction_detail: str,
    dollars_at_stake: float = 0.0,
) -> int:
    """
    Log an autonomous correction to the database.

    Args:
        claim_id: The Claim.MD claim ID
        client_name: Patient name
        client_id: Medicaid / member ID
        correction_type: One of CORRECTION_TYPES
        correction_detail: What was changed (e.g., "NPI corrected from X to Y")
        dollars_at_stake: Billed amount of the claim

    Returns:
        Row ID of the inserted record.
    """
    if correction_type not in CORRECTION_TYPES:
        logger.warning(
            "Unknown correction type — logging anyway",
            correction_type=correction_type,
            claim_id=claim_id,
        )

    conn = _get_conn()
    cur = conn.execute(
        """INSERT INTO autonomous_corrections
           (claim_id, client_name, client_id, correction_type,
            correction_detail, dollars_at_stake, resolved,
            resolved_date, created_at)
           VALUES (?, ?, ?, ?, ?, ?, 0, '', ?)""",
        (
            claim_id,
            client_name,
            client_id,
            correction_type,
            correction_detail,
            dollars_at_stake,
            datetime.now().isoformat(),
        ),
    )
    conn.commit()
    row_id = cur.lastrowid
    conn.close()

    logger.info(
        "Autonomous correction logged",
        claim_id=claim_id,
        correction_type=correction_type,
        dollars=dollars_at_stake,
    )
    return row_id


# ---------------------------------------------------------------------------
# Checking for resolved corrections
# ---------------------------------------------------------------------------

async def check_resolved_corrections():
    """
    Query unresolved autonomous corrections and check Claim.MD API
    to see if the claim status changed to 'A' (Accepted/Paid).

    If paid, marks as resolved with the date.
    Should be called as part of daily orchestration.
    """
    from sources.claimmd_api import ClaimMDAPI

    conn = _get_conn()
    unresolved = conn.execute(
        """SELECT id, claim_id, correction_type, dollars_at_stake
           FROM autonomous_corrections
           WHERE resolved = 0"""
    ).fetchall()
    conn.close()

    if not unresolved:
        logger.debug("No unresolved autonomous corrections to check")
        return {"checked": 0, "resolved": 0, "dollars_recovered": 0.0}

    api = ClaimMDAPI()
    if not api.key:
        logger.warning("No Claim.MD API key — cannot check resolved corrections")
        return {"checked": 0, "resolved": 0, "dollars_recovered": 0.0}

    resolved_count = 0
    dollars_recovered = 0.0
    checked = 0

    # Group by claim_id to avoid duplicate API calls
    claim_ids = list(set(row["claim_id"] for row in unresolved))

    for claim_id in claim_ids:
        checked += 1
        try:
            responses = await api.get_claim_responses(
                response_id="0", claim_id=claim_id
            )
            # Check if any response shows accepted/paid status
            is_paid = False
            for resp in responses:
                status = resp.get("status", "")
                # "A" = Accepted, "1" = Accepted in some formats,
                # "P" = Paid in some formats
                if status in ("A", "1", "P"):
                    is_paid = True
                    break

            if is_paid:
                # Mark all corrections for this claim as resolved
                conn = _get_conn()
                conn.execute(
                    """UPDATE autonomous_corrections
                       SET resolved = 1, resolved_date = ?
                       WHERE claim_id = ? AND resolved = 0""",
                    (date.today().isoformat(), claim_id),
                )
                conn.commit()
                conn.close()

                # Sum up dollars for this claim
                claim_dollars = sum(
                    row["dollars_at_stake"]
                    for row in unresolved
                    if row["claim_id"] == claim_id
                )
                dollars_recovered += claim_dollars
                resolved_count += sum(
                    1 for row in unresolved if row["claim_id"] == claim_id
                )

                logger.info(
                    "Autonomous correction resolved — claim paid",
                    claim_id=claim_id,
                    dollars=claim_dollars,
                )

        except Exception as e:
            logger.warning(
                "Failed to check claim status",
                claim_id=claim_id,
                error=str(e),
            )

    logger.info(
        "Resolved corrections check complete",
        checked=checked,
        resolved=resolved_count,
        dollars_recovered=round(dollars_recovered, 2),
    )

    return {
        "checked": checked,
        "resolved": resolved_count,
        "dollars_recovered": round(dollars_recovered, 2),
    }


# ---------------------------------------------------------------------------
# Reporting queries
# ---------------------------------------------------------------------------

def get_correction_stats(days: int = 30) -> dict:
    """
    Get autonomous correction statistics for the scorecard.

    Returns:
        {
            "total_corrected": int,
            "total_resolved": int,
            "total_dollars_at_stake": float,
            "total_dollars_recovered": float,
            "by_type": {
                "entity_fix": {"corrected": X, "resolved": X},
                ...
            },
            "auto_fix_rate": float,    # corrections / total claims (needs total_claims param)
            "resolution_rate": float,  # resolved / total corrections
        }
    """
    cutoff = (date.today() - timedelta(days=days)).isoformat()
    conn = _get_conn()

    # Overall counts
    totals = conn.execute(
        """SELECT
               COUNT(*) as total_corrected,
               SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as total_resolved,
               COALESCE(SUM(dollars_at_stake), 0) as total_dollars,
               COALESCE(SUM(CASE WHEN resolved = 1 THEN dollars_at_stake ELSE 0 END), 0) as recovered
           FROM autonomous_corrections
           WHERE created_at >= ?""",
        (cutoff,),
    ).fetchone()

    # By correction type
    by_type_rows = conn.execute(
        """SELECT correction_type,
                  COUNT(*) as corrected,
                  SUM(CASE WHEN resolved = 1 THEN 1 ELSE 0 END) as resolved
           FROM autonomous_corrections
           WHERE created_at >= ?
           GROUP BY correction_type
           ORDER BY corrected DESC""",
        (cutoff,),
    ).fetchall()

    conn.close()

    total_corrected = totals["total_corrected"]
    total_resolved = totals["total_resolved"]
    resolution_rate = round(
        (total_resolved / total_corrected * 100), 1
    ) if total_corrected > 0 else 0.0

    by_type = {}
    for row in by_type_rows:
        by_type[row["correction_type"]] = {
            "corrected": row["corrected"],
            "resolved": row["resolved"],
        }

    return {
        "total_corrected": total_corrected,
        "total_resolved": total_resolved,
        "total_dollars_at_stake": round(totals["total_dollars"], 2),
        "total_dollars_recovered": round(totals["recovered"], 2),
        "by_type": by_type,
        "resolution_rate": resolution_rate,
    }


def get_daily_correction_summary() -> dict:
    """
    Get today's autonomous correction counts for the daily ClickUp summary.

    Returns:
        {
            "total": int,
            "by_type": {"entity_fix": X, "npi_fix": X, ...},
        }
    """
    today = date.today().isoformat()
    conn = _get_conn()

    rows = conn.execute(
        """SELECT correction_type, COUNT(*) as cnt
           FROM autonomous_corrections
           WHERE DATE(created_at) = ?
           GROUP BY correction_type""",
        (today,),
    ).fetchall()
    conn.close()

    by_type = {row["correction_type"]: row["cnt"] for row in rows}
    total = sum(by_type.values())

    return {"total": total, "by_type": by_type}
