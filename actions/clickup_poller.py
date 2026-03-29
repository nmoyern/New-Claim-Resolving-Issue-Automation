"""
actions/clickup_poller.py
--------------------------
Polls ClickUp for completed tasks and processes responses.

The automation creates tasks for human review (bank verification,
entity fixes, intake issues, write-off approvals, etc.). This module
checks those tasks for completion and acts on the responses.

Workflow:
  1. Query SQLite for all open automation-created tasks
  2. For each, call ClickUp API to get current status + comments
  3. If task is "complete" or "closed":
     a. Extract the response from the most recent comment
     b. Route to the appropriate action handler
     c. Mark the task as resolved in SQLite
  4. Log everything for audit trail

Task types and expected responses:
  - bank_verify:       Justin confirms payment received (→ mark_paid)
  - entity_fix:        Justin identifies correct entity (→ fix billing co)
  - intake_issue:      NaTarsha confirms intake/Dropbox fix (→ retry claim)
  - write_off_approval: Desiree approves/denies write-offs (→ process batch)
  - diagnosis_missing:  Staff provides diagnosis code (→ update + resubmit)
  - insurance_change:   Coach confirms action taken (→ close loop)
  - generic:           Any other task (→ log completion)
"""
from __future__ import annotations

import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional

import aiohttp

from config.settings import CLICKUP_API_TOKEN, DRY_RUN
from logging_utils.logger import get_logger

logger = get_logger("clickup_poller")

_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_PATH = _DB_DIR / "claims_history.db"

CLICKUP_BASE = "https://api.clickup.com/api/v2"

# Task types we recognize in task names/descriptions
TASK_TYPE_PATTERNS = {
    "bank_verify": [
        "payment not received", "bank", "reconcil",
    ],
    "entity_fix": [
        "entity", "billing company", "correct entity",
        "kjln", "nhcs",
    ],
    "intake_issue": [
        "dropbox save", "intake", "dropbox",
    ],
    "write_off_approval": [
        "write-off approval", "write off approval",
    ],
    "diagnosis_missing": [
        "diagnosis missing", "diagnosis blank",
    ],
    "insurance_change": [
        "insurance change", "coverage terminated",
    ],
    "pre_billing": [
        "pre-billing", "pre billing",
    ],
    "entity_verification": [
        "entity verification", "verify company", "verify entity",
        "needs verification", "correct company",
    ],
}

# ClickUp statuses that mean "done"
DONE_STATUSES = {"complete", "closed", "done", "resolved", "approved"}


def _ensure_poller_table():
    """Extend the clickup_patient_tasks table with task_type and response fields."""
    conn = sqlite3.connect(str(_DB_PATH))
    # Add columns if they don't exist (SQLite doesn't error on duplicate ADD)
    for col, col_type in [
        ("task_type", "TEXT DEFAULT 'generic'"),
        ("response_text", "TEXT DEFAULT ''"),
        ("response_by", "TEXT DEFAULT ''"),
        ("response_date", "TEXT DEFAULT ''"),
        ("claim_id", "TEXT DEFAULT ''"),
        ("unique_code", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE clickup_patient_tasks ADD COLUMN {col} {col_type}"
            )
        except sqlite3.OperationalError:
            pass  # Column already exists
    conn.commit()
    conn.close()


_ensure_poller_table()


def _classify_task_type(name: str, description: str = "") -> str:
    """Determine task type from name/description text."""
    combined = f"{name} {description}".lower()
    for task_type, patterns in TASK_TYPE_PATTERNS.items():
        if any(p in combined for p in patterns):
            return task_type
    return "generic"


async def poll_completed_tasks() -> Dict[str, int]:
    """
    Poll ClickUp for tasks created by the automation that are now complete.
    Process each completed task's response and take appropriate action.

    Returns summary dict with counts.
    """
    result = {
        "tasks_checked": 0,
        "tasks_completed": 0,
        "actions_taken": 0,
        "errors": 0,
    }

    if not CLICKUP_API_TOKEN:
        logger.warning("No CLICKUP_API_TOKEN — cannot poll tasks")
        return result

    # Get all open tasks from our tracking table
    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    open_tasks = conn.execute(
        """SELECT task_id, patient_name, task_type, claim_id, unique_code
           FROM clickup_patient_tasks
           WHERE status = 'open'
           ORDER BY created_at ASC"""
    ).fetchall()
    conn.close()

    if not open_tasks:
        logger.info("No open automation tasks to poll")
        return result

    logger.info("Polling ClickUp for task completions",
                open_tasks=len(open_tasks))

    headers = {
        "Authorization": CLICKUP_API_TOKEN,
        "Content-Type": "application/json",
    }

    async with aiohttp.ClientSession() as session:
        for task_row in open_tasks:
            task_id = task_row["task_id"]
            result["tasks_checked"] += 1

            try:
                # Get task status from ClickUp
                task_data = await _get_task(session, headers, task_id)
                if not task_data:
                    continue

                task_name = task_data.get("name", "")
                status = (
                    task_data.get("status", {}).get("status", "")
                ).lower()

                # Check if task is completed
                if status not in DONE_STATUSES:
                    continue

                result["tasks_completed"] += 1
                logger.info(
                    "Completed task found",
                    task_id=task_id,
                    name=task_name,
                    status=status,
                )

                # Get comments to find the human response
                comments = await _get_task_comments(
                    session, headers, task_id
                )
                response_text, response_by = _extract_response(
                    comments, task_name
                )

                # Determine task type if not already classified
                task_type = task_row["task_type"] or "generic"
                if task_type == "generic":
                    desc = task_data.get("description", "")
                    task_type = _classify_task_type(task_name, desc)

                # Process the response based on task type
                action_taken = await _handle_completed_task(
                    task_type=task_type,
                    task_id=task_id,
                    task_name=task_name,
                    patient_name=task_row["patient_name"],
                    claim_id=task_row["claim_id"] or "",
                    unique_code=task_row["unique_code"] or "",
                    response_text=response_text,
                    response_by=response_by,
                )

                if action_taken:
                    result["actions_taken"] += 1

                # Mark task as resolved in SQLite
                _mark_task_resolved(
                    task_id, response_text, response_by, task_type
                )

            except Exception as e:
                logger.error(
                    "Error polling task",
                    task_id=task_id, error=str(e),
                )
                result["errors"] += 1

    logger.info("ClickUp polling complete", **result)
    return result


async def _get_task(
    session: aiohttp.ClientSession,
    headers: dict,
    task_id: str,
) -> Optional[dict]:
    """Fetch a single task from ClickUp API."""
    url = f"{CLICKUP_BASE}/task/{task_id}"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                return await resp.json()
            elif resp.status == 404:
                logger.warning("Task not found in ClickUp (deleted?)",
                               task_id=task_id)
                _mark_task_resolved(
                    task_id, "Task deleted from ClickUp", "system", "generic"
                )
                return None
            else:
                body = await resp.text()
                logger.warning("ClickUp API error",
                               task_id=task_id, status=resp.status,
                               body=body[:200])
                return None
    except Exception as e:
        logger.error("Failed to fetch task", task_id=task_id, error=str(e))
        return None


async def _get_task_comments(
    session: aiohttp.ClientSession,
    headers: dict,
    task_id: str,
) -> List[dict]:
    """Fetch comments on a task, newest first."""
    url = f"{CLICKUP_BASE}/task/{task_id}/comment"
    try:
        async with session.get(url, headers=headers) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("comments", [])
            return []
    except Exception:
        return []


def _extract_response(comments: List[dict], task_name: str) -> tuple:
    """
    Extract the human response from task comments.
    Ignores comments from the automation itself.
    Returns (response_text, response_by).
    """
    # Look at comments in reverse chronological order
    for comment in reversed(comments):
        user = comment.get("user", {})
        username = user.get("username", "")
        comment_text = ""

        # ClickUp comment structure: comment_text or comment array
        if "comment_text" in comment:
            comment_text = comment["comment_text"]
        elif "comment" in comment:
            # comment is a list of text segments
            parts = comment["comment"]
            if isinstance(parts, list):
                comment_text = " ".join(
                    p.get("text", "") for p in parts
                    if isinstance(p, dict)
                )
            elif isinstance(parts, str):
                comment_text = parts

        # Skip automation-generated comments
        if not comment_text:
            continue
        lower = comment_text.lower()
        if any(marker in lower for marker in (
            "generated by claims automation",
            "#auto",
            "autonomous actions taken",
        )):
            continue

        if comment_text.strip():
            return comment_text.strip(), username

    return "", ""


async def _handle_completed_task(
    task_type: str,
    task_id: str,
    task_name: str,
    patient_name: str,
    claim_id: str,
    unique_code: str,
    response_text: str,
    response_by: str,
) -> bool:
    """
    Route a completed task to the appropriate action handler.
    Returns True if an action was taken.
    """
    logger.info(
        "Processing completed task",
        task_type=task_type,
        task_id=task_id,
        response_by=response_by,
        response_preview=response_text[:100] if response_text else "(none)",
    )

    if DRY_RUN:
        logger.info(
            "DRY_RUN: Would process completed task",
            task_type=task_type, task_id=task_id,
        )
        return True

    try:
        if task_type == "bank_verify":
            return await _handle_bank_verify(
                unique_code, response_text, response_by
            )
        elif task_type == "entity_fix":
            return await _handle_entity_fix(
                claim_id, patient_name, response_text, response_by
            )
        elif task_type == "intake_issue":
            return await _handle_intake_resolved(
                claim_id, patient_name, response_text, response_by
            )
        elif task_type == "write_off_approval":
            return await _handle_writeoff_approval(
                response_text, response_by
            )
        elif task_type == "diagnosis_missing":
            return await _handle_diagnosis_provided(
                claim_id, patient_name, response_text, response_by
            )
        elif task_type == "insurance_change":
            return await _handle_insurance_change_resolved(
                patient_name, response_text, response_by
            )
        else:
            # Generic — just log the completion
            logger.info(
                "Generic task completed — no automated follow-up",
                task_id=task_id,
                task_name=task_name,
                response_by=response_by,
            )
            return True

    except Exception as e:
        logger.error(
            "Error handling completed task",
            task_type=task_type, task_id=task_id, error=str(e),
        )
        return False


# ---------------------------------------------------------------------------
# Individual response handlers
# ---------------------------------------------------------------------------

async def _handle_bank_verify(
    unique_code: str, response_text: str, response_by: str
) -> bool:
    """Justin confirmed a bank payment was received."""
    if not unique_code:
        # Try to extract code from response text
        match = re.search(r'PAY-[A-Z0-9]{6}', response_text or "")
        if match:
            unique_code = match.group(0)

    if unique_code:
        try:
            from reconciliation.payment_tracker import PaymentTracker
            tracker = PaymentTracker()
            # Extract paid date if mentioned
            date_match = re.search(
                r'(\d{1,2}/\d{1,2}/\d{2,4})', response_text or ""
            )
            paid_date = date_match.group(1) if date_match else ""

            success = tracker.mark_paid(
                unique_code,
                paid_date=paid_date,
                marked_by=f"clickup:{response_by}",
            )
            tracker.close()
            if success:
                logger.info(
                    "Bank payment verified via ClickUp",
                    code=unique_code, by=response_by,
                )
            return success
        except Exception as e:
            logger.error("Bank verify handler failed", error=str(e))
    return False


async def _handle_entity_fix(
    claim_id: str, patient_name: str,
    response_text: str, response_by: str,
) -> bool:
    """Justin identified the correct entity for a claim."""
    # Parse entity from response
    response_lower = (response_text or "").lower()
    correct_entity = ""
    if "kjln" in response_lower:
        correct_entity = "KJLN"
    elif "nhcs" in response_lower:
        correct_entity = "NHCS"
    elif "mary" in response_lower:
        correct_entity = "MARYS_HOME"

    if correct_entity and claim_id:
        try:
            from sources.claimmd_api import ClaimMDAPI
            api = ClaimMDAPI()
            if api.key:
                success = await api.modify_claim(
                    claim_id, {"billing_region": correct_entity}
                )
                if success:
                    await api.add_claim_note(
                        claim_id,
                        f"Entity corrected to {correct_entity} per "
                        f"{response_by} (ClickUp task). "
                        f"#AUTO #{date.today().strftime('%m/%d/%y')}",
                    )
                    logger.info(
                        "Entity fix applied from ClickUp",
                        claim_id=claim_id,
                        entity=correct_entity,
                        by=response_by,
                    )
                return success
        except Exception as e:
            logger.error("Entity fix handler failed", error=str(e))
    else:
        logger.info(
            "Entity fix task completed but no entity detected in response",
            patient=patient_name,
            response=response_text[:100] if response_text else "",
        )
    return False


async def _handle_intake_resolved(
    claim_id: str, patient_name: str,
    response_text: str, response_by: str,
) -> bool:
    """NaTarsha confirmed intake/Dropbox issue is resolved."""
    logger.info(
        "Intake issue resolved via ClickUp",
        patient=patient_name,
        by=response_by,
    )
    # Add note to claim if we have a claim_id
    if claim_id:
        try:
            from sources.claimmd_api import ClaimMDAPI
            api = ClaimMDAPI()
            if api.key:
                await api.add_claim_note(
                    claim_id,
                    f"Intake/Dropbox issue resolved by {response_by}. "
                    f"#AUTO #{date.today().strftime('%m/%d/%y')}",
                )
                return True
        except Exception as e:
            logger.error("Intake resolved handler failed", error=str(e))
    return True  # Still counts as handled


async def _handle_writeoff_approval(
    response_text: str, response_by: str,
) -> bool:
    """Desiree approved or denied write-offs."""
    response_lower = (response_text or "").lower()
    approved = any(w in response_lower for w in (
        "approved", "approve", "yes", "ok", "confirmed",
    ))
    denied = any(w in response_lower for w in (
        "denied", "deny", "no", "reject",
    ))

    if approved:
        logger.info(
            "Write-off batch approved via ClickUp",
            by=response_by,
        )
        # The write-offs were already queued — approval means they proceed
        # The actual write-off processing happens in the orchestrator
        return True
    elif denied:
        logger.info(
            "Write-off batch denied via ClickUp",
            by=response_by,
            response=response_text[:200],
        )
        # Log denial — claims stay in queue for re-review
        return True

    logger.info(
        "Write-off approval response unclear",
        by=response_by,
        response=response_text[:200] if response_text else "",
    )
    return True  # Task is still resolved


async def _handle_diagnosis_provided(
    claim_id: str, patient_name: str,
    response_text: str, response_by: str,
) -> bool:
    """Staff provided the missing diagnosis code.
    Updates Lauris facesheet AND Claim.MD, then resubmits."""
    icd_match = re.search(
        r'\b([A-TV-Z]\d{2}(?:\.\d{1,4})?)\b',
        response_text or "",
    )
    if not icd_match or not claim_id:
        logger.info(
            "Diagnosis task completed but no ICD-10 code detected",
            patient=patient_name,
            response=response_text[:100] if response_text else "",
        )
        return False

    diag_code = icd_match.group(1)
    # Extract description if provided (e.g., "F33.1 Major depressive...")
    desc_match = re.search(
        r'[A-TV-Z]\d{2}(?:\.\d{1,4})?\s*[-–]?\s*(.*)',
        response_text or "",
    )
    description = desc_match.group(1).strip() if desc_match else ""

    try:
        # Step 1: Update Lauris facesheet
        from lauris.billing import LaurisSession
        from lauris.diagnosis import (
            _lookup_uid_from_record_number,
            update_facesheet_diagnosis,
        )
        from sources.claimmd_api import ClaimMDAPI

        api = ClaimMDAPI()

        # Get member ID from claim to find Lauris UID
        # Try to get it from the claim responses
        member_id = ""
        if api.key:
            responses = await api.get_claim_responses(
                response_id="0", claim_id=claim_id
            )
            for r in responses:
                mid = (r.get("ins_number", "") or "").strip()
                if mid:
                    member_id = mid
                    break

        if member_id:
            async with LaurisSession(headless=True) as lauris:
                uid = await _lookup_uid_from_record_number(
                    lauris.page, member_id
                )
                if uid:
                    await update_facesheet_diagnosis(
                        lauris.page, uid,
                        diag_code, description,
                    )
                    logger.info(
                        "Facesheet updated from ClickUp response",
                        uid=uid, diag=diag_code, by=response_by,
                    )

        # Step 2: Update Claim.MD and resubmit
        if api.key:
            success = await api.modify_claim(
                claim_id, {"diag": diag_code}
            )
            if success:
                await api.add_claim_note(
                    claim_id,
                    f"Diagnosis {diag_code} added per "
                    f"{response_by} (ClickUp). "
                    f"Facesheet updated. Resubmitting. "
                    f"#AUTO #{date.today().strftime('%m/%d/%y')}",
                )
                logger.info(
                    "Diagnosis applied from ClickUp",
                    claim_id=claim_id,
                    diag=diag_code,
                    by=response_by,
                )

                # Step 3: Log autonomous correction
                from reporting.autonomous_tracker import (
                    log_autonomous_correction,
                )
                log_autonomous_correction(
                    claim_id=claim_id,
                    client_name=patient_name,
                    client_id=member_id,
                    correction_type="diagnosis_fix",
                    correction_detail=(
                        f"{diag_code} added per {response_by} "
                        f"via ClickUp"
                    ),
                )
            return success

    except Exception as e:
        logger.error(
            "Diagnosis handler failed", error=str(e)
        )
    return False


async def _handle_insurance_change_resolved(
    patient_name: str, response_text: str, response_by: str,
) -> bool:
    """Life coach confirmed insurance change action taken."""
    logger.info(
        "Insurance change task resolved via ClickUp",
        patient=patient_name,
        by=response_by,
    )
    return True


# ---------------------------------------------------------------------------
# SQLite helpers
# ---------------------------------------------------------------------------

def _mark_task_resolved(
    task_id: str,
    response_text: str,
    response_by: str,
    task_type: str,
):
    """Mark a task as resolved in our tracking table."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """UPDATE clickup_patient_tasks SET
            status = 'resolved',
            task_type = ?,
            response_text = ?,
            response_by = ?,
            response_date = ?,
            updated_at = ?
           WHERE task_id = ?""",
        (
            task_type,
            (response_text or "")[:500],
            response_by,
            datetime.now().isoformat(),
            datetime.now().isoformat(),
            task_id,
        ),
    )
    conn.commit()
    conn.close()
    logger.info("Task marked as resolved in SQLite", task_id=task_id)


def store_task_metadata(
    task_id: str,
    task_type: str = "generic",
    claim_id: str = "",
    unique_code: str = "",
    original_due_date: str = "",
):
    """Store additional metadata for a task (called when task is created)."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        """UPDATE clickup_patient_tasks SET
            task_type = ?,
            claim_id = ?,
            unique_code = ?,
            original_due_date = ?
           WHERE task_id = ?""",
        (task_type, claim_id, unique_code, original_due_date, task_id),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Conversational follow-up system
# ---------------------------------------------------------------------------

REQUIRED_INFO = {
    "diagnosis_missing": {
        "pattern": r"\b([A-TV-Z]\d{2}(?:\.\d{1,4})?)\b",
        "description": "ICD-10 diagnosis code (e.g. F33.1)",
        "example": "F33.1",
    },
    "entity_fix": {
        "pattern": r"\b(KJLN|NHCS|Mary'?s?\s*Home)\b",
        "description": "correct billing entity (KJLN, NHCS, or Mary's Home)",
        "example": "KJLN",
    },
    "bank_verify": {
        "pattern": r"(PAY-[A-Z0-9]{6}|confirm|verified|received|paid)",
        "description": "payment confirmation or PAY code",
        "example": "Confirmed payment received",
    },
    "write_off_approval": {
        "pattern": r"\b(approve[d]?|deny|denied|reject|yes|no)\b",
        "description": "approval or denial (Approved/Denied)",
        "example": "Approved",
    },
    "insurance_change": {
        "pattern": r"(contact|spoke|called|verified|updated|confirmed)",
        "description": "confirmation that the client was contacted",
        "example": "Contacted client, insurance updated",
    },
    "intake_issue": {
        "pattern": r"(fix|resolved|uploaded|saved|corrected|done|complete)",
        "description": "confirmation that the issue is resolved",
        "example": "Resolved - document saved to Dropbox",
    },
    "pre_billing": {
        "pattern": r"(fix|resolved|corrected|done|complete|updated|verified)",
        "description": "confirmation that the pre-billing issue is resolved",
        "example": "Fixed and ready for billing",
    },
    "entity_verification": {
        "pattern": r"(verified|confirmed|correct|yes)",
        "description": "confirmation that the entity/company is verified",
        "example": "verified",
    },
}


def _add_poller_columns():
    """Add tracking columns for conversational follow-up."""
    conn = sqlite3.connect(str(_DB_PATH))
    for col, col_type in [
        ("last_comment_id", "TEXT DEFAULT ''"),
        ("follow_up_count", "INTEGER DEFAULT 0"),
        ("last_follow_up_date", "TEXT DEFAULT ''"),
        ("escalated", "INTEGER DEFAULT 0"),
        ("original_due_date", "TEXT DEFAULT ''"),
        ("original_assignees", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(
                f"ALTER TABLE clickup_patient_tasks ADD COLUMN {col} {col_type}"
            )
        except sqlite3.OperationalError:
            pass
    conn.commit()
    conn.close()


_add_poller_columns()


def _validate_response(task_type: str, response_text: str) -> dict:
    """Check if the response contains the required information."""
    if not response_text or not response_text.strip():
        info = REQUIRED_INFO.get(task_type, {})
        return {"valid": False, "extracted": "", "missing": info.get("description", "a response")}

    info = REQUIRED_INFO.get(task_type)
    if not info:
        return {"valid": True, "extracted": response_text, "missing": ""}

    match = re.search(info["pattern"], response_text, re.IGNORECASE)
    if match:
        return {"valid": True, "extracted": match.group(0), "missing": ""}
    return {"valid": False, "extracted": "", "missing": info["description"]}


async def check_open_task_comments() -> dict:
    """
    Check ALL open tasks for new comments from team members.

    Flow:
      1. Get comments → find new human comments
      2. If valid response → process it, close task, thank them
      3. If invalid → reply asking for clarification
      4. If overdue with no response → send follow-up reminder
      5. Enforce due date — if removed by team, restore it
      6. Enforce assignee — if all removed, restore original
    """
    result = {
        "tasks_checked": 0, "responses_found": 0,
        "actions_taken": 0, "follow_ups_sent": 0,
        "due_dates_restored": 0, "assignees_restored": 0,
        "errors": 0,
    }

    if not CLICKUP_API_TOKEN:
        return result

    conn = sqlite3.connect(str(_DB_PATH))
    conn.row_factory = sqlite3.Row
    open_tasks = conn.execute(
        """SELECT task_id, patient_name, task_type, claim_id,
                  unique_code, last_comment_id, follow_up_count,
                  last_follow_up_date, escalated, created_at,
                  original_due_date, original_assignees
           FROM clickup_patient_tasks
           WHERE status = 'open'
           ORDER BY created_at ASC"""
    ).fetchall()
    conn.close()

    if not open_tasks:
        return result

    logger.info("Checking open tasks for comments", open_tasks=len(open_tasks))

    headers = {"Authorization": CLICKUP_API_TOKEN, "Content-Type": "application/json"}

    async with aiohttp.ClientSession() as session:
        for task_row in open_tasks:
            task_id = task_row["task_id"]
            result["tasks_checked"] += 1

            try:
                task_data = await _get_task(session, headers, task_id)
                if not task_data:
                    continue

                task_name = task_data.get("name", "")
                status = task_data.get("status", {}).get("status", "").lower()
                due_ts = task_data.get("due_date")
                assignees = task_data.get("assignees", [])
                task_type = task_row["task_type"] or _classify_task_type(
                    task_name, task_data.get("description", "")
                )

                if status in DONE_STATUSES:
                    continue

                # --- Enforce due date (don't let team remove it) ---
                original_due = task_row["original_due_date"] or ""
                if original_due and not due_ts:
                    await _restore_due_date(session, headers, task_id, original_due)
                    result["due_dates_restored"] += 1
                    logger.info("Due date restored", task_id=task_id)

                # --- Enforce assignee (must have at least one) ---
                if not assignees:
                    orig_assignees = task_row["original_assignees"] or ""
                    if orig_assignees:
                        await _restore_assignees(
                            session, headers, task_id, orig_assignees
                        )
                        result["assignees_restored"] += 1
                        logger.info("Assignees restored", task_id=task_id)

                # --- Check for new comments ---
                comments = await _get_task_comments(session, headers, task_id)
                last_seen = task_row["last_comment_id"] or ""
                new_comments = _get_new_human_comments(comments, last_seen)

                if new_comments:
                    result["responses_found"] += 1
                    latest = new_comments[-1]
                    response_text = latest["text"]
                    response_by = latest["username"]
                    latest_id = latest["comment_id"]

                    validation = _validate_response(task_type, response_text)

                    if validation["valid"]:
                        action_taken = await _handle_completed_task(
                            task_type=task_type, task_id=task_id,
                            task_name=task_name,
                            patient_name=task_row["patient_name"],
                            claim_id=task_row["claim_id"] or "",
                            unique_code=task_row["unique_code"] or "",
                            response_text=response_text,
                            response_by=response_by,
                        )
                        if action_taken:
                            result["actions_taken"] += 1
                            await _update_task_status(
                                session, headers, task_id, "complete"
                            )
                            await _post_comment(
                                session, headers, task_id,
                                f"Thank you, {response_by}. The information "
                                f"has been processed and the claim updated."
                                f"\n\n#AUTO",
                            )
                            _mark_task_resolved(
                                task_id, response_text, response_by, task_type
                            )
                    else:
                        missing = validation["missing"]
                        info = REQUIRED_INFO.get(task_type, {})
                        example = info.get("example", "")
                        ex_str = f" (e.g., {example})" if example else ""

                        await _post_comment(
                            session, headers, task_id,
                            f"Thank you for responding, {response_by}. "
                            f"However, I could not find the {missing} "
                            f"in your response.\n\n"
                            f"Could you please provide the {missing}{ex_str}?"
                            f"\n\n"
                            f'Your response: "{response_text[:200]}"\n\n#AUTO',
                        )
                        result["follow_ups_sent"] += 1

                    _update_last_comment(task_id, latest_id)

                else:
                    # No new comments — send follow-up if overdue
                    if due_ts:
                        due_date = datetime.fromtimestamp(int(due_ts) / 1000)
                        days_overdue = (datetime.now() - due_date).days

                        # Only follow up once per day max
                        last_fu = task_row["last_follow_up_date"] or ""
                        already_followed_today = (
                            last_fu and last_fu[:10] == date.today().isoformat()
                        )

                        if days_overdue >= 1 and not already_followed_today:
                            follow_count = (task_row["follow_up_count"] or 0) + 1
                            await _post_comment(
                                session, headers, task_id,
                                f"This task is {days_overdue} day(s) overdue. "
                                f"A response is needed to resolve this claim."
                                f"\n\n(Follow-up #{follow_count})\n\n#AUTO",
                            )

                            conn2 = sqlite3.connect(str(_DB_PATH))
                            conn2.execute(
                                """UPDATE clickup_patient_tasks
                                   SET follow_up_count = ?,
                                       last_follow_up_date = ?,
                                       updated_at = ?
                                   WHERE task_id = ?""",
                                (follow_count, datetime.now().isoformat(),
                                 datetime.now().isoformat(), task_id),
                            )
                            conn2.commit()
                            conn2.close()
                            result["follow_ups_sent"] += 1

            except Exception as e:
                logger.error("Error checking task", task_id=task_id, error=str(e))
                result["errors"] += 1

    logger.info("Open task comment check complete", **result)
    return result


def _get_new_human_comments(comments: List[dict], last_seen_id: str) -> List[dict]:
    """Extract human comments newer than last_seen_id."""
    new_comments = []
    past_marker = not last_seen_id

    for comment in comments:
        comment_id = str(comment.get("id", ""))
        if not past_marker:
            if comment_id == last_seen_id:
                past_marker = True
            continue

        text = ""
        if "comment_text" in comment:
            text = comment["comment_text"]
        elif "comment" in comment:
            parts = comment["comment"]
            if isinstance(parts, list):
                text = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
            elif isinstance(parts, str):
                text = parts

        if not text or not text.strip():
            continue
        if any(m in text.lower() for m in ("#auto", "generated by claims automation")):
            continue

        user = comment.get("user", {})
        new_comments.append({
            "comment_id": comment_id,
            "text": text.strip(),
            "username": user.get("username", ""),
            "user_id": user.get("id", ""),
        })
    return new_comments


def _update_last_comment(task_id: str, comment_id: str):
    """Update the last seen comment ID."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute(
        "UPDATE clickup_patient_tasks SET last_comment_id=?, updated_at=? WHERE task_id=?",
        (comment_id, datetime.now().isoformat(), task_id),
    )
    conn.commit()
    conn.close()


async def _post_comment(
    session: aiohttp.ClientSession, headers: dict,
    task_id: str, comment_text: str,
) -> bool:
    """Post a comment to a ClickUp task."""
    if DRY_RUN:
        logger.info("DRY_RUN: Would post comment", task_id=task_id, comment=comment_text[:80])
        return True
    url = f"{CLICKUP_BASE}/task/{task_id}/comment"
    try:
        async with session.post(
            url, json={"comment_text": comment_text, "notify_all": True}, headers=headers
        ) as resp:
            if resp.status in (200, 201):
                return True
            body = await resp.text()
            logger.warning("Failed to post comment", task_id=task_id, status=resp.status)
    except Exception as e:
        logger.error("Error posting comment", task_id=task_id, error=str(e))
    return False


async def _update_task_status(
    session: aiohttp.ClientSession, headers: dict,
    task_id: str, status: str,
) -> bool:
    """Update a task's status."""
    if DRY_RUN:
        return True
    url = f"{CLICKUP_BASE}/task/{task_id}"
    try:
        async with session.put(url, json={"status": status}, headers=headers) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _restore_due_date(
    session: aiohttp.ClientSession, headers: dict,
    task_id: str, original_due_iso: str,
) -> bool:
    """Restore a due date that was removed by a team member."""
    if DRY_RUN:
        return True
    try:
        due_dt = datetime.fromisoformat(original_due_iso)
        due_ts = int(due_dt.timestamp() * 1000)
        url = f"{CLICKUP_BASE}/task/{task_id}"
        async with session.put(
            url, json={"due_date": due_ts, "due_date_time": True}, headers=headers
        ) as resp:
            return resp.status == 200
    except Exception:
        return False


async def _restore_assignees(
    session: aiohttp.ClientSession, headers: dict,
    task_id: str, original_assignees_str: str,
) -> bool:
    """Restore assignees if all were removed. Expects comma-separated IDs."""
    if DRY_RUN:
        return True
    try:
        assignee_ids = [int(a.strip()) for a in original_assignees_str.split(",") if a.strip()]
        if not assignee_ids:
            return False
        url = f"{CLICKUP_BASE}/task/{task_id}"
        async with session.put(
            url, json={"assignees": {"add": assignee_ids}}, headers=headers
        ) as resp:
            return resp.status == 200
    except Exception:
        return False
