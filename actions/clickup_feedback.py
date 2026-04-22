"""
actions/clickup_feedback.py
-----------------------------
ClickUp feedback loop — tracks claim→task mappings, polls for staff
responses, parses structured replies, and enables the automation to
act on completed tasks.

Key rules:
  - Same claim re-encountered → reopen/comment on existing task (no duplicates)
  - Automation does NOT act until task status is complete/closed
  - Staff may communicate with each other before closing — respect that
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

import aiohttp

from config.settings import CLICKUP_API_TOKEN
from logging_utils.logger import get_logger

logger = get_logger("clickup_feedback")

_DB_PATH = Path(__file__).resolve().parent.parent / "data" / "claims_history.db"
_DB_PATH.parent.mkdir(parents=True, exist_ok=True)

CLICKUP_BASE = "https://api.clickup.com/api/v2"
DONE_STATUSES = {"complete", "closed", "done", "resolved"}
TASK_DUE_DAYS = 3  # Tasks are due 3 days after creation/reopen

# Response instruction block appended to auth-related ClickUp tasks
RESPONSE_INSTRUCTIONS = {
    "auth_verification": (
        "\n\n--- STAFF RESPONSE NEEDED ---\n"
        "After verifying in the MCO portal, please comment with:\n"
        "  Auth: [verified auth number]\n"
        "  Entity: [KJLN / NHCS / Mary's Home]\n"
        "  Action: [resubmit / write off / rebill / appeal]\n"
        "Then mark this task as Complete."
    ),
    "era_posting": (
        "\n\n--- STAFF RESPONSE NEEDED ---\n"
        "After verifying the ERA, please comment with:\n"
        "  Action: [posted / write off / needs investigation]\n"
        "Then mark this task as Complete."
    ),
    "insurance_mismatch": (
        "\n\n--- STAFF RESPONSE NEEDED ---\n"
        "After verifying the client's coverage, please comment with:\n"
        "  Auth: [auth number if found]\n"
        "  Entity: [correct entity: KJLN / NHCS / Mary's Home]\n"
        "  Action: [rebill / write off]\n"
        "Then mark this task as Complete."
    ),
    "recon_submitted": (
        "\n\n--- FOR TRACKING ---\n"
        "Reconsideration was submitted automatically.\n"
        "If recon is denied or no response within 30 days,\n"
        "comment: Action: appeal\n"
        "Then mark this task as Complete."
    ),
    "lauris_fix": (
        "\n\n--- STAFF RESPONSE NEEDED ---\n"
        "After updating Lauris, comment with:\n"
        "  Action: [fixed / write off]\n"
        "Then mark this task as Complete."
    ),
}


def _ensure_table():
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clickup_claim_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,
            pcn TEXT DEFAULT '',
            task_id TEXT NOT NULL,
            task_type TEXT DEFAULT '',
            patient_name TEXT DEFAULT '',
            patient_key TEXT DEFAULT '',
            mco TEXT DEFAULT '',
            handler_context TEXT DEFAULT '{}',
            status TEXT DEFAULT 'pending_staff',
            staff_response TEXT DEFAULT '',
            parsed_auth TEXT DEFAULT '',
            parsed_entity TEXT DEFAULT '',
            parsed_action TEXT DEFAULT '',
            responded_by TEXT DEFAULT '',
            response_date TEXT DEFAULT '',
            encounter_count INTEGER DEFAULT 1,
            last_encounter TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cct_claim "
        "ON clickup_claim_tasks(claim_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cct_pcn "
        "ON clickup_claim_tasks(pcn)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cct_status "
        "ON clickup_claim_tasks(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_cct_task "
        "ON clickup_claim_tasks(task_id)"
    )
    conn.commit()
    conn.close()


_ensure_table()


# ======================================================================
# Record a new claim → task mapping
# ======================================================================

def record_claim_task(
    claim_id: str,
    pcn: str,
    task_id: str,
    task_type: str,
    patient_name: str = "",
    patient_key: str = "",
    mco: str = "",
    handler_context: dict | None = None,
) -> None:
    """Record a ClickUp task created for a claim."""
    now = datetime.now().isoformat()
    ctx = json.dumps(handler_context or {})
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """INSERT INTO clickup_claim_tasks
           (claim_id, pcn, task_id, task_type, patient_name, patient_key,
            mco, handler_context, status, encounter_count,
            last_encounter, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending_staff', 1, ?, ?, ?)""",
        (claim_id, pcn, task_id, task_type, patient_name, patient_key,
         mco, ctx, now, now, now),
    )
    conn.commit()
    conn.close()
    logger.info(
        "Claim task recorded",
        claim_id=claim_id,
        pcn=pcn,
        task_id=task_id,
        task_type=task_type,
    )


# ======================================================================
# Check if a claim already has a pending ClickUp task
# ======================================================================

def check_claim_has_pending_task(
    claim_id: str, pcn: str = "",
) -> dict | None:
    """Check if there's an existing ClickUp task for this claim.

    Returns:
      None — no pending/responded task exists (create new if needed)
      {"status": "pending_staff", "task_id": ..., ...} — task open, skip
      {"status": "staff_responded", ..., parsed fields} — ready to process
    """
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row

    # Search by claim_id first, then by PCN
    row = conn.execute(
        """SELECT * FROM clickup_claim_tasks
           WHERE (claim_id = ? OR pcn = ?)
             AND status IN ('pending_staff', 'staff_responded')
           ORDER BY created_at DESC LIMIT 1""",
        (claim_id, pcn or claim_id),
    ).fetchone()

    if not row:
        conn.close()
        return None

    result = dict(row)
    conn.close()
    return result


async def reopen_existing_task(
    task_id: str,
    claim_id: str,
    pcn: str,
    new_info: str,
) -> bool:
    """Reopen/comment on an existing ClickUp task and bump encounter count.

    If the task was closed, reopens it and sets due date to 3 business
    days from now. If still open, just adds the comment.
    """
    if not CLICKUP_API_TOKEN:
        return False

    headers = {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }

    try:
        async with aiohttp.ClientSession() as session:
            # Check current task status
            async with session.get(
                f"{CLICKUP_BASE}/task/{task_id}",
                headers=headers,
            ) as resp:
                if resp.status == 200:
                    task_data = await resp.json()
                    task_status = (
                        task_data.get("status", {}).get("status", "")
                        .lower().strip()
                    )
                    # If closed, reopen it with new 3-day due date
                    if task_status in DONE_STATUSES:
                        new_due = _due_date_ms_from_now(TASK_DUE_DAYS)
                        await _update_task(
                            session, headers, task_id,
                            {
                                "status": "to do",
                                "due_date": new_due,
                                "due_date_time": True,
                            },
                        )
                        logger.info(
                            "Reopened closed ClickUp task",
                            task_id=task_id,
                        )

            # Add comment
            await _add_comment(
                session, headers, task_id,
                f"Automation re-encountered this claim on "
                f"{datetime.now().strftime('%m/%d/%Y')}.\n\n"
                f"{new_info}",
            )
    except Exception as exc:
        logger.warning("ClickUp reopen/comment failed", error=str(exc)[:100])
        return False

    # Update encounter count in DB
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """UPDATE clickup_claim_tasks
           SET encounter_count = encounter_count + 1,
               last_encounter = ?, updated_at = ?
           WHERE (claim_id = ? OR pcn = ?)
             AND status = 'pending_staff'""",
        (now, now, claim_id, pcn or claim_id),
    )
    conn.commit()
    conn.close()

    logger.info(
        "Existing ClickUp task updated with re-encounter",
        task_id=task_id,
        claim_id=claim_id,
    )
    return True


# ======================================================================
# Poll ClickUp for completed tasks with staff responses
# ======================================================================

async def poll_claim_feedback() -> dict:
    """Poll ClickUp for completed auth-related tasks.

    Checks tasks where status is 'pending_staff' in our DB:
    - If ClickUp task is closed WITH actionable info → mark as staff_responded
    - If ClickUp task is closed WITHOUT info → reopen task, keep original due date
    - If ClickUp task is open but missing/wrong due date → set to 3 days out

    Returns summary dict.
    """
    result = {
        "polled": 0, "responded": 0, "reopened": 0,
        "due_date_fixed": 0, "still_pending": 0, "errors": 0,
    }

    if not CLICKUP_API_TOKEN:
        return result

    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    pending = conn.execute(
        "SELECT * FROM clickup_claim_tasks WHERE status = 'pending_staff'"
    ).fetchall()
    conn.close()

    result["polled"] = len(pending)
    headers = {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        for row in pending:
            task_id = row["task_id"]
            try:
                # Get task details
                async with session.get(
                    f"{CLICKUP_BASE}/task/{task_id}",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        result["errors"] += 1
                        continue
                    task_data = await resp.json()

                task_status = (
                    task_data.get("status", {}).get("status", "")
                    .lower().strip()
                )
                due_date_ms = task_data.get("due_date")

                # --------------------------------------------------
                # Task still open — check due date
                # --------------------------------------------------
                if task_status not in DONE_STATUSES:
                    result["still_pending"] += 1

                    # Ensure due date is set (3 days from creation)
                    if not due_date_ms:
                        new_due = _due_date_ms_from_now(TASK_DUE_DAYS)
                        await _update_task(
                            session, headers, task_id,
                            {"due_date": new_due, "due_date_time": True},
                        )
                        result["due_date_fixed"] += 1
                        logger.info(
                            "Set missing due date on task",
                            task_id=task_id,
                        )
                    continue

                # --------------------------------------------------
                # Task is closed — get comments and check for info
                # --------------------------------------------------
                async with session.get(
                    f"{CLICKUP_BASE}/task/{task_id}/comment",
                    headers=headers,
                ) as resp:
                    if resp.status != 200:
                        result["errors"] += 1
                        continue
                    comments_data = await resp.json()

                comments = comments_data.get("comments", [])
                staff_comments = _extract_staff_comments(comments)
                all_text = "\n".join(sc["text"] for sc in staff_comments)
                parsed = parse_staff_response(all_text)

                has_actionable_info = bool(
                    parsed.get("action")
                    or parsed.get("auth")
                    or parsed.get("entity")
                )

                # --------------------------------------------------
                # Closed WITHOUT info → reopen, keep original due date
                # --------------------------------------------------
                if not has_actionable_info:
                    logger.info(
                        "Task closed without actionable info — reopening",
                        task_id=task_id,
                        claim_id=row["claim_id"],
                    )
                    # Reopen the task (set to "open" or "to do" status)
                    await _reopen_task(session, headers, task_id)
                    # Add comment explaining why it was reopened
                    await _add_comment(
                        session, headers, task_id,
                        "This task was closed without the required "
                        "information. Please provide the needed details "
                        "and then mark as Complete.\n\n"
                        "Required: Auth #, Entity (KJLN/NHCS/Mary's Home), "
                        "and Action (resubmit/write off/rebill/appeal)."
                    )
                    result["reopened"] += 1
                    continue

                # --------------------------------------------------
                # Closed WITH info → capture response
                # --------------------------------------------------
                responded_by = (
                    staff_comments[-1]["user"] if staff_comments else ""
                )
                now = datetime.now().isoformat()
                conn = sqlite3.connect(str(_DB_PATH))
                conn.execute(
                    """UPDATE clickup_claim_tasks
                       SET status = 'staff_responded',
                           staff_response = ?,
                           parsed_auth = ?,
                           parsed_entity = ?,
                           parsed_action = ?,
                           responded_by = ?,
                           response_date = ?,
                           updated_at = ?
                       WHERE task_id = ?""",
                    (
                        all_text[:2000],
                        parsed.get("auth", ""),
                        parsed.get("entity", ""),
                        parsed.get("action", ""),
                        responded_by,
                        now,
                        now,
                        task_id,
                    ),
                )
                conn.commit()
                conn.close()

                result["responded"] += 1
                logger.info(
                    "Staff response captured",
                    task_id=task_id,
                    claim_id=row["claim_id"],
                    parsed_auth=parsed.get("auth", ""),
                    parsed_entity=parsed.get("entity", ""),
                    parsed_action=parsed.get("action", ""),
                )

            except Exception as exc:
                result["errors"] += 1
                logger.warning(
                    "Poll error for task",
                    task_id=task_id,
                    error=str(exc)[:100],
                )

    return result


def _extract_staff_comments(comments: list) -> list[dict]:
    """Extract non-automation comments from ClickUp comment list."""
    staff = []
    for c in comments:
        poster = c.get("user", {}).get("username", "")
        text = ""
        for part in c.get("comment", []):
            if part.get("type") == "text":
                text += part.get("text", "")
        if text.strip() and "AUTO" not in poster.upper():
            staff.append({
                "text": text.strip(),
                "user": poster,
                "date": c.get("date", ""),
            })
    return staff


def _due_date_ms_from_now(days: int) -> int:
    """Return a ClickUp timestamp (Unix ms) for N business days from now."""
    from datetime import timedelta
    d = datetime.now()
    added = 0
    while added < days:
        d += timedelta(days=1)
        if d.weekday() < 5:  # Mon-Fri
            added += 1
    # Set to 5 PM
    d = d.replace(hour=17, minute=0, second=0, microsecond=0)
    return int(d.timestamp() * 1000)


async def _update_task(
    session: aiohttp.ClientSession,
    headers: dict,
    task_id: str,
    updates: dict,
) -> bool:
    """Update a ClickUp task's fields."""
    try:
        async with session.put(
            f"{CLICKUP_BASE}/task/{task_id}",
            json=updates,
            headers=headers,
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _reopen_task(
    session: aiohttp.ClientSession,
    headers: dict,
    task_id: str,
) -> bool:
    """Reopen a closed ClickUp task by setting status to 'to do'."""
    return await _update_task(
        session, headers, task_id, {"status": "to do"},
    )


async def _add_comment(
    session: aiohttp.ClientSession,
    headers: dict,
    task_id: str,
    text: str,
) -> bool:
    """Add a comment to a ClickUp task."""
    try:
        async with session.post(
            f"{CLICKUP_BASE}/task/{task_id}/comment",
            json={"comment_text": text, "notify_all": True},
            headers=headers,
        ) as resp:
            return resp.status in (200, 201)
    except Exception:
        return False


def mark_task_processed(task_id: str) -> None:
    """Mark a task as processed after the automation has acted on it."""
    now = datetime.now().isoformat()
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """UPDATE clickup_claim_tasks
           SET status = 'processed', updated_at = ?
           WHERE task_id = ?""",
        (now, task_id),
    )
    conn.commit()
    conn.close()


# ======================================================================
# Parse structured staff responses
# ======================================================================

def parse_staff_response(text: str) -> dict:
    """Parse staff comment text for auth, entity, and action.

    Handles formats like:
      Auth: UM12345
      Entity: KJLN
      Action: resubmit

    Also handles free-form text with embedded auth numbers.
    """
    result = {"auth": "", "entity": "", "action": ""}
    if not text:
        return result

    lines = text.strip()

    # Auth number
    auth_match = re.search(
        r"(?:auth(?:orization)?|cert)\s*[:#]?\s*([A-Z0-9]{5,20})",
        lines, re.IGNORECASE,
    )
    if auth_match:
        result["auth"] = auth_match.group(1).strip()
    else:
        # Bare auth number pattern
        bare = re.search(r"\b([A-Z]{1,3}\d{6,14})\b", lines)
        if bare:
            result["auth"] = bare.group(1)

    # Entity
    entity_match = re.search(
        r"(?:entity|company)\s*[:#]?\s*(kjln|nhcs|mary'?s?\s*home)",
        lines, re.IGNORECASE,
    )
    if entity_match:
        raw = entity_match.group(1).strip().upper()
        if "KJLN" in raw:
            result["entity"] = "KJLN"
        elif "NHCS" in raw:
            result["entity"] = "NHCS"
        elif "MARY" in raw:
            result["entity"] = "MARYS_HOME"
    else:
        # Check for entity names anywhere in text
        upper = lines.upper()
        if "KJLN" in upper:
            result["entity"] = "KJLN"
        elif "NHCS" in upper or "NEW HEIGHTS" in upper:
            result["entity"] = "NHCS"
        elif "MARY" in upper:
            result["entity"] = "MARYS_HOME"

    # Action
    action_match = re.search(
        r"(?:action)\s*[:#]?\s*(resubmit|write\s*off|rebill|appeal|cancel|posted|fixed)",
        lines, re.IGNORECASE,
    )
    if action_match:
        raw_action = action_match.group(1).strip().lower()
        result["action"] = raw_action.replace(" ", "_")
    else:
        lower = lines.lower()
        if "write off" in lower or "write-off" in lower or "w/o" in lower:
            result["action"] = "write_off"
        elif "resubmit" in lower or "re-submit" in lower:
            result["action"] = "resubmit"
        elif "rebill" in lower or "re-bill" in lower:
            result["action"] = "rebill"
        elif "appeal" in lower:
            result["action"] = "appeal"

    return result


def get_response_instructions(task_type: str) -> str:
    """Get the response instruction block for a task type."""
    return RESPONSE_INSTRUCTIONS.get(task_type, "")
